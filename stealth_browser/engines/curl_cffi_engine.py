"""curl_cffi engine — TLS-impersonating HTTP, no JS execution.

Why this engine exists:
  DataDome, Akamai BMP, and Cloudflare-Enterprise inspect the TLS
  ClientHello fingerprint (JA3/JA4) BEFORE any JS runs. Chromium-via-
  nodriver has a fingerprint that DIFFERS from real Chrome on certain
  edge cases (cipher ordering, extension list, GREASE values, ALPN order).
  curl-impersonate (the underlying C library that curl_cffi wraps) was
  specifically built to send the EXACT bytes of a real Chrome 131
  ClientHello — so the TLS layer can't tell us apart.

What this engine is GREAT for:
  - Static HTML content (most news/blog/listing pages — surprisingly many)
  - APIs that return JSON
  - Sites where the anti-bot check happens at TLS layer (not JS layer)
  - 50-100x faster than a real browser (~200ms vs ~10s)
  - Cheap: pure HTTP, no Chromium process, ~10MB RAM vs ~800MB

What this engine CAN'T do:
  - Execute JavaScript (the page DOM is what the server returns)
  - Run anti-bot challenge JS (so won't pass Turnstile, PerimeterX, etc.)
  - Capture client-rendered SPAs (React/Vue/Angular apps)
  - Take a screenshot — we don't advertise the SCREENSHOT capability,
    so the router won't pick us when the caller needs a real PNG.

Router strategy:
  - JS_EXEC capability NOT advertised → caller must opt in to non-JS path
  - SCREENSHOT NOT advertised — we have no rendering pipeline
  - LIGHTWEIGHT capability YES → router picks this when prefer_lightweight=True
  - TLS_IMPERSONATION + HTTP2_FINGERPRINT YES → wins on those vendors
  - Cost: 0 cents/page (pure HTTP, no compute beyond the request itself)

Failure escalation:
  - HTTP 403/429/503 → return EngineFailedError(retriable_on_other_engine=True)
    so router falls through to nodriver
  - Hard timeout → same
  - DNS / connection refused → same
"""

from __future__ import annotations

import re
import time
from typing import Optional

from .base import (
    Capability,
    EngineFailedError,
    EngineSnapshotResult,
    Requirements,
)


# Chrome impersonation profile supported by curl_cffi. We pick the
# newest stable that matches our --user-agent (Chrome 131).
# If a future curl_cffi version drops chrome131 support, fall back to
# the closest available via _IMPERSONATE_FALLBACKS.
_IMPERSONATE_PROFILE = "chrome131"
_IMPERSONATE_FALLBACKS = ("chrome124", "chrome120", "chrome116")

# Compiled once at module load — title extraction is hot path.
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)

# HTTP statuses that strongly indicate anti-bot block (not server fault).
_BLOCK_STATUSES = frozenset({403, 429, 503, 520, 521, 522, 525})

# Body text cap. Pure sanity bound — keeps a runaway response (huge
# HTML download) from bloating result JSON / log files. NOT a memory
# budget; curl_cffi already streamed the bytes into RAM by this point.
_BODY_TEXT_CAP = 200_000


class CurlCffiEngine:
    """TLS-impersonating HTTP client. No JS execution, no screenshot."""

    name = "curl_cffi"
    capabilities = (
        Capability.DOM_QUERY           # we return raw HTML; caller parses
        | Capability.TLS_IMPERSONATION # the whole point
        | Capability.HTTP2_FINGERPRINT # curl-impersonate matches Chrome H2 SETTINGS
        | Capability.LIGHTWEIGHT       # ~10MB RAM, <500ms typical
        | Capability.PROXY_SUPPORT     # honors proxy URL
        # NOT advertising: JS_EXEC (no JS), SCREENSHOT (no rendering).
    )
    cost_per_request_cents = 0  # essentially free — pure HTTP

    async def is_available(self) -> bool:
        try:
            import curl_cffi  # noqa: F401
            return True
        except ImportError:
            return False

    async def snapshot(
        self,
        url: str,
        *,
        requirements: Requirements,
    ) -> EngineSnapshotResult:
        """Fetch HTML via curl_cffi with Chrome-131 TLS impersonation.

        Returns the raw HTML in `elements` as a single pseudo-element so
        the existing extract pipeline can still query it. NOT a real
        DOM — caller that needs computed styles / interactive state
        should use a browser engine instead.
        """
        # If caller requires JS execution, we can't help — escalate immediately
        # rather than returning empty HTML the caller will assume is real.
        # (Defensive: the router shouldn't even pick us when needs_js=True
        # because JS_EXEC isn't in our capabilities, but check anyway.)
        if requirements.needs_js:
            raise EngineFailedError(
                "curl_cffi can't execute JS; escalate to a browser engine",
                engine=self.name,
                retriable_on_other_engine=True,
            )

        from curl_cffi.requests import AsyncSession

        # Best-effort proxy plumbing. Passes the vendor_hint so when a
        # residential plan is configured (RESIDENTIAL_PROXIES_JSON env),
        # IP-rep-sensitive vendors (cloudflare, imperva, akamai, datadome)
        # transparently route through residential IPs. Falls back to the
        # datacenter pool, then to direct.
        proxy_url = self._maybe_proxy_url(requirements.vendor_hint)

        t0 = time.perf_counter()
        async with AsyncSession(impersonate=_IMPERSONATE_PROFILE) as session:
            try:
                resp = await session.get(
                    url,
                    proxy=proxy_url,
                    timeout=min(requirements.max_latency_s, 30.0),
                    allow_redirects=True,
                )
            except Exception as e:
                raise EngineFailedError(
                    f"curl_cffi request failed: {type(e).__name__}: {e}",
                    engine=self.name,
                    retriable_on_other_engine=True,
                ) from e

        elapsed = round(time.perf_counter() - t0, 3)

        # Anti-bot-style HTTP statuses: escalate to a browser engine
        if resp.status_code in _BLOCK_STATUSES:
            raise EngineFailedError(
                f"curl_cffi got HTTP {resp.status_code} — likely anti-bot block, escalate",
                engine=self.name,
                retriable_on_other_engine=True,
            )

        html = resp.text or ""
        title = ""
        m = _TITLE_RE.search(html)
        if m:
            title = m.group(1).strip()[:200]

        # Wrap the raw HTML in a single "element" so downstream code that
        # iterates result.elements still works. Tag it as 'html' (not a
        # real DOM node — caller checks attrs['x-engine'] to know).
        # bbox is intentionally null: we have no rendered geometry, so
        # honest is better than fabricating viewport-shaped coords.
        elements = [
            {
                "tag": "html",
                "text": html[:_BODY_TEXT_CAP],
                "css": "html",
                "xpath": "/html",
                "attrs": {
                    "x-engine": "curl_cffi",
                    "x-status": str(resp.status_code),
                    "x-final-url": str(resp.url),
                },
                "bbox": None,  # no rendered geometry — see module docstring
            }
        ]

        return EngineSnapshotResult(
            url=str(resp.url),
            title=title,
            screenshot_base64="",     # honest: no screenshot capability
            elements=elements,
            viewport={"width": 0, "height": 0},  # no rendering
            page={"width": 0, "height": 0},
            engine_name=self.name,
            elapsed_s=elapsed,
            cost_cents=self.cost_per_request_cents,
            proxy_used=self._proxy_label(proxy_url),
            cookies_carried=len(resp.cookies),
            notes=f"http {resp.status_code}, {len(html)} bytes",
        )

    @staticmethod
    def _proxy_label(proxy_url: Optional[str]) -> Optional[str]:
        """Credential-stripped proxy label for telemetry."""
        if not proxy_url:
            return None
        # Strip userinfo: http://user:pass@host:port → host:port
        return proxy_url.split("@", 1)[-1] if "@" in proxy_url else proxy_url

    @staticmethod
    def _maybe_proxy_url(vendor_hint: Optional[str] = None) -> Optional[str]:
        """Same proxy pool that browser engines use — share IPs so the
        target can't detect engine swap by IP change. Best-effort.

        Routes through residential when vendor_hint indicates an
        IP-reputation-sensitive vendor AND a residential plan is
        configured; otherwise falls back to the datacenter pool."""
        try:
            from app import proxies
            return proxies.pick_for_vendor(vendor_hint)
        except Exception:
            return None
