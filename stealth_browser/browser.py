"""StealthBrowser — thin opinionated wrapper around nodriver with stealth + proxy.

What this gives you over raw nodriver:

  1. **Stealth defaults.** Both Chromium-args and the JS init script from
     `stealth.py` are applied automatically. You don't need to remember
     to inject anything.

  2. **CDP-level proxy auth.** Setting `proxy_url=` plumbs the auth
     handler through Chromium's CDP — Chromium's `--proxy-server` flag
     by itself can't do basic-auth, leading to the famous "every request
     hangs forever" bug. We register `Fetch.authRequired` automatically.

  3. **Transient-retry decorator.** nodriver has well-known flake patterns
     (StopIteration in CDP cleanup, websocket dropped mid-handshake).
     `with_retry()` swallows the flake, restarts the browser (rotating
     to a new proxy if a pool is configured), and re-runs once.

  4. **Proxy rotation.** Pass `proxies=[...]` for a list of HTTP-proxy
     URLs and the pool rotates per browser restart. Useful for
     distributing load + dodging per-proxy rate limits.

  5. **Per-tab convenience.** `async with pool.tab(url) as tab:` opens,
     navigates, yields, closes — no manual lifecycle dance.

Non-goals:
  * Multi-browser pool. nodriver is single-browser by design (one
    Chromium subprocess). For parallelism, run multiple `StealthBrowser`
    instances in separate asyncio tasks.
  * In-flight proxy switching. Chromium can't change proxies after
    launch; you'd need to restart the browser. The `with_retry`
    helper does this for you on failure.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import nodriver as uc
from nodriver import cdp

from .stealth import ULTRA_STEALTH_CHROMIUM_ARGS, ULTRA_STEALTH_JS


# nodriver's known-flaky exception patterns. Match by substring because
# nodriver wraps these in various ways (RuntimeError, asyncio.CancelledError,
# WSException, etc.) but the message is stable across versions.
_TRANSIENT_ERROR_MARKERS = (
    "StopIteration",
    "coroutine raised StopIteration",
    "Target crashed",
    "Connection closed",
    "connection closed",
    "websocket",
)


class StealthBrowser:
    """One Chromium subprocess, optionally fronted by a rotating proxy list.

    Lifecycle:
        pool = StealthBrowser(proxies=[...])
        await pool.start()
        async with pool.tab(url) as tab:
            ...
        await pool.stop()

    Or use `with_retry()` to wrap an operation in the transient-retry
    decorator (recommended for production):

        result = await pool.with_retry(lambda: do_thing(pool))
    """

    def __init__(
        self,
        *,
        proxy_url: Optional[str] = None,
        proxies: Optional[list[str]] = None,
        headless: bool = True,
        extra_chromium_args: Optional[list[str]] = None,
    ) -> None:
        """Construct a pool.

        Args:
            proxy_url: Single proxy URL like `http://user:pass@host:port`.
                Convenience for the no-rotation case.
            proxies: List of proxy URLs to rotate across on each browser
                restart. If both `proxy_url` and `proxies` are passed,
                they're merged (single URL prepended to the list).
            headless: True (default) runs `--headless=new`. Set False for
                local debugging.
            extra_chromium_args: Optional list appended to the default
                stealth args. Use for site-specific tweaks.
        """
        self._proxies: list[str] = list(proxies or [])
        if proxy_url and proxy_url not in self._proxies:
            self._proxies.insert(0, proxy_url)
        self._headless = headless
        self._extra_args = extra_chromium_args or []
        self._browser: Optional[uc.Browser] = None
        self._current_proxy: Optional[str] = None

    # ── Public lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch Chromium with stealth + (optional) proxy. Safe to call
        when already started — returns immediately if browser is alive."""
        if self._browser is not None:
            return
        proxy = self._pick_proxy()
        args = list(ULTRA_STEALTH_CHROMIUM_ARGS) + self._extra_args
        if not self._headless:
            # Strip headless flags for debugging mode
            args = [a for a in args if not a.startswith("--headless")]
        if proxy:
            host, port, user, password = _parse_proxy(proxy)
            args.append(f"--proxy-server=http://{host}:{port}")
            args.append("--proxy-bypass-list=<-loopback>;127.0.0.1;localhost")
            self._current_proxy = proxy
        else:
            self._current_proxy = None
        self._browser = await uc.start(browser_args=args)
        # Apply our JS init script BEFORE any navigation happens.
        # This runs in every frame, every navigation, in document_start.
        await self._browser.send(
            cdp.page.add_script_to_evaluate_on_new_document(ULTRA_STEALTH_JS)
        )
        if proxy:
            host, port, user, password = _parse_proxy(proxy)
            await self._setup_proxy_auth(user, password)

    async def stop(self) -> None:
        """Shut down the browser subprocess."""
        try:
            if self._browser is not None:
                result = self._browser.stop()
                if hasattr(result, "__await__"):
                    await result
        finally:
            self._browser = None
            self._current_proxy = None

    async def restart(self, *, rotate_proxy: bool = True) -> None:
        """Hard reset — called after we see a transient CDP error.

        `rotate_proxy=True` (default) picks a new proxy from the pool, which
        is half the point of having a pool. Pass `False` to retry with the
        same proxy (useful when you've confirmed the error wasn't IP-related).
        """
        await self.stop()
        await self.start() if rotate_proxy else None
        if not rotate_proxy and self._current_proxy is not None:
            # Re-start with the same proxy. Easiest: pin it to head of list.
            head = self._current_proxy
            self._proxies = [head] + [p for p in self._proxies if p != head]
            await self.start()

    # ── Convenience: per-tab context manager ────────────────────────────

    @asynccontextmanager
    async def tab(self, url: str) -> AsyncIterator[uc.Tab]:
        """Open a tab, navigate, yield, close. The common case.

            async with pool.tab(url) as tab:
                ...
        """
        if self._browser is None:
            await self.start()
        assert self._browser is not None
        tab = await self._browser.get(url)
        try:
            yield tab
        finally:
            try:
                await tab.close()
            except Exception:
                pass

    # ── Production helper: transient-retry decorator ────────────────────

    async def with_retry(self, op):
        """Run `op()` (a zero-arg async callable). If it raises one of
        the known nodriver transient errors, restart the browser
        (rotating proxy for fresh egress IP) and run once more.

        Non-transient errors propagate on first raise — we don't want to
        mask actual bugs by retrying everything."""
        try:
            return await op()
        except Exception as e:
            err_str = str(e)
            if not any(m in err_str for m in _TRANSIENT_ERROR_MARKERS):
                raise
            await self.restart(rotate_proxy=True)
            return await op()

    # ── Introspection ───────────────────────────────────────────────────

    @property
    def current_proxy(self) -> Optional[str]:
        """`host:port` of the current proxy, or None if running direct.
        Credentials are stripped — safe to log."""
        if not self._current_proxy:
            return None
        host, port, _u, _p = _parse_proxy(self._current_proxy)
        return f"{host}:{port}"

    # ── Internals ───────────────────────────────────────────────────────

    def _pick_proxy(self) -> Optional[str]:
        if not self._proxies:
            return None
        return random.choice(self._proxies)

    async def _setup_proxy_auth(self, user: str, password: str) -> None:
        """Register a CDP Fetch handler that auto-responds to proxy
        basic-auth challenges. Without this, Chromium pops a basic-auth
        dialog and the request hangs forever in headless mode."""
        if self._browser is None:
            return
        try:
            await self._browser.send(cdp.fetch.enable(handle_auth_requests=True))
        except Exception as e:
            print(f"[stealth-browser] cdp.fetch.enable failed: {e!r}")
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
                print(f"[stealth-browser] continue_with_auth failed: {e!r}")

        async def _on_paused(event) -> None:
            try:
                await self._browser.send(
                    cdp.fetch.continue_request(request_id=event.request_id)
                )
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
        except Exception as e:
            print(f"[stealth-browser] add_handler failed: {e!r}")


# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_proxy(url: str) -> tuple[str, int, str, str]:
    """`http://user:pass@host:port` → (host, port, user, password).

    Raises ValueError on malformed URL (better than silently passing weird
    args to Chromium and getting a cryptic crash 30s into the run)."""
    p = urlparse(url)
    if not p.hostname or not p.port:
        raise ValueError(f"proxy URL missing host/port: {url}")
    return p.hostname, p.port, p.username or "", p.password or ""
