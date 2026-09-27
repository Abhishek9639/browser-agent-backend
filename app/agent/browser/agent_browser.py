"""Binds the Playwright driver to what the loop actually asks for.

The driver knows about pages, contexts and tabs. The loop knows about
perceiving, acting and photographing. This adapter is the one place those two
vocabularies meet, which is what lets the orchestrator be tested with a fake
that has no browser behind it at all.
"""

from contextlib import suppress

from pydantic import BaseModel

from app.agent.browser.driver import ActionResult, BrowserSnapshot, PlaywrightDriver
from app.agent.browser.screenshots import Screenshot, capture
from app.agent.browser.stability import settle
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.core.config import Settings

__all__ = ["DriverBrowser"]


class DriverBrowser:
    """An :class:`app.agent.contracts.AgentBrowser` backed by a real browser."""

    def __init__(self, driver: PlaywrightDriver, settings: Settings) -> None:
        self._driver = driver
        self._token_budget = settings.AGENT_DOM_TOKEN_BUDGET
        self._quiet_ms = settings.AGENT_SETTLE_QUIET_MS
        self._settle_timeout_ms = settings.AGENT_SETTLE_TIMEOUT_MS

    async def perceive(self) -> PageState:
        return await perceive(
            self._driver.page,
            self._driver,
            token_budget=self._token_budget,
        )

    async def settle(self) -> None:
        await settle(
            self._driver.page,  # type: ignore[arg-type]
            quiet_ms=self._quiet_ms,
            timeout_ms=self._settle_timeout_ms,
        )

    async def screenshot(self) -> Screenshot:
        return await capture(self._driver.page)  # type: ignore[arg-type]

    async def capture_state(self) -> BrowserSnapshot:
        return await self._driver.capture_state()

    async def execute(self, action: BaseModel) -> ActionResult:
        return await self._driver.execute(action)

    async def scroll_into_view(self, selector: str) -> bool:
        """Scroll an element into the middle of the viewport.

        Centred rather than merely on screen, because a sticky header or a
        cookie bar sitting at the top is exactly what tends to be covering the
        thing that would not click.
        """
        try:
            locator = self._driver.page.locator(selector).first
            if not await locator.count():
                return False
            await locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            return False
        return True

    async def press(self, key: str) -> None:
        with suppress(Exception):
            await self._driver.page.keyboard.press(key)

    async def click_at(self, x: int, y: int) -> bool:
        """Click a viewport coordinate.

        Bounds-checked here rather than trusted: a coordinate that came from a
        model naming a point on a picture is exactly the input that should not
        be handed to the mouse without a look.
        """
        try:
            viewport = self._driver.page.viewport_size or {"width": 0, "height": 0}
            if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
                return False
            await self._driver.page.mouse.click(x, y)
        except Exception:
            return False
        return True
