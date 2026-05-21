"""Real-world example — proxy rotation + bot-wall detection.

Demonstrates the two production features most people skip:

  1. CDP-level proxy auth (Chromium's --proxy-server can't do basic-auth
     on its own; the pool wires the Fetch.authRequired handler).
  2. Structured detection of which anti-bot vendor blocked you so you
     can route around it instead of silently returning empty data.

    python examples/with_proxy_and_detect.py
"""

import asyncio

from stealth_browser import StealthBrowser, detect_block


# Plug in real proxies — Webshare, BrightData, Oxylabs, whatever.
# For testing, datacenter proxies are fine; for production, residential.
PROXIES = [
    # "http://user:pass@1.2.3.4:6543",
    # "http://user:pass@5.6.7.8:6543",
]

TARGETS = [
    "https://news.ycombinator.com",      # trivial — should always pass
    "https://www.zillow.com",            # PerimeterX press-and-hold — should detect block
    "https://www.amazon.com/dp/B08N5WRWNW",  # works with rotating residential
]


async def main():
    pool = StealthBrowser(proxies=PROXIES)
    await pool.start()
    try:
        for url in TARGETS:
            try:
                async with pool.tab(url) as tab:
                    await tab.wait(3)
                    html = await tab.get_content()
                    title = await tab.evaluate("document.title")
                    block = detect_block(title=title, html=html, url=url)

                    print(f"\n── {url}")
                    print(f"  Title:   {title!r}")
                    print(f"  Proxy:   {pool.current_proxy or 'direct'}")
                    if block.blocked:
                        print(f"  🛑 BLOCKED by {block.vendor}: {block.title}")
                        print(f"     {block.message}")
                        print(f"     Suggestion: {block.suggestion}")
                    else:
                        print(f"  ✅ OK — extraction would proceed normally")
            except Exception as e:
                print(f"\n── {url}\n  ERROR: {e!r}")
    finally:
        await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
