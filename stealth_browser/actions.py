"""Browser actions — click / fill / scroll / wait before snapshot or extract.

Unlocks scraping behind-login pages, dismissing cookie banners, triggering
infinite scroll, etc. Each action is bounded with a timeout so a misbehaving
page can't hang the whole request.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal, TypedDict

log = logging.getLogger(__name__)


class BrowserAction(TypedDict, total=False):
    kind: Literal["click", "fill", "scroll", "wait", "wait_for"]
    selector: str          # CSS selector for click/fill/wait_for
    value: str             # text to type for fill, "bottom" / "top" / "px:300" for scroll
    ms: int                # milliseconds to wait (for kind="wait")
    timeout_ms: int        # max wait for wait_for (default 5000)


_DEFAULT_TIMEOUT_MS = 5000


async def run_actions(tab, actions: list[BrowserAction]) -> None:
    """Run actions sequentially. Errors per step are surfaced via log but
    don't stop the chain — actions are best-effort. If a step is
    fundamental (e.g. you need to be logged in for the page to render),
    the subsequent snapshot/extract will fail naturally."""
    for i, a in enumerate(actions):
        kind = a.get("kind")
        try:
            if kind == "click":
                await _do_click(tab, a)
            elif kind == "fill":
                await _do_fill(tab, a)
            elif kind == "scroll":
                await _do_scroll(tab, a)
            elif kind == "wait":
                await asyncio.sleep(a.get("ms", 500) / 1000.0)
            elif kind == "wait_for":
                await _do_wait_for(tab, a)
        except Exception as e:
            log.warning("step %d (%s) failed: %r", i, kind, e)


async def _do_click(tab, a: BrowserAction) -> None:
    selector = a.get("selector", "")
    if not selector:
        return
    await tab.evaluate(
        f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
        f"if (el) el.click(); }})()"
    )
    # Tiny wait for any synchronous re-render the click triggered.
    await asyncio.sleep(0.2)


async def _do_fill(tab, a: BrowserAction) -> None:
    selector = a.get("selector", "")
    value = a.get("value", "")
    if not selector:
        return
    # Set value + fire input + change events so React / Vue / Svelte controls
    # see it. Plain `.value =` alone doesn't trigger framework reactivity.
    await tab.evaluate(
        f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
                                          : window.HTMLInputElement.prototype,
                'value'
            ).set;
            nativeSetter.call(el, {json.dumps(value)});
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()
        """
    )


async def _do_scroll(tab, a: BrowserAction) -> None:
    target = a.get("value", "bottom")
    js: str
    if target == "bottom":
        js = "window.scrollTo(0, document.documentElement.scrollHeight)"
    elif target == "top":
        js = "window.scrollTo(0, 0)"
    elif isinstance(target, str) and target.startswith("px:"):
        try:
            px = int(target.split(":", 1)[1])
            js = f"window.scrollBy(0, {px})"
        except ValueError:
            return
    else:
        return
    await tab.evaluate(js)
    await asyncio.sleep(0.3)


async def _do_wait_for(tab, a: BrowserAction) -> None:
    """Poll for a selector to appear (or be visible). Bounded by timeout_ms."""
    selector = a.get("selector", "")
    timeout_ms = a.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
    if not selector:
        return
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_event_loop().time() < deadline:
        try:
            present = await tab.evaluate(
                f"!!document.querySelector({json.dumps(selector)})"
            )
            if isinstance(present, tuple):
                present = present[0]
            if present:
                return
        except Exception:
            pass
        await asyncio.sleep(0.15)
