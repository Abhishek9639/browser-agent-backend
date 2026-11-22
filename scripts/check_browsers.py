"""Check installed browser engines directly, preserving useful launch errors.

Run from a normal Terminal: .venv/bin/python scripts/check_browsers.py firefox
No database, AI provider, credentials or external website is needed.
"""

import argparse
import asyncio

from playwright.async_api import async_playwright


async def check(engines: list[str]) -> None:
    async with async_playwright() as playwright:
        for engine in engines:
            browser = await getattr(playwright, engine).launch(headless=True)
            try:
                context = await browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
                page = await context.new_page()
                await page.set_content("<h1>Browser ready</h1>")
                if await page.inner_text("h1") != "Browser ready":
                    raise RuntimeError(f"{engine} did not render the test page")
                print(f"PASS {engine} {browser.version}: launch, context and page rendering")
            finally:
                await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engines", nargs="*", choices=["chromium", "firefox"])
    arguments = parser.parse_args()
    asyncio.run(check(arguments.engines or ["chromium", "firefox"]))
