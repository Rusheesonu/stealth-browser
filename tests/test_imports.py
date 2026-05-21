"""Smoke tests — would have caught the from-app import bug in 10 seconds."""


def test_top_level_import():
    import stealth_browser
    assert stealth_browser.__version__ == "0.2.0"


def test_engines_import():
    from stealth_browser.engines import router, Requirements  # noqa: F401
    from stealth_browser.engines.base import (  # noqa: F401
        Engine,
        Capability,
        EngineSnapshotResult,
        EngineFailedError,
    )
    # Router must have engines registered at import time
    assert len(router._engines) >= 1
    names = {e.name for e in router._engines}
    # nodriver always available (it's a core dep, no extras needed)
    assert "nodriver" in names


def test_detect_block_signatures():
    from stealth_browser.detect import detect_block
    # Cloudflare wall
    r = detect_block(title="Just a moment...", html="<html>cf-browser-verification</html>")
    assert r.blocked and r.vendor == "cloudflare"
    # Clean
    r = detect_block(title="Hello World", html="<html><body>welcome</body></html>")
    assert not r.blocked


def test_stealth_browser_alias():
    """Backward-compat alias from README/examples."""
    from stealth_browser import StealthBrowser
    assert StealthBrowser is not None
