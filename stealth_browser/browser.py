"""Shared nodriver browser pool — stealth + (optionally) residential proxy.

Why nodriver instead of Playwright — nodriver patches Chromium at the
flag/CDP level to close automation leaks that Playwright+stealth-JS
can't reach. Clears soft Cloudflare challenges and Turnstile invisible
mode out of the box, even without proxies. For harder targets we layer
a residential proxy via Chromium's --proxy-server flag.

Lifecycle: single nodriver Browser, lazy-init on first request, kept
hot for the process lifetime. Per-request we open a fresh Tab, inject
stealth via CDP `addScriptToEvaluateOnNewDocument` so it runs before
any page JS on every nav, then navigate + screenshot + close tab.

Proxies: if `backend/data/proxies.json` is populated, the pool picks a
random proxy at browser-start time. Auth challenges are auto-answered
via CDP `Fetch.authRequired` so Chromium doesn't hang on the basic-auth
dialog. On restart (transient error path) we rotate to a new proxy.

Resilience: nodriver has well-known transient errors (StopIteration in
its CDP cleanup coroutine, target crashed, websocket closed) that
happen on healthy browsers under timing races. We detect by error
string and restart+retry once.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import nodriver as uc
from nodriver import cdp

from app import proxies
from app.stealth import ULTRA_STEALTH_CHROMIUM_ARGS, ULTRA_STEALTH_JS


_TRANSIENT_ERROR_MARKERS = (
    "StopIteration",
    "coroutine raised StopIteration",
    "Target crashed",
    "Connection closed",
    "connection closed",
    "websocket",
)


def is_transient_nodriver_error(exc: BaseException | str) -> bool:
    """Matches the nodriver flakes that clear on a browser restart."""
    msg = str(exc) if not isinstance(exc, str) else exc
    if not msg:
        return False
    return any(m in msg for m in _TRANSIENT_ERROR_MARKERS)


# (host, port, user, password) — what we hand to Chromium + CDP auth handler.
ProxyTuple = tuple[str, int, str, str]


class BrowserPool:
    def __init__(self) -> None:
        self._browser: Optional[uc.Browser] = None
        # nodriver is not safe under concurrent CDP traffic on a single
        # browser — serialize tab work through one lock.
        self._lock = asyncio.Lock()
        self._current_proxy: Optional[ProxyTuple] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self, proxy: Optional[ProxyTuple] = None) -> None:
        if self._browser is not None:
            return

        args = list(ULTRA_STEALTH_CHROMIUM_ARGS)
        if proxy:
            host, port, user, password = proxy
            args.append(f"--proxy-server=http://{host}:{port}")
            # We don't want the proxy to intercept localhost / about:blank
            # which would 502 on tab init.
            args.append("--proxy-bypass-list=<-loopback>;127.0.0.1;localhost")
            self._current_proxy = proxy
            print(f"[browser] starting with proxy {host}:{port}")
        else:
            self._current_proxy = None
            print("[browser] starting without proxy (direct)")

        self._browser = await uc.start(browser_args=args)

        # Proxy auth — set up the CDP handler immediately after browser is up
        # so the very first nav already has it ready.
        if proxy:
            await self._setup_proxy_auth(proxy[2], proxy[3])

    async def stop(self) -> None:
        try:
            if self._browser is not None:
                result = self._browser.stop()
                if hasattr(result, "__await__"):
                    await result
        finally:
            self._browser = None
            self._current_proxy = None

    async def restart(self, *, rotate_proxy: bool = True) -> None:
        """Hard reset — called after we see a transient CDP error. Any
        in-flight tabs are toast; caller must re-open.

        rotate_proxy=True picks a new proxy from the pool (default — gives us
        a fresh egress IP each restart, which is half the point of having
        a proxy pool). Pass False to keep the same proxy if you're sure the
        error wasn't IP-related."""
        await self.stop()
        new_proxy: Optional[ProxyTuple] = None
        if rotate_proxy and proxies.available():
            new_proxy = proxies.host_port_user_pass()
        await self.start(proxy=new_proxy)

    # ── health / liveness ─────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._browser is not None

    def current_proxy_label(self) -> Optional[str]:
        """`host:port` of the current proxy, or None if direct. Safe to expose
        from /health — credentials are not included."""
        if not self._current_proxy:
            return None
        host, port, *_ = self._current_proxy
        return f"{host}:{port}"

    async def _ensure_live(self) -> None:
        """Bounded probe against about:blank. If the browser handle is
        a zombie (OOM, crash, websocket dropped) we tear it down so the
        next caller does a clean restart."""
        if self._browser is None:
            return
        try:
            probe = await asyncio.wait_for(
                self._browser.get("about:blank"), timeout=5.0
            )
            try:
                await probe.close()
            except Exception:
                pass
        except Exception:
            try:
                await self.stop()
            except Exception:
                pass
            self._browser = None

    # ── tab open w/ stealth init ──────────────────────────────────────────

    async def open_tab(self, url: str = "about:blank") -> uc.Tab:
        """Open a tab with stealth JS pre-installed. Caller must close it.

        Lazy-starts the browser on first call. If a proxy pool is configured,
        the first start picks a random proxy; subsequent restarts rotate.
        """
        async with self._lock:
            await self._ensure_live()
            if self._browser is None:
                # First request — pick a proxy if pool is configured, else go direct.
                proxy = proxies.host_port_user_pass() if proxies.available() else None
                await self.start(proxy=proxy)
            assert self._browser is not None
            tab = await self._browser.get(url)

        # Register stealth JS to run before any page script on every
        # navigation. CDP method: Page.addScriptToEvaluateOnNewDocument.
        try:
            await tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=ULTRA_STEALTH_JS)
            )
        except Exception:
            # Fallback: inject once on the current document. Most basic
            # detectors check fingerprint bits on first script eval, so
            # this still clears the soft checks even without the init hook.
            try:
                await tab.evaluate(ULTRA_STEALTH_JS)
            except Exception:
                pass
        return tab

    # ── proxy auth handler (CDP Fetch.AuthRequired) ───────────────────────

    async def _setup_proxy_auth(self, user: str, password: str) -> None:
        """Register a CDP Fetch handler that auto-responds to proxy auth
        challenges. Without this, Chromium pops a basic-auth dialog and
        the request hangs forever in headless mode.

        nodriver 0.50.x API: the Browser object IS the CDP transport —
        use `browser.send(cdp_cmd)` and `browser.add_handler(EventType,
        callback)` directly. Earlier nodriver releases exposed a separate
        `.connection` attribute; we used to look that up via getattr and
        bail when it was None, which silently disabled all proxy auth on
        modern nodriver. Hence the long-running "proxy logs say 'starting
        with proxy X' but every scrape times out" bug. Fixed 2026-05-21.

        Best-effort: if any of the CDP calls fail (unsupported API,
        timing race, etc.), we log + fall through. The proxy will still
        work for unauthenticated targets but will hang on
        basic-auth-required ones (which is most of them)."""
        if self._browser is None:
            return

        try:
            # Enable fetch interception for auth challenges only (not every
            # request — that would tank performance).
            await self._browser.send(cdp.fetch.enable(handle_auth_requests=True))
        except Exception as e:
            print(f"[browser] cdp.fetch.enable failed: {e!r} — proxy auth NOT wired")
            return

        async def _on_auth(event) -> None:
            try:
                await self._browser.send(
                    cdp.fetch.continue_with_auth(
                        request_id=event.request_id,
                        auth_challenge_response=cdp.fetch.AuthChallengeResponse(
                            response="ProvideCredentials",
                            username=user,
                            password=password,
                        ),
                    )
                )
            except Exception as e:
                print(f"[browser] continue_with_auth failed: {e!r}")

        async def _on_paused(event) -> None:
            # We enabled auth-only interception, but defensively handle any
            # RequestPaused that slips through so the request doesn't stall.
            try:
                await self._browser.send(cdp.fetch.continue_request(request_id=event.request_id))
            except Exception:
                pass

        try:
            self._browser.add_handler(
                cdp.fetch.AuthRequired,
                lambda evt: asyncio.create_task(_on_auth(evt)),
            )
            self._browser.add_handler(
                cdp.fetch.RequestPaused,
                lambda evt: asyncio.create_task(_on_paused(evt)),
            )
            print(f"[browser] proxy auth handler registered ({user}@{self.current_proxy_label()})")
        except Exception as e:
            print(f"[browser] add_handler failed: {e!r} — proxy auth NOT wired")


pool = BrowserPool()


# ── transient-retry decorator ─────────────────────────────────────────────

async def with_transient_retry(op, *, label: str = "op", max_retries: int = 3):
    """Run `op()` (a zero-arg async callable). On any transient nodriver
    flake (websocket drop, StopIteration in CDP cleanup, Target crashed,
    'InvalidStatus: server rejected WebSocket connection: HTTP 500' which
    is the failure-to-bind-CDP-port-during-Chromium-boot case), restart
    the browser and retry up to `max_retries` times with linear backoff.

    Bench (2026-05-21) showed that single-retry was insufficient: on a
    local 12-URL antibot run, 3/8 protected URLs failed with the WebSocket
    500 error. The same flake usually clears within 1-3s when Chromium
    fully releases the port. With max_retries=3 + 2s backoff, those 3
    failures should drop to ~0.

    Non-transient errors (timeouts, real navigation failures, anything
    that's not in _TRANSIENT_ERROR_MARKERS) propagate on first raise —
    we don't want to mask real bugs by retrying everything."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):   # initial + N retries
        try:
            return await op()
        except Exception as e:
            if not is_transient_nodriver_error(e):
                raise
            last_err = e
            if attempt >= max_retries:
                # Out of retries — surface the final error
                print(f"[{label}] transient nodriver error persisted after {max_retries + 1} attempts: {e!r}")
                raise
            backoff = 2.0 * (attempt + 1)    # 2s, 4s, 6s — linear, not exponential
            print(f"[{label}] transient flake (attempt {attempt + 1}/{max_retries + 1}): {e!r} — restart+rotate, retry in {backoff}s")
            try:
                await pool.restart(rotate_proxy=True)
                await asyncio.sleep(backoff)
            except Exception as restart_err:
                # If restart itself flakes, log and continue — the next
                # op() call will attempt its own lazy start.
                print(f"[{label}] restart also flaked: {restart_err!r} — falling through to retry")
                await asyncio.sleep(backoff)
    # Unreachable — the loop either returns or raises — but mypy wants it.
    if last_err:
        raise last_err
    raise RuntimeError(f"[{label}] with_transient_retry exited loop with no result")
