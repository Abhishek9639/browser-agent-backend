"""Binds the Playwright driver to what the loop actually asks for.

The driver knows about pages, contexts and tabs. The loop knows about
perceiving, acting and photographing. This adapter is the one place those two
vocabularies meet, which is what lets the orchestrator be tested with a fake
that has no browser behind it at all.
"""

from pydantic import BaseModel

from app.agent.browser.driver import ActionResult, BrowserSnapshot, PlaywrightDriver
from app.agent.browser.screenshots import Screenshot, capture
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.core.config import Settings

__all__ = ["DriverBrowser"]


class DriverBrowser:
    """An :class:`app.agent.contracts.AgentBrowser` backed by a real browser."""

    def __init__(self, driver: PlaywrightDriver, settings: Settings) -> None:
        self._driver = driver
        self._token_budget = settings.AGENT_DOM_TOKEN_BUDGET

    async def perceive(self) -> PageState:
        return await perceive(
            self._driver.page,
            self._driver,
            token_budget=self._token_budget,
        )

    async def screenshot(self) -> Screenshot:
        return await capture(self._driver.page)  # type: ignore[arg-type]

    async def capture_state(self) -> BrowserSnapshot:
        return await self._driver.capture_state()

    async def execute(self, action: BaseModel) -> ActionResult:
        return await self._driver.execute(action)
