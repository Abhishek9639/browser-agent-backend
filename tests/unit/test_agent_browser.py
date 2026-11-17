"""The adapter between the loop and the driver, without a browser.

What is tested here is the retry around perception. A click that starts a
navigation leaves the next read racing the new document, and that read used to
end the whole task as "internal error (PageNotReadyError)".
"""

from typing import Any

import pytest

from app.agent.browser import agent_browser
from app.agent.browser.agent_browser import PERCEIVE_ATTEMPTS, DriverBrowser
from app.agent.browser.errors import BrowserClosedError
from app.agent.perception.dom_extractor import PageNotReadyError
from app.agent.perception.state import PageState
from app.core.config import Settings

STATE = PageState(url="https://example.test/results", title="Results")


class FakePage:
    def __init__(self) -> None:
        self.load_waits = 0

    # Playwright's own signature, which this only mirrors.
    async def wait_for_load_state(self, state: str, *, timeout: float) -> None:  # noqa: ASYNC109
        self.load_waits += 1

    async def evaluate(self, expression: str, arg: object = None) -> object:
        return True


class FakeSessions:
    def __init__(self) -> None:
        self.remembered: list[PageState] = []

    def remember(self, state: PageState) -> None:
        self.remembered.append(state)


class FakeDriver:
    def __init__(self, *, closed: bool = False) -> None:
        self._page = FakePage()
        self._closed = closed
        self.sessions = FakeSessions()

    @property
    def page(self) -> FakePage:
        if self._closed:
            raise BrowserClosedError("The browser window or tab was closed.")
        return self._page


def reads(monkeypatch: pytest.MonkeyPatch, *outcomes: BaseException | PageState) -> list[int]:
    """Make perception return or raise these in turn. Returns a call counter."""
    calls: list[int] = []

    async def fake_perceive(page: object, session: object, **kwargs: Any) -> PageState:
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(agent_browser, "perceive", fake_perceive)
    monkeypatch.setattr(agent_browser, "RETRY_PAUSE_S", 0.0)
    return calls


def browser(settings: Settings, driver: FakeDriver) -> DriverBrowser:
    return DriverBrowser(driver, settings)  # type: ignore[arg-type]


async def test_a_read_that_meets_a_navigation_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls = reads(
        monkeypatch,
        PageNotReadyError("Could not read the page: Execution context was destroyed"),
        STATE,
    )
    driver = FakeDriver()

    state = await browser(settings, driver).perceive()

    assert state is STATE
    assert len(calls) == 2
    # It waited for the new document before looking again.
    assert driver.page.load_waits >= 1
    assert driver.sessions.remembered == [STATE]


async def test_a_timeout_is_tried_again_too(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls = reads(monkeypatch, TimeoutError(), STATE)

    assert await browser(settings, FakeDriver()).perceive() is STATE
    assert len(calls) == 2


async def test_a_page_that_never_settles_gives_up_after_a_few_reads(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls = reads(monkeypatch, PageNotReadyError("The page did not respond within 5000 ms."))

    with pytest.raises(PageNotReadyError):
        await browser(settings, FakeDriver()).perceive()
    assert len(calls) == PERCEIVE_ATTEMPTS


async def test_a_closed_window_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Retrying a window that is gone only delays the message that says so."""
    calls = reads(monkeypatch, STATE)

    with pytest.raises(BrowserClosedError):
        await browser(settings, FakeDriver(closed=True)).perceive()
    assert calls == []
