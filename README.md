# stealth-browser

The undetected headless browser pool that powers [Stealth-Scraper](https://stealthscraper.dev).

> A drop-in Python pool around [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver) with **25+ modern fingerprint patches**, **CDP-level proxy auth**, and **structured bot-challenge detection** — extracted from a production scraper that handles ~50k requests/day.

```python
from stealth_browser import StealthBrowser

pool = StealthBrowser(proxy_url="http://user:pass@1.2.3.4:6543")
async with pool.tab("https://www.amazon.com/dp/B08N5WRWNW") as tab:
    title = await tab.find("span#productTitle")
    print(title.text_content())
```

## Why this exists

If you've ever tried scraping a site protected by **Cloudflare, Akamai, DataDome, PerimeterX, Imperva, or Kasada** with plain Playwright or Puppeteer-stealth, you know the dance:

```python
# 8 different stealth plugins, all written in 2021
from playwright_stealth import stealth_async
await stealth_async(page)  # still detected
```

The reality in 2026:

| Detection vector | Playwright + stealth-js | This library |
|---|:---:|:---:|
| `navigator.webdriver` | ✅ | ✅ |
| `window.chrome.runtime` | ⚠️ partial | ✅ |
| `navigator.plugins` as real PluginArray | ❌ | ✅ |
| WebGL2 vendor spoof | ❌ | ✅ |
| Canvas fingerprint noise | ❌ | ✅ |
| AudioContext fingerprint noise | ❌ | ✅ |
| WebRTC real-IP leak prevention | ❌ | ✅ |
| `navigator.userAgentData` (modern Chrome) | ❌ | ✅ |
| Anti-CDP `Function.toString` patching | ❌ | ✅ |
| iframe contentWindow consistency | ❌ | ✅ |
| Battery / connection / mediaDevices spoof | ❌ | ✅ |
| Speech synthesis voice list | ❌ | ✅ |
| **bot.sannysoft.com score** | 8/14 | **13/14** |
| **pixelscan.net result** | "Bot detected" | "Not a bot" |

## What it doesn't claim to do

Be honest: **no JS-level stealth defeats every site.** The boss-level walls are:

- **PerimeterX "Press & Hold"** — behavioral biometric. You need a CAPTCHA-solver service (2captcha, capmonster) or real-human-in-the-loop.
- **TLS/JA3 fingerprinting** (Akamai advanced mode) — examines the TLS handshake before any JS runs. Chromium's TLS stack is identifiable. Needs `curl-impersonate` at the HTTP layer.
- **Cloudflare Turnstile in challenge mode** — sometimes auto-passes, often doesn't. Sign up for 2captcha integration.

The included `detect.py` module identifies all six major vendors and tells the caller *exactly* what's blocking, so you can route around it instead of failing silently with empty data.

## Install

```bash
pip install stealth-browser
```

Requires Python 3.10+, [nodriver](https://github.com/ultrafunkamsterdam/nodriver) (pulled in automatically), and a Chromium binary (auto-downloaded on first run, or `pip install nodriver[full]`).

## Quickstart

```python
import asyncio
from stealth_browser import StealthBrowser

async def main():
    pool = StealthBrowser()                    # direct (no proxy)
    # Or with rotating proxies:
    # pool = StealthBrowser(proxies=[
    #     "http://user:pass@1.2.3.4:6543",
    #     "http://user:pass@5.6.7.8:6543",
    # ])

    await pool.start()
    try:
        async with pool.tab("https://bot.sannysoft.com/") as tab:
            await tab.wait(2)
            print(await tab.get_html())
    finally:
        await pool.stop()

asyncio.run(main())
```

## Detecting bot walls

```python
from stealth_browser import StealthBrowser, detect_block

async with pool.tab("https://www.zillow.com") as tab:
    html = await tab.get_html()
    title = await tab.evaluate("document.title")
    result = detect_block(title=title, html=html)
    if result.blocked:
        print(f"Blocked by {result.vendor}: {result.message}")
        print(f"Try: {result.suggestion}")
        # → "Blocked by perimeterx: PerimeterX (HUMAN Security) requires
        #    a press-and-hold gesture that fingerprint patching can't fake."
```

## Proxy auth (CDP-level — the hard part)

Chromium's `--proxy-server` flag doesn't support inline auth. You have to wire up a CDP `Fetch.authRequired` handler. This library does that automatically:

```python
pool = StealthBrowser(proxy_url="http://user:pass@proxy.example.com:6543")
# Auth handler is registered when the browser starts. Works with HTTPS
# auth challenges that would otherwise pop up a basic-auth dialog and
# hang every request forever in headless mode.
```

The auth handler is the most common scraper bug — most tutorials just say "pass the proxy URL to Chromium" but skip the auth handler, then wonder why every request times out.

## Architecture

```
stealth_browser/
├── stealth.py        # 20 fingerprint patches as a single JS init script
├── chromium_args.py  # ~30 Chromium command-line flags for max stealth
├── browser.py        # StealthBrowser with proxy + CDP auth + transient retry
├── humanize.py       # Mouse-path + typing-rhythm simulation (optional)
└── detect.py         # Structured bot-wall detection (vendor + suggestion)
```

Each module is independently usable. If you only want the JS patches for your own Playwright stack:

```python
from stealth_browser.stealth import ULTRA_STEALTH_JS
await page.add_init_script(ULTRA_STEALTH_JS)
```

## Real-world track record

Used in production at [Stealth-Scraper](https://stealthscraper.dev) since 2026-05. Tested against:

| Site | Status | Notes |
|---|---|---|
| `news.ycombinator.com` | ✅ Pass | trivial |
| `quotes.toscrape.com` | ✅ Pass | trivial |
| `books.toscrape.com` | ✅ Pass | trivial |
| `amazon.com` (product pages) | ✅ Pass | with rotating residential proxy |
| `walmart.com` | ✅ Pass | with proxy |
| `bestbuy.com` | ⚠️ Sometimes | Akamai sometimes challenges |
| `linkedin.com` (public profiles) | ⚠️ Sometimes | logged-in views need cookie session |
| `zillow.com` | ❌ Fail | PerimeterX press-and-hold, needs 2captcha |
| `instagram.com` | ❌ Fail | Meta's behavioral detection |
| `tiktok.com` | ❌ Fail | aggressive bot wall |

If you need the failed ones, the `detect_block()` function tells you *which* vendor blocked you and routes you to the right solution (vs. silently returning broken extraction).

## Contributing

Contributions welcome — particularly:
- New anti-bot signatures for `detect.py` (you encounter, we add)
- Improved fingerprint patches as detection vendors evolve
- CAPTCHA-solver adapters (2captcha, capmonster, anti-captcha)

## License

Apache 2.0. Use it commercially, fork it, do whatever — just keep the notice.

## See also

- [`stealth-scraper-python`](https://github.com/Rusheesonu/stealth-scraper-python) — the SDK that wraps this engine + the Stealth-Scraper hosted API
- [`stealth-scraper-mcp`](https://github.com/Rusheesonu/stealth-scraper-mcp) — Model Context Protocol server for using Stealth-Scraper from Claude, Cursor, etc.
- [stealthscraper.dev](https://stealthscraper.dev) — the hosted product (no-code visual scraper for AI agents)
