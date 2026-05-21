"""Basic example — open a page, snapshot its HTML.

    python examples/basic.py

Tests against bot.sannysoft.com (the standard "am I detected as a bot"
fingerprint test page). You should see most checks pass.
"""

import asyncio

from stealth_browser import StealthBrowser


async def main():
    pool = StealthBrowser()
    await pool.start()
    try:
        async with pool.tab("https://bot.sannysoft.com/") as tab:
            await tab.wait(3)  # let the page run its detection tests
            html = await tab.get_content()
            # Print a snippet so you can see the table of results
            print(html[:2000])
    finally:
        await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
