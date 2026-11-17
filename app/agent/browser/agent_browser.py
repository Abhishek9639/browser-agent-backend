"""Binds the Playwright driver to what the loop actually asks for.

The driver knows about pages, contexts and tabs. The loop knows about
perceiving, acting and photographing. This adapter is the one place those two
vocabularies meet, which is what lets the orchestrator be tested with a fake
that has no browser behind it at all.
"""

import asyncio
from contextlib import suppress
from typing import Final

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

from app.agent.browser.driver import ActionResult, BrowserSnapshot, PlaywrightDriver
from app.agent.browser.screenshots import Screenshot, capture
from app.agent.browser.stability import settle
from app.agent.perception.dom_extractor import PageNotReadyError
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.core.config import Settings
from app.core.logging import get_logger

__all__ = ["PERCEIVE_ATTEMPTS", "DriverBrowser"]

logger = get_logger(__name__)

# Reads of the page before perception gives up. A navigation in flight
# replaces the document under the collector, and the read after the new one
# has loaded nearly always works; three covers a redirect chain as well.
PERCEIVE_ATTEMPTS: Final = 3
# Added on each retry, so the second wait is longer than the first.
RETRY_PAUSE_S: float = 0.5
# What a page in the middle of changing raises. A closed window is not among
# them: the driver reports that as BrowserClosedError, which is not retried.
_NOT_READY: Final = (PageNotReadyError, PlaywrightError, TimeoutError)


class DriverBrowser:
    """An :class:`app.agent.contracts.AgentBrowser` backed by a real browser."""

    def __init__(self, driver: PlaywrightDriver, settings: Settings) -> None:
        self._driver = driver
        self._token_budget = settings.AGENT_DOM_TOKEN_BUDGET
        self._quiet_ms = settings.AGENT_SETTLE_QUIET_MS
        self._settle_timeout_ms = settings.AGENT_SETTLE_TIMEOUT_MS

    async def perceive(self) -> PageState:
        state = await self._read_page()
        # Perception is the only moment the session manager can learn what a
        # tab holds without doing browser work of its own, so this is where
        # the labels behind "Tabs: t1 SkyFare results" come from.
        self._driver.sessions.remember(state)
        return state

    async def _read_page(self) -> PageState:
        """Perceive, looking again while the page is between documents.

        The collector turns every Playwright failure into PageNotReadyError,
        so this is where that error is answered. It used to escape and end the
        task as an "internal error" whenever a click started a navigation that
        was still running when the page was read.

        ``self._driver.page`` is resolved on every attempt, so a tab that
        closed itself (a payment or sign-in popup) gives way to the tab the
        session manager falls back to.
        """
        for attempt in range(1, PERCEIVE_ATTEMPTS):
            try:
                return await perceive(
                    self._driver.page,
                    self._driver,
                    token_budget=self._token_budget,
                )
            except _NOT_READY as exc:
                logger.info("perceive_retry", attempt=attempt, error_type=type(exc).__name__)
                await self._wait_for_page(attempt)
        # The last attempt raises whatever it meets.
        return await perceive(
            self._driver.page,
            self._driver,
            token_budget=self._token_budget,
        )

    async def _wait_for_page(self, attempt: int) -> None:
        """Let a navigation finish before the next read. Never raises for a page."""
        with suppress(PlaywrightError):
            await self._driver.page.wait_for_load_state(
                "domcontentloaded", timeout=self._settle_timeout_ms
            )
        await asyncio.sleep(RETRY_PAUSE_S * attempt)
        await self.settle()

    async def settle(self) -> None:
        await settle(
            self._driver.page,  # type: ignore[arg-type]
            quiet_ms=self._quiet_ms,
            timeout_ms=self._settle_timeout_ms,
        )

    async def screenshot(self) -> Screenshot:
        # The coloured frame is for the person watching, not for the model.
        await self._driver.hide_indicator(True)
        try:
            try:
                return await capture(
                    self._driver.page,  # type: ignore[arg-type]
                    viewport=await self._driver.viewport_size(),
                )
            except PlaywrightTimeoutError:
                await asyncio.sleep(1.0)
                return await capture(
                    self._driver.page,  # type: ignore[arg-type]
                    viewport=await self._driver.viewport_size(),
                )
        finally:
            await self._driver.hide_indicator(False)

    async def set_indicator(self, mode: str) -> None:
        await self._driver.set_indicator(mode)

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
            viewport = await self._driver.viewport_size()
            if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
                return False
            await self._driver.page.mouse.click(x, y)
        except Exception:
            return False
        return True
