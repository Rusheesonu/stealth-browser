"""Smoke benchmark — runs stealth-browser against a list of known anti-bot
detectors and reports pass/fail.

A page is considered "pass" if it loads in <30s and its body contains none
of the well-known challenge phrases. This is a heuristic, not a perfect
signal — eyeball the screenshots in `out/` for ground truth.

Run:
    python benchmarks/run.py

Output:
    Per-target pass/fail, plus a summary table.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from stealth_browser import StealthBrowser


# Targets we benchmark against. The string after the URL is the test bucket
# — soft (no proxy needed), hard (proxy recommended), captcha (visible CAPTCHA
# expected, "pass" means the page rendered the challenge UI without crashing).
TARGETS: list[tuple[str, str]] = [
    ("https://nowsecure.nl",                              "soft"),
    ("https://bot.sannysoft.com",                         "soft"),
    ("https://browserleaks.com/javascript",               "soft"),
    ("https://arh.antoinevastel.com/bots/areyouheadless", "soft"),
    ("https://abrahamjuliot.github.io/creepjs/",          "soft"),
    # Add your harder targets here. Cloudflare-protected sites, Datadome
    # endpoints etc. They require fresh residential IPs to score reliably.
]


CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "captcha",
    "access denied",
    "blocked by",
    "are you a robot",
    "verification required",
)


async def test_target(browser: StealthBrowser, url: str) -> tuple[bool, str, float]:
    """Returns (pass, reason, elapsed_seconds)."""
    t0 = time.perf_counter()
    try:
        tab = await asyncio.wait_for(browser.get(url), timeout=30.0)
    except asyncio.TimeoutError:
        return (False, "timeout (30s)", time.perf_counter() - t0)
    except Exception as e:
        return (False, f"navigation error: {e!r}", time.perf_counter() - t0)

    try:
        body = await tab.evaluate("document.body.innerText || ''")
        body_lower = str(body).lower()
        for phrase in CHALLENGE_PHRASES:
            if phrase in body_lower:
                return (False, f"challenge phrase: {phrase!r}", time.perf_counter() - t0)
        return (True, "ok", time.perf_counter() - t0)
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def main() -> None:
    Path("out").mkdir(exist_ok=True)
    results: list[tuple[str, str, bool, str, float]] = []

    async with StealthBrowser() as browser:
        for url, bucket in TARGETS:
            print(f"  → {url}", end="  ", flush=True)
            ok, reason, elapsed = await test_target(browser, url)
            mark = "✅" if ok else "❌"
            print(f"{mark} ({elapsed:.1f}s) {reason}")
            results.append((url, bucket, ok, reason, elapsed))

    # Summary
    print("\n" + "=" * 78)
    passes = sum(1 for r in results if r[2])
    print(f"{passes}/{len(results)} passed")
    print("=" * 78)
    for url, bucket, ok, reason, elapsed in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark}  [{bucket:6}]  {elapsed:5.1f}s  {url}")


if __name__ == "__main__":
    asyncio.run(main())
