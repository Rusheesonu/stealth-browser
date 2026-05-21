"""URL → (screenshot + element catalog) via nodriver + stealth."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nodriver import cdp

from app.actions import run_actions, BrowserAction
from app.browser import pool, with_transient_retry
from app.extract_js import COLLECT_ELEMENTS_JS


@dataclass
class SnapshotResult:
    url: str
    title: str
    screenshot_base64: str
    viewport: dict[str, int]
    page: dict[str, int]
    elements: list[dict[str, Any]]


async def take_snapshot(
    url: str,
    *,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    actions: list[BrowserAction] | None = None,
    warmup: bool = False,
) -> SnapshotResult:
    """One-shot snapshot with a restart+retry on transient nodriver flakes.

    Optional actions run after navigation but before element collection —
    used to dismiss cookie banners, log in, scroll-trigger lazy content.

    warmup=False (DEFAULT, after iter 6 bench): the cookie-warmup approach
    was tested and caused MORE problems than it solved on the antibot
    bench — visiting site root first then immediately scraping a deep URL
    looked MORE suspicious to Akamai (macys.com regressed from PASS to
    45s timeout) and the warmup-tab cleanup is racy on nodriver 0.45+
    causing "No target with given id found" errors on the follow-up
    scrape (broke 2captcha demo). Disabled by default; opt-in via warmup=
    True when you've validated it helps a specific site. Future improvement:
    extract cf_clearance cookie post-warmup and replay via curl-impersonate
    instead of re-using the same browser session."""

    async def _once() -> SnapshotResult:
        if warmup:
            await _warmup_session(url)
        return await _snapshot_inner(url, viewport_width, viewport_height, actions)

    return await with_transient_retry(_once, label="snapshot")


# Per-process cache of (hostname → already-warmed) so a 100-URL crawl
# doesn't warm the same domain 100 times. Cleared on browser restart
# because the cookies are gone too.
_warmed_hosts: set[str] = set()


async def _warmup_session(target_url: str) -> None:
    """Visit the site root first to collect anti-bot session cookies.

    No-op if we've already warmed this host on the current browser
    instance. Best-effort: a failure to warm doesn't block the real
    snapshot — that already has its own retry."""
    from urllib.parse import urlparse
    try:
        u = urlparse(target_url)
    except Exception:
        return
    host = (u.hostname or "").lower()
    if not host or host in _warmed_hosts:
        return
    # Skip warmup for sites known not to need it (cheap heuristic — these
    # don't run anti-bot challenges at root so warming is wasted time).
    if host.endswith(("toscrape.com", "ycombinator.com", "httpbin.org", "example.com", "wikipedia.org")):
        _warmed_hosts.add(host)
        return

    root = f"{u.scheme}://{u.hostname}/"
    if target_url.rstrip("/") == root.rstrip("/"):
        # Target IS the root — no separate warmup needed; the main scrape
        # will collect cookies naturally.
        _warmed_hosts.add(host)
        return

    warmup_tab = None
    try:
        warmup_tab = await pool.open_tab(root)
        # Brief pause for any CF/DataDome challenge JS to execute and set
        # the clearance cookie. 2.5s is the sweet spot per testing —
        # under 2s misses some challenges, over 3s adds noticeable latency.
        await asyncio.sleep(2.5)
        _warmed_hosts.add(host)
        print(f"[warmup] warmed {host}")
    except Exception as e:
        # Don't poison the cache on error — let the next attempt retry.
        print(f"[warmup] {host} warmup failed (continuing anyway): {e!r}")
    finally:
        if warmup_tab is not None:
            try: await warmup_tab.close()
            except Exception: pass


def reset_warmup_cache() -> None:
    """Clear the warmed-hosts cache. Call after pool.restart() or when
    cookies are believed stale."""
    _warmed_hosts.clear()


async def _snapshot_inner(
    url: str,
    viewport_width: int,
    viewport_height: int,
    actions: list[BrowserAction] | None,
) -> SnapshotResult:
    """Order matters more than anything in this function.

    The hard lesson: any viewport resize after navigation fires a window
    `resize` event, which triggers re-layouts + lazy mount of things
    like Amazon's filter sidebar. That means bboxes and the screenshot
    end up in different layout states.

    The stable ordering that actually works:
      1. Set viewport ONCE before navigation.
      2. Navigate, wait for ready state.
      3. Force-eager all lazy images (rewrite loading=lazy → eager,
         hydrate data-src shims).
      4. Scroll through to trigger intersection-observer based loads.
      5. Scroll back to (0, 0) and wait for images + layout settle.
      6. COLLECT ELEMENTS FIRST — freezes the truth-of-DOM at scroll=0.
      7. THEN screenshot with capture_beyond_viewport=True. Even if
         this causes a brief layout shift during the capture, the
         bboxes are already frozen and the pixel-to-bbox mapping stays
         correct.
    """
    tab = await pool.open_tab("about:blank")
    try:
        # Set the viewport ONCE, before we navigate. We never touch it
        # again in this function — that's the whole point.
        try:
            await tab.send(cdp.emulation.set_device_metrics_override(
                width=viewport_width,
                height=viewport_height,
                device_scale_factor=1,
                mobile=False,
            ))
        except Exception:
            pass

        await tab.get(url)
        await _wait_ready(tab, timeout=8.0)
        await asyncio.sleep(0.5)

        # Run pre-snapshot actions (dismiss cookie banners, log in, etc).
        # Failures are logged but don't abort — best-effort.
        if actions:
            try:
                await run_actions(tab, actions)
                # Give the page a moment to settle after actions before we
                # start collecting elements / taking screenshots.
                await asyncio.sleep(0.4)
            except Exception as e:
                print(f"[snapshot] actions failed: {e!r}")

        # 3. Force-eager all lazy images BEFORE scrolling. That way when
        # intersection-observers fire during the scroll pass, the images
        # they reference are already downloading rather than waiting for
        # an observer hit. Belt-and-suspenders: also handle data-src,
        # data-srcset, and data-lazy-src shims common on old scripts.
        await tab.evaluate(
            r"""
            (() => {
                document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                    img.loading = 'eager';
                });
                document.querySelectorAll('img[data-src]').forEach(img => {
                    if (!img.src || img.src.startsWith('data:')) img.src = img.dataset.src;
                });
                document.querySelectorAll('img[data-srcset]').forEach(img => {
                    if (!img.srcset) img.srcset = img.dataset.srcset;
                });
                document.querySelectorAll('img[data-lazy-src]').forEach(img => {
                    if (!img.src) img.src = img.dataset.lazySrc;
                });
            })()
            """
        )

        # 4. Scroll through the full page to hit any observer-based
        # loaders that skip force-eager. Bounded — no infinite scroll.
        await _scroll_full_height(tab)

        # 5. Scroll back to origin and wait for the layout to settle.
        # Image decode is async; we wait for it explicitly here so our
        # bbox collection sees fully-rendered sizes.
        await tab.evaluate("window.scrollTo(0, 0)")
        await _wait_for_images(tab, timeout=4.0)
        # Poll until two consecutive samples of body scrollHeight agree —
        # catches the Amazon failure mode where a banner / filter sidebar
        # lazy-inserts content right around the 500ms mark and shoves
        # everything below it down ~70px. Without this, bbox collection
        # happens on the pre-insert layout and the screenshot ends up
        # capturing the post-insert layout, producing that "coming above
        # again" vertical offset. Cheap belt-and-suspenders; bounded.
        await _wait_for_stable_height(tab, timeout=3.0)
        await asyncio.sleep(0.3)

        # 6. EXPAND THE VIEWPORT to the full document height BEFORE
        # both bbox collection and screenshot.
        #
        # Previous version used capture_beyond_viewport=True at the
        # screenshot step, but that briefly resizes the viewport during
        # capture — which triggers layout shifts on pages with `vh`-sized
        # sections, sticky headers, or intersection-observer reveals
        # (Target, most modern e-commerce). The bbox data from step 5
        # then references the pre-shift layout while the screenshot
        # captures the post-shift layout, producing the visible offset
        # where hover overlays appear above/below the actual element.
        #
        # Fix: do the expansion FIRST, settle the layout, then collect
        # bboxes + screenshot at the same expanded viewport. Everything
        # is captured in identical layout state, alignment is exact.
        #
        # Clamp to 24000px height — anything taller and we accept the
        # small alignment risk via capture_beyond_viewport rather than
        # OOM the renderer.
        try:
            raw_height = await tab.evaluate("document.documentElement.scrollHeight")
            if isinstance(raw_height, tuple):
                raw_height = raw_height[0]
            page_height = max(int(raw_height or viewport_height), viewport_height)
        except Exception:
            page_height = viewport_height
        clamped_height = min(page_height, 24000)
        needs_beyond_viewport = page_height > clamped_height

        try:
            await tab.send(cdp.emulation.set_device_metrics_override(
                width=viewport_width,
                height=clamped_height,
                device_scale_factor=1,
                mobile=False,
            ))
            # Let the page settle at the new viewport. Intersection
            # observers that fire newly-visible will run their handlers
            # in this window; the second _wait_for_stable_height covers
            # the rare case where they shove content around.
            await asyncio.sleep(0.4)
            await _wait_for_stable_height(tab, timeout=1.5)
        except Exception:
            # Renderer didn't accept the resize (probably an OOM-style
            # rejection on huge pages) — fall back to the old strategy:
            # bbox first then screenshot with capture_beyond_viewport.
            needs_beyond_viewport = True

        # 7. Collect elements at the expanded viewport.
        data = await _evaluate_json(tab, COLLECT_ELEMENTS_JS)

        # 8. Screenshot at the SAME expanded viewport. Only fall back to
        # capture_beyond_viewport when the page was taller than our
        # height clamp (rare; only mega-scroll pages).
        shot = await tab.send(cdp.page.capture_screenshot(
            format_="png",
            capture_beyond_viewport=needs_beyond_viewport,
        ))
        screenshot_b64 = shot if isinstance(shot, str) else str(shot)

        print(
            f"[snapshot] {url} → {len(data.get('elements', []))} elements "
            f"(page {data.get('page', {}).get('width')}×{data.get('page', {}).get('height')})"
        )

        return SnapshotResult(
            url=data.get("url", url),
            title=data.get("title", ""),
            screenshot_base64=screenshot_b64,
            viewport=data.get("viewport", {"width": viewport_width, "height": viewport_height}),
            page=data.get("page", {"width": viewport_width, "height": viewport_height}),
            elements=data.get("elements", []),
        )
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def _wait_for_images(tab, timeout: float) -> None:
    """Wait until every <img> on the page has either loaded or failed.
    Bounded — a broken CDN shouldn't hang our snapshot forever."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            done = await tab.evaluate(
                "Array.from(document.images).every(img => img.complete)"
            )
            if isinstance(done, tuple):
                done = done[0]
            if done:
                return
        except Exception:
            pass
        await asyncio.sleep(0.25)


async def _wait_for_stable_height(tab, timeout: float, samples: int = 3) -> None:
    """Poll document.body.scrollHeight until it stops changing. Returns
    as soon as `samples` consecutive polls agree. Bounded."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: int | None = None
    streak = 0
    while asyncio.get_event_loop().time() < deadline:
        try:
            h = await tab.evaluate("document.documentElement.scrollHeight")
            if isinstance(h, tuple):
                h = h[0]
            h = int(h)
        except Exception:
            h = None
        if h is not None and h == last:
            streak += 1
            if streak >= samples:
                return
        else:
            streak = 1
            last = h
        await asyncio.sleep(0.2)


async def _evaluate_json(tab, expression: str) -> dict:
    """Run JS via CDP Runtime.evaluate with return_by_value=True so we
    always get a dict back (not a RemoteObject handle). Logs and
    returns an empty shape if the eval threw in-page."""
    try:
        result = await tab.send(cdp.runtime.evaluate(
            expression=expression,
            return_by_value=True,
            await_promise=False,
            allow_unsafe_eval_blocked_by_csp=True,
        ))
    except TypeError:
        # Older nodriver builds don't accept allow_unsafe_eval_blocked_by_csp.
        result = await tab.send(cdp.runtime.evaluate(
            expression=expression,
            return_by_value=True,
            await_promise=False,
        ))

    # CDP Runtime.evaluate returns a (RemoteObject, ExceptionDetails) tuple.
    remote, exc = (result if isinstance(result, tuple) else (result, None))

    if exc is not None:
        text = getattr(exc, "text", None) or getattr(exc, "exception", None)
        print(f"[snapshot] in-page eval raised: {text!r}")
        return {"elements": [], "viewport": {}, "page": {}}

    value = getattr(remote, "value", None)
    if value is None:
        print("[snapshot] CDP returned no value (type may not be serializable)")
        return {"elements": [], "viewport": {}, "page": {}}
    if not isinstance(value, dict):
        print(f"[snapshot] CDP returned {type(value).__name__} instead of dict")
        return {"elements": [], "viewport": {}, "page": {}}
    return value


async def _wait_ready(tab, timeout: float) -> None:
    """Poll document.readyState until 'complete' or timeout. nodriver
    has no generic wait_for_load helper — we roll our own so a dead
    page never traps us past the timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            state = await tab.evaluate("document.readyState")
            if isinstance(state, tuple):
                state = state[0]
            if state == "complete":
                return
        except Exception:
            pass
        await asyncio.sleep(0.15)


async def _scroll_full_height(tab) -> None:
    """Scroll through 8 viewport heights max, triggering lazy images
    without trapping on infinite scroll."""
    await tab.evaluate(
        r"""
        (async () => {
            const step = window.innerHeight * 0.9;
            const max = window.innerHeight * 8;
            for (let y = 0; y < max; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 150));
                if (y + step >= document.documentElement.scrollHeight) break;
            }
        })()
        """
    )
