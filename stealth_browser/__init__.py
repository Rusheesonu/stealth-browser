"""stealth-browser — multi-engine scraping with anti-bot routing.

Public API (high level — the 95% case):

    from stealth_browser.engines import router, Requirements
    snap, decision = await router.snapshot(url, requirements=Requirements(...))

Low-level pieces (independently usable):

    from stealth_browser import StealthBrowser, detect_block
    from stealth_browser.stealth import ULTRA_STEALTH_JS, ULTRA_STEALTH_CHROMIUM_ARGS

Version policy: 0.x is unstable, 1.0 will lock the public API.
"""

from __future__ import annotations

from .stealth import ULTRA_STEALTH_JS, ULTRA_STEALTH_CHROMIUM_ARGS
from .detect import BlockDetection, detect_block

__version__ = "0.2.0"

__all__ = [
    # Headline exports — the 95% case
    "ULTRA_STEALTH_JS",
    "ULTRA_STEALTH_CHROMIUM_ARGS",
    "BlockDetection",
    "detect_block",
    "StealthBrowser",
    # Pool is in browser.py — import on demand to avoid pulling nodriver
    # for users who only want the JS patches.
]


def __getattr__(name: str):
    # Lazy-load the browser pool so just importing the package doesn't
    # cost a nodriver import (useful for users who only need the JS
    # patches or the detect_block helper).
    if name == "StealthBrowser":
        # Back-compat alias for README / example code: the underlying
        # class is `BrowserPool` in browser.py. Both names resolve to
        # the same class so user code that uses either keeps working.
        from .browser import BrowserPool
        return BrowserPool
    if name == "BrowserPool":
        from .browser import BrowserPool
        return BrowserPool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
