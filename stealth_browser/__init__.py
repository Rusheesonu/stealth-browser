"""stealth-browser — undetected headless Chromium pool with proxy + CDP auth.

Public API:

    from stealth_browser import StealthBrowser, detect_block
    from stealth_browser.stealth import ULTRA_STEALTH_JS, ULTRA_STEALTH_CHROMIUM_ARGS

The lower-level modules (`stealth`, `detect`, `browser`) are also importable
directly when you only need a single piece (e.g. injecting `ULTRA_STEALTH_JS`
into your own Playwright stack).

Version policy: 0.x is unstable, 1.0 will lock the public API.
"""

from __future__ import annotations

from .stealth import ULTRA_STEALTH_JS, ULTRA_STEALTH_CHROMIUM_ARGS
from .detect import BlockDetection, detect_block

__version__ = "0.1.0"

__all__ = [
    # Headline exports — the 95% case
    "ULTRA_STEALTH_JS",
    "ULTRA_STEALTH_CHROMIUM_ARGS",
    "BlockDetection",
    "detect_block",
    # Pool is in browser.py — import on demand to avoid pulling nodriver
    # for users who only want the JS patches.
]


def __getattr__(name: str):
    # Lazy-load StealthBrowser so `pip install stealth-browser` with no
    # nodriver doesn't error on import. Only when you reach for the pool
    # do we need nodriver.
    if name == "StealthBrowser":
        from .browser import StealthBrowser
        return StealthBrowser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
