#!/usr/bin/env python3
"""Print what the agent would see on a page.

    python scripts/perceive.py https://www.example.com/
    python scripts/perceive.py --show-fenced https://www.example.com/

Opens a browser, runs one perception pass, and prints the summary exactly as
the planner would receive it. Useful for checking a page before pointing the
agent at it, and for showing what the model does and does not get told.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.browser.actions import NavigateAction
from app.agent.browser.driver import PlaywrightDriver
from app.agent.perception.perceive import perceive
from app.agent.perception.untrusted import wrap_page_state
from app.core.config import get_settings


async def run(url: str, *, headed: bool, show_fenced: bool, budget: int) -> int:
    settings = get_settings()
    # The point of the script is to look at an arbitrary page, so the sandbox
    # allow-list is widened to whatever was asked for. The agent itself never
    # does this.
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    settings = settings.model_copy(
        update={
            "AGENT_ALLOWED_DOMAINS": [*settings.AGENT_ALLOWED_DOMAINS, host],
            "AGENT_HEADLESS": not headed,
        }
    )

    driver = PlaywrightDriver(settings)
    await driver.launch()
    try:
        result = await driver.execute(NavigateAction(url=url))
        if not result.ok:
            print(f"Could not open {url}: {result.error_code} {result.error_message}")
            return 1

        state = await perceive(driver.page, driver, token_budget=budget)
    finally:
        await driver.close()

    print(state.summary_text)
    print()
    print(
        f"{state.element_count} elements "
        f"({sum(1 for e in state.elements if e.in_viewport)} in viewport, "
        f"{sum(1 for e in state.elements if e.covered)} covered)  "
        f"dom_hash={state.dom_hash}  "
        f"summary={len(state.summary_text)} chars "
        f"(~{len(state.summary_text) // 4} tokens of {budget})"
    )

    if show_fenced:
        print()
        print("--- as the prompt receives it ---")
        print(wrap_page_state(state.summary_text))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--show-fenced",
        action="store_true",
        help="also print the untrusted-data wrapping",
    )
    parser.add_argument("--budget", type=int, default=3000, help="summary token budget")
    args = parser.parse_args()

    return asyncio.run(
        run(args.url, headed=args.headed, show_fenced=args.show_fenced, budget=args.budget)
    )


if __name__ == "__main__":
    sys.exit(main())
