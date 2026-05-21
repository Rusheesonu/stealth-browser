"""Camoufox engine — patched Firefox, the only known thing that beats CreepJS.

Why this engine exists:
  Web research (May 2026) confirmed: every Chromium-based stealth stack
  leaves Chromium-specific fingerprints that CreepJS, fingerprint.com,
  and the more sophisticated PerimeterX configurations latch onto. The
  only known way to score 0% on CreepJS's "headless" + "stealth-detection"
  scales is to switch ENGINES — to patched Firefox. Camoufox is that
  patched Firefox, plus a Playwright-compatible async API. MIT-licensed.

What this engine is GREAT for:
  - CreepJS clean verdict (the one thing Chromium stealth never gets)
  - fingerprint.com bot-detect — Firefox blends in differently
  - PerimeterX behavioral mode (not press-and-hold; that still needs solver)
  - Kasada (specifically targets headless Chromium; Firefox sidesteps)
  - Any vendor with Chromium-specific runtime sensors

What this engine COSTS:
  - ~350MB binary (one-time download, cached in ~/Library/Caches/camoufox)
  - ~600MB RAM per browser instance (similar to Chromium)
  - Cold-start ~3-5s (similar to Chromium)
  - Per-page latency comparable to nodriver: 8-12s typical

What this engine CAN'T do:
  - Native CDP — uses Playwright async API instead (different but capable)
  - Reuse our existing nodriver browser pool (separate process)
  - Some sites SPECIFICALLY target Firefox quirks (rare, but exists)

Router strategy:
  - FIREFOX_ENGINE capability set — distinguishes from Chromium stack
  - JS_EXEC, SCREENSHOT, DOM_QUERY — full browser features
  - Cost: 2¢/page (slightly higher RAM than nodriver = pricier compute)
  - VENDOR_AFFINITY puts camoufox FIRST for: kasada, perimeterx
  - VENDOR_AFFINITY puts camoufox LAST for: cloudflare (nodriver fine there)

Element extraction:
  We run a small JS pass that collects up to 100 *real* visible elements
  with bbox + tag + truncated text. No placeholder padding — every row
  in `elements` is a real DOM node. Schema matches NodriverEngine's
  extract_js.COLLECT_ELEMENTS_JS shape (tag/text/css/xpath/attrs/bbox)
  so downstream code is engine-agnostic.

Lazy import: the engine class itself doesn't import Camoufox/Playwright
at module-load time. is_available() does the import probe, and snapshot()
imports for real. Keeps router init light when camoufox isn't installed.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Optional

from .base import (
    Capability,
    EngineFailedError,
    EngineSnapshotResult,
    Requirements,
)


# Lightweight cross-engine element extractor. Runs in the page context,
# returns up to 100 *real* visible elements with bboxes. Schema matches
# extract_js.COLLECT_ELEMENTS_JS so engines are interchangeable downstream.
_EXTRACT_ELEMENTS_JS = """
(() => {
  const SELECTOR = 'a, button, input, select, textarea, h1, h2, h3, h4, '
                 + 'img, p, span, li, label, form, nav, header, footer, '
                 + 'section, article, main, [role="button"], [role="link"]';
  const out = [];
  const all = document.querySelectorAll(SELECTOR);
  const cssPath = (el) => {
    if (!el || el === document.body) return 'body';
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 6) {
      let part = cur.tagName.toLowerCase();
      if (cur.className && typeof cur.className === 'string') {
        const cls = cur.className.split(/\\s+/).filter(Boolean)[0];
        if (cls) part += '.' + cls;
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  for (const el of all) {
    if (out.length >= 100) break;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const text = (el.innerText || el.value || el.alt || '').trim();
    out.push({
      tag: el.tagName.toLowerCase(),
      text: text.slice(0, 500),
      css: cssPath(el),
      xpath: '',
      attrs: {
        id: el.id || null,
        class: (typeof el.className === 'string') ? el.className : null,
        href: el.href || null,
        name: el.name || null,
        type: el.type || null,
      },
      bbox: {
        x: Math.round(r.x),
        y: Math.round(r.y),
        w: Math.round(r.width),
        h: Math.round(r.height),
      },
    });
  }
  return {
    elements: out,
    real_total: all.length,
    page_height: Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0,
    ),
  };
})()
"""


class CamoufoxEngine:
    """Camoufox-driven Firefox engine. Best on Chromium-targeted detectors."""

    name = "camoufox"
    capabilities = (
        Capability.JS_EXEC
        | Capability.SCREENSHOT
        | Capability.DOM_QUERY
        | Capability.FIREFOX_ENGINE      # KEY — beats Chromium-targeted detection
        | Capability.PROXY_SUPPORT
        | Capability.COOKIE_PERSISTENCE
        | Capability.MOBILE_EMULATION    # camoufox supports os= for spoofing
        # Not advertising TLS_IMPERSONATION because Camoufox uses Firefox's
        # native TLS stack — distinct from Chrome's but not impersonating it.
    )
    cost_per_request_cents = 2   # slightly higher than nodriver due to Firefox RAM

    async def is_available(self) -> bool:
        """Verify camoufox package + downloaded Firefox binary.

        get_path("camoufox") returns the wrong path on macOS (Resources/
        vs MacOS/ subdir of the .app bundle), so we instead check that
        the cache root has the launcher binary at any of the documented
        locations. Belt-and-suspenders for cross-platform install layouts.
        """
        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except Exception:
            return False
        try:
            from platformdirs import user_cache_dir
            root = user_cache_dir("camoufox")
        except Exception:
            root = os.path.expanduser("~/Library/Caches/camoufox")
        if not os.path.isdir(root):
            return False
        candidates = [
            os.path.join(root, "Camoufox.app", "Contents", "MacOS", "camoufox"),  # macOS bundle
            os.path.join(root, "camoufox"),                                        # Linux flat layout
            os.path.join(root, "camoufox-bin"),                                    # alt layout
        ]
        return any(os.path.isfile(p) and os.access(p, os.X_OK) for p in candidates)

    async def snapshot(
        self,
        url: str,
        *,
        requirements: Requirements,
    ) -> EngineSnapshotResult:
        """Drive one snapshot through Camoufox (Playwright Firefox API)."""
        from camoufox.async_api import AsyncCamoufox

        # Best-effort proxy plumbing from the same pool nodriver uses,
        # so target sites can't fingerprint engine choice by IP swap.
        # Routes through residential when vendor_hint indicates an
        # IP-rep-sensitive vendor AND a residential plan is configured.
        proxy_config = self._maybe_proxy_config(requirements.vendor_hint)

        # `humanize` enables camoufox's mouse-path simulation. PerimeterX
        # and Kasada specifically score behavioral signals (cursor entropy,
        # micro-stalls, Fitts's-law-shaped trajectories) even on a single
        # navigation — so we turn it on whenever the vendor_hint indicates
        # a behavioral detector, not just when the caller asks for explicit
        # interactions. Cheap (~30ms cursor jitter), no downside.
        behavioral_hint = requirements.vendor_hint in {"perimeterx", "kasada"}
        launch_opts: dict[str, Any] = {
            "headless": True,
            "humanize": requirements.needs_interaction or behavioral_hint,
            "os": "android" if requirements.needs_mobile_ui else "macos",
        }
        if proxy_config:
            launch_opts["proxy"] = proxy_config

        viewport_w = 390 if requirements.needs_mobile_ui else 1440
        viewport_h = 844 if requirements.needs_mobile_ui else 900

        t0 = time.perf_counter()
        try:
            async with AsyncCamoufox(**launch_opts) as browser:
                page = await browser.new_page()
                await page.set_viewport_size({"width": viewport_w, "height": viewport_h})

                timeout_ms = int(min(requirements.max_latency_s, 25.0) * 1000)
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

                # Brief settle wait for client-rendered content. networkidle
                # never fires on pages with long-poll WS / SSE — swallow.
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                # Detection sites (creepjs, fingerprint.com, amiunique)
                # compute verdicts CLIENT-SIDE after networkidle — they
                # probe canvas/WebGL/audio for 4-7s after load. Without
                # this wait, the LLM judge reads the "computing..." state
                # instead of the verdict.
                #
                # `asyncio.sleep(6)` here killed camoufox's websocket
                # keepalive ("Browser.close" on most fingerprint-test sites).
                # Use Playwright-native `page.wait_for_timeout` which keeps
                # the connection alive (it's a yield inside the page's
                # event loop, not a Python-level sleep).
                if requirements.vendor_hint == "fingerprint-test":
                    try:
                        await page.wait_for_timeout(6000)
                    except Exception:
                        pass

                # Single-shot title — avoid the double round-trip.
                raw_title = await page.title()
                title = (raw_title or "")[:200]

                screenshot_bytes = await page.screenshot(full_page=False)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                # Real element extraction — no placeholder padding. Up to 100
                # actual visible elements with real bboxes.
                extract = await page.evaluate(_EXTRACT_ELEMENTS_JS)
                elements = extract.get("elements", []) if isinstance(extract, dict) else []
                real_total = extract.get("real_total", 0) if isinstance(extract, dict) else 0
                page_height = extract.get("page_height", viewport_h) if isinstance(extract, dict) else viewport_h
        except Exception as e:
            raise EngineFailedError(
                f"camoufox snapshot failed: {type(e).__name__}: {e}",
                engine=self.name,
                retriable_on_other_engine=True,
            ) from e

        # NOTE: tried suspicious-empty escalation here. Reverted — the
        # persistent-failure sites (g2.com, hyatt.com, crunchbase-discover)
        # returned ≤2 elements on BOTH engines, so escalation just added
        # latency without recovering any sites. See nodriver_engine.py.

        return EngineSnapshotResult(
            url=url,
            title=title,
            screenshot_base64=screenshot_b64,
            elements=elements,
            viewport={"width": viewport_w, "height": viewport_h},
            page={"width": viewport_w, "height": int(page_height)},
            engine_name=self.name,
            elapsed_s=round(time.perf_counter() - t0, 3),
            cost_cents=self.cost_per_request_cents,
            proxy_used=self._proxy_label(proxy_config),
            cookies_carried=0,
            notes=f"firefox, dom-total={real_total}, returned={len(elements)}",
        )

    @staticmethod
    def _proxy_label(proxy_config: Optional[dict[str, str]]) -> Optional[str]:
        """Credential-stripped proxy label for telemetry."""
        if not proxy_config:
            return None
        server = proxy_config.get("server", "")
        # server is host:port shape, no creds — safe to return directly,
        # but strip any inline userinfo defensively.
        if "@" in server:
            return server.split("@", 1)[-1]
        return server or None

    @staticmethod
    def _maybe_proxy_config(vendor_hint: Optional[str] = None) -> Optional[dict[str, str]]:
        """Build Playwright-shaped proxy config from our pool. None if no
        proxies configured. Same pool nodriver uses.

        Picks residential when vendor_hint is in the IP-rep-sensitive set
        and a residential plan is configured; otherwise datacenter."""
        try:
            from .. import proxies
            url = proxies.pick_for_vendor(vendor_hint)
            if not url:
                return None
            # Parse user:pass@host:port out of the URL so Playwright can
            # consume the typed proxy config.
            # url shape: http://user:pass@host:port
            from urllib.parse import urlparse
            p = urlparse(url)
            return {
                "server": f"{p.scheme}://{p.hostname}:{p.port}",
                "username": p.username or "",
                "password": p.password or "",
            }
        except Exception:
            return None
