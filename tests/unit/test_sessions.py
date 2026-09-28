"""The multi-tab session manager (spec sections 4.1 and 4.4).

These run against a real Chromium, because the behaviour under test is mostly
about what Playwright does: which pages fire the ``page`` event, when a page
reports itself closed, and what happens to the context when nobody tidies up.
A fake that answered those questions the way I expect would be testing my
expectations rather than the browser.
"""

import pytest
from playwright.async_api import Error as PlaywrightError

from app.agent.browser.actions import (
    ClickAction,
    CloseTabAction,
    ExtractAction,
    NewTabAction,
    SwitchTabAction,
    WaitAction,
)
from app.agent.browser.driver import PlaywrightDriver
from app.agent.browser.errors import ActionErrorCode
from app.agent.browser.sessions import TabLimitReached, TabSessionManager
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.core.config import Settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def small(browser_settings: Settings) -> object:
    """A driver with room for three tabs, so the limit is cheap to reach."""
    return PlaywrightDriver(browser_settings.model_copy(update={"AGENT_MAX_TABS": 3}))


async def goto(driver: PlaywrightDriver, base: str, page: str) -> None:
    await driver.page.goto(f"{base}/{page}", wait_until="domcontentloaded")


# ------------------------------------------------------------------- ids


async def test_the_first_tab_is_t1(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "tabs.html")

    assert driver.tab_ids == ["t1"]
    assert driver.active_tab_id == "t1"


async def test_ids_are_never_reused(driver: PlaywrightDriver, html_server: str) -> None:
    """A closed tab's id is retired.

    Reusing it would make the planner's own transcript lie: "switch to t2"
    three steps ago and "switch to t2" now would name different pages, and
    nothing in the conversation would say so.
    """
    await goto(driver, html_server, "tabs.html")
    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))
    assert driver.tab_ids == ["t1", "t2"]

    await driver.execute(CloseTabAction(tab_id="t2"))
    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))

    assert driver.tab_ids == ["t1", "t3"]


async def test_opening_a_tab_does_not_count_it_twice(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """``new_page()`` fires the ``page`` event as well as returning the page.

    An early version registered the tab from both paths and reported two.
    """
    await goto(driver, html_server, "tabs.html")

    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))
    await driver.execute(WaitAction(seconds=0.2))

    assert driver.tab_ids == ["t1", "t2"]


# ----------------------------------------------------------------- popups


async def test_a_target_blank_link_becomes_a_tab(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The section 12 ``newtab`` chaos case, which is how SkyFare behaves."""
    await goto(driver, html_server, "tabs.html")

    await driver.execute(ClickAction(selector="#detail"))
    await driver.execute(WaitAction(seconds=0.3))

    assert driver.tab_ids == ["t1", "t2"]
    # The click opened it; it did not move the agent into it. Switching is a
    # decision the planner makes, not a side effect of a link's attributes.
    assert driver.active_tab_id == "t1"


async def test_the_agent_can_read_a_popup_and_come_back(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The whole point of tracking popups, end to end."""
    await goto(driver, html_server, "tabs.html")
    await driver.execute(ClickAction(selector="#detail"))
    await driver.execute(WaitAction(seconds=0.3))

    await driver.execute(SwitchTabAction(tab_id="t2"))
    read = await driver.execute(ExtractAction())

    assert read.ok
    assert "5,260" in (read.extracted or "")

    await driver.execute(SwitchTabAction(tab_id="t1"))

    assert driver.page.url.endswith("tabs.html")


# ------------------------------------------------------------------ limit


async def test_the_limit_stops_the_agent_opening_more(
    small: PlaywrightDriver, html_server: str
) -> None:
    await small.launch()
    try:
        await goto(small, html_server, "tabs.html")
        await small.execute(NewTabAction(url=f"{html_server}/detail.html"))
        await small.execute(NewTabAction(url=f"{html_server}/detail.html"))

        refused = await small.execute(NewTabAction(url=f"{html_server}/detail.html"))

        assert not refused.ok
        assert refused.error_code is ActionErrorCode.TAB_LIMIT_REACHED
        assert small.tab_ids == ["t1", "t2", "t3"]
    finally:
        await small.close()


async def test_the_limit_holds_against_a_page_opening_tabs_itself(
    small: PlaywrightDriver, html_server: str
) -> None:
    """A page that opens eight tabs in a loop does not get eight tabs.

    This is the case the limit exists for. An agent asking for too many tabs
    is a planning mistake; a page spawning them is the site fighting back, and
    it is the one that can do it faster than the ladder can recover.
    """
    await small.launch()
    try:
        await goto(small, html_server, "tabs.html")
        await small.execute(ClickAction(selector="#spawn"))
        await small.execute(WaitAction(seconds=1.0))

        assert len(small.tab_ids) == 3
    finally:
        await small.close()


async def test_the_limit_frees_up_when_a_tab_closes(
    small: PlaywrightDriver, html_server: str
) -> None:
    await small.launch()
    try:
        await goto(small, html_server, "tabs.html")
        await small.execute(NewTabAction(url=f"{html_server}/detail.html"))
        await small.execute(NewTabAction(url=f"{html_server}/detail.html"))
        await small.execute(CloseTabAction(tab_id="t2"))

        opened = await small.execute(NewTabAction(url=f"{html_server}/detail.html"))

        assert opened.ok
        assert small.tab_ids == ["t1", "t3", "t4"]
    finally:
        await small.close()


# ------------------------------------------------------------------ close


async def test_closing_the_active_tab_lands_on_the_most_recent(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """Not the oldest.

    An agent that opened a detail view from a results page wants the results
    page back when it closes the detail view, and that is the tab before this
    one rather than whatever it started on.
    """
    await goto(driver, html_server, "tabs.html")
    await driver.execute(NewTabAction(url=f"{html_server}/select.html"))
    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))

    await driver.execute(CloseTabAction())

    assert driver.active_tab_id == "t2"
    assert driver.page.url.endswith("select.html")


async def test_closing_a_background_tab_leaves_the_active_one_alone(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "tabs.html")
    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))

    await driver.execute(CloseTabAction(tab_id="t1"))

    assert driver.active_tab_id == "t2"
    assert driver.tab_ids == ["t2"]


async def test_closing_an_unknown_tab_fails_cleanly(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "tabs.html")

    result = await driver.execute(CloseTabAction(tab_id="t42"))

    assert not result.ok
    assert result.error_code is ActionErrorCode.TAB_NOT_FOUND


async def test_a_tab_the_page_closed_is_forgotten(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """``window.close()`` is not something the agent asked for.

    Reporting it as open would hand the planner a tab it cannot switch to.

    The tab is opened by the page rather than by ``new_tab``, because Chromium
    only lets a script close a window a script opened.
    """
    await goto(driver, html_server, "tabs.html")
    await driver.execute(ClickAction(selector="#detail"))
    await driver.execute(WaitAction(seconds=0.3))
    await driver.execute(SwitchTabAction(tab_id="t2"))

    await driver.page.evaluate("() => window.close()")
    await driver.execute(WaitAction(seconds=0.3))

    assert driver.tab_ids == ["t1"]
    assert driver.active_tab_id == "t1"


async def test_orphans_are_closed_when_the_task_ends(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """A popup nobody closed must not outlive the task.

    The context is shut down here too, so this is really about the manager
    being asked first: on a browser shared between runs, the context is not
    always what goes away.
    """
    await goto(driver, html_server, "tabs.html")
    await driver.execute(ClickAction(selector="#detail"))
    await driver.execute(ClickAction(selector="#second"))
    await driver.execute(WaitAction(seconds=0.3))
    assert len(driver.tab_ids) == 3

    closed = await driver.sessions.close_all()

    assert closed == 3
    assert driver.tab_ids == []


# ------------------------------------------------------------------ labels


async def test_the_header_names_what_each_tab_holds(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """ "Tabs: t1 Results (active), t2 Flight AI-203 details".

    A bare "t1, t2" tells a planner nothing about which tab to switch to, so
    the summary carries each tab's title.
    """
    await goto(driver, html_server, "tabs.html")
    first = await perceive(driver.page, driver)
    driver.sessions.remember(first)

    await driver.execute(NewTabAction(url=f"{html_server}/detail.html"))
    second = await perceive(driver.page, driver)
    driver.sessions.remember(second)

    line = next(row for row in second.summary_text.splitlines() if row.startswith("Tabs:"))

    assert line == "Tabs: t1 Results, t2 Flight AI-203 details (active)"


async def test_a_tab_nobody_has_looked_at_yet_prints_as_its_id(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "tabs.html")
    await driver.execute(ClickAction(selector="#detail"))
    await driver.execute(WaitAction(seconds=0.3))

    state = await perceive(driver.page, driver)
    line = next(row for row in state.summary_text.splitlines() if row.startswith("Tabs:"))

    assert line == "Tabs: t1 Results (active), t2"


# ------------------------------------------------------------- the registry


async def test_the_registry_answers_for_the_tab_id_the_loop_records() -> None:
    """The manager's answer wins over the one in the page state.

    The state is a snapshot from whenever perception last ran; the registry is
    current. A step event has to name a tab the client can still find.
    """
    manager = TabSessionManager()
    stale = PageState(url="https://skyfare.test/", active_tab_id="t9")

    assert manager.tab_id(stale) == "t9"


async def test_opening_past_the_limit_raises() -> None:
    """The manager's own contract, without a driver in the way."""
    manager = TabSessionManager(max_tabs=0)

    with pytest.raises(TabLimitReached):
        await manager.open(None)  # type: ignore[arg-type]


async def test_an_unlaunched_manager_has_no_page() -> None:
    manager = TabSessionManager()

    with pytest.raises(PlaywrightError):
        _ = manager.page
