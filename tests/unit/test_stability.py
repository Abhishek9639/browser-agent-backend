"""Waiting for a page to stop moving (spec section 4.7).

The point of these is the race, not the helper. A click that triggers a render
is not finished when `click()` returns, and verification that reads the page at
that moment fails a step that worked.
"""

import asyncio
import time

import pytest

from app.agent.browser.actions import ClickAction, NavigateAction
from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import PlaywrightDriver
from app.agent.browser.stability import await_late_overlays, await_popup_in_place, settle
from app.agent.perception.dom_extractor import modal_scope_source
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState
from app.agent.planner.schema import Decision
from app.agent.verification.verifier import DeterministicVerifier
from app.core.config import Settings

pytestmark = pytest.mark.browser


async def open_page(driver: PlaywrightDriver, html_server: str, page: str) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/{page}"))


def click_decision() -> Decision:
    return Decision.model_validate(
        {
            "action": "click",
            "targetIndex": 0,
            "target": "Search flights",
            "reasoning": "Run the search.",
            "expectedOutcome": {"type": "dom_changed", "value": ""},
            "confidence": 0.9,
        }
    )


async def test_reading_the_page_too_early_misses_the_result(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The failure mode, reproduced. This is what settling exists to prevent."""
    await open_page(driver, html_server, "slow_render.html")
    before = await perceive(driver.page, driver)

    await driver.page.click("#search")
    hasty = await perceive(driver.page, driver)

    # The click has returned and the render has not happened yet.
    assert hasty.dom_hash == before.dom_hash
    assert not any("Book this flight" in element.name for element in hasty.elements)


async def test_settling_waits_for_the_render(driver: PlaywrightDriver, html_server: str) -> None:
    await open_page(driver, html_server, "slow_render.html")
    before = await perceive(driver.page, driver)

    await driver.page.click("#search")
    settled = await settle(driver.page)
    after = await perceive(driver.page, driver)

    assert settled is True
    assert after.dom_hash != before.dom_hash
    assert any("Book this flight" in element.name for element in after.elements)


async def test_a_slow_step_verifies_once_the_page_has_settled(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """End to end: the same click fails verification early and passes late."""
    await open_page(driver, html_server, "slow_render.html")
    before = await perceive(driver.page, driver)
    verifier = DeterministicVerifier()

    result = await driver.execute(ClickAction(selector="#search"))
    hasty = await perceive(driver.page, driver)
    early = await verifier.check(click_decision(), before, hasty, result)

    await settle(driver.page)
    after = await perceive(driver.page, driver)
    late = await verifier.check(click_decision(), before, after, result)

    # Without the wait the agent would record a working step as failed and
    # start recovering from a success, which is the expensive kind of wrong.
    assert early.ok is False
    assert late.ok is True


async def test_a_page_that_never_goes_quiet_is_read_anyway(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "simple_form.html")
    # A timer that mutates the DOM forever, which is what an animation or a
    # polling widget looks like.
    await driver.page.evaluate(
        "() => { setInterval(() => { document.title = 'tick ' + Date.now(); }, 30); }"
    )

    settled = await settle(driver.page, quiet_ms=200, timeout_ms=700)

    # Reports that it gave up rather than raising or hanging: failing a step
    # because the site has a carousel on it would be worse.
    assert settled is False


async def test_settling_a_quiet_page_returns_quickly(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "simple_form.html")

    assert await settle(driver.page, quiet_ms=100, timeout_ms=3000) is True


# A page that settles before it asks for a city. Booking sites build themselves,
# go quiet, and only then put the location picker up on a timer.


async def test_settling_reads_straight_past_a_late_popup(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The failure mode, reproduced. The planner was handed this page."""
    await open_page(driver, html_server, "late_popup.html")

    assert await settle(driver.page) is True
    hasty = await perceive(driver.page, driver)

    # Settled, and still no popup: the planner sees the movie cards and clicks
    # one, and the picker lands on top of it a moment later.
    assert hasty.modal_open is False
    assert any("Avengers" in element.name for element in hasty.elements)

    # The popup is real and only late, not missing.
    await asyncio.sleep(1.2)
    assert (await perceive(driver.page, driver)).modal_open is True


async def test_waiting_for_late_overlays_lets_the_planner_see_the_popup(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "late_popup.html")
    await settle(driver.page)

    appeared = await await_late_overlays(driver.page, modal_scope=modal_scope_source())
    state = await perceive(driver.page, driver)

    assert appeared is True
    assert state.modal_open is True
    # The city choices are what the planner is now held to, which is what makes
    # it pick a city before anything on the page underneath.
    in_popup = {element.name for element in state.elements if element.in_modal}
    assert {"Mumbai", "Delhi-NCR", "Bengaluru"} <= in_popup


async def test_a_page_that_has_been_up_a_while_answers_at_once(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The cost on nearly every read: one round trip, no wait."""
    await open_page(driver, html_server, "simple_form.html")
    await asyncio.sleep(0.6)

    started = time.monotonic()
    appeared = await await_late_overlays(
        driver.page, modal_scope=modal_scope_source(), grace_ms=400
    )

    assert appeared is False
    assert time.monotonic() - started < 0.3


async def test_a_new_page_with_no_popup_is_held_no_longer_than_the_grace(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "simple_form.html")

    started = time.monotonic()
    appeared = await await_late_overlays(
        driver.page, modal_scope=modal_scope_source(), grace_ms=800
    )

    assert appeared is False
    # Bounded by the document's age, not added on top of it.
    assert time.monotonic() - started < 1.2


def agent_browser(driver: PlaywrightDriver, settings: Settings, *, grace_ms: int) -> DriverBrowser:
    # Set explicitly: the point is the difference the grace makes, so neither
    # side of that comparison may depend on the environment.
    return DriverBrowser(
        driver, settings.model_copy(update={"AGENT_LOAD_POPUP_GRACE_MS": grace_ms})
    )


async def test_without_the_grace_the_agent_browser_reads_past_a_late_popup(
    driver: PlaywrightDriver, html_server: str, settings: Settings
) -> None:
    """The bug on the real path, kept as the control for the test below.

    A task's start URL is loaded and then read with nothing settling it in
    between. That read happens before the popup exists.
    """
    browser = agent_browser(driver, settings, grace_ms=0)
    await open_page(driver, html_server, "late_popup.html")

    state = await browser.perceive()

    assert state.modal_open is False


async def test_the_agent_browser_sees_a_late_popup_straight_after_a_load(
    driver: PlaywrightDriver, html_server: str, settings: Settings
) -> None:
    """The fix, on the same path and the same page as the control above.

    Both reads the planner acts on go through ``perceive``: the first one after
    a start URL and the one after each action. A read made the instant the page
    loads still has to see the popup that is about to go up.
    """
    browser = agent_browser(driver, settings, grace_ms=2000)
    await open_page(driver, html_server, "late_popup.html")

    state = await browser.perceive()

    assert state.modal_open is True


async def test_a_grace_of_zero_turns_the_wait_off(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "late_popup.html")

    appeared = await await_late_overlays(driver.page, modal_scope=modal_scope_source(), grace_ms=0)

    assert appeared is False
    assert (await perceive(driver.page, driver)).modal_open is False


# --------------------------------------------- a popup still on its way in


async def open_format_picker(driver: PlaywrightDriver, html_server: str, query: str = "") -> None:
    """Click "Book tickets" and settle, the way the loop does after an action."""
    await open_page(driver, html_server, f"sliding_popup.html{query}")
    await driver.execute(ClickAction(selector="#book"))
    await settle(driver.page)


def popup_choices(state: PageState) -> set[str]:
    return {element.name for element in state.elements if element.in_modal and not element.covered}


async def test_reading_a_sliding_popup_too_early_finds_none_of_its_choices(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """BookMyShow's format picker, reproduced, as the control for the tests below.

    The veil is up and the popup is detected, but its panel is still below the
    fold, so not one format is on screen and the page behind it is covered. A
    planner reading this has nothing to choose and answers from the background:
    "it is listed as I'm interested, so it cannot be booked".
    """
    await open_format_picker(driver, html_server)

    state = await perceive(driver.page, driver)

    assert state.modal_open is True
    assert "DOLBY CINEMA 2D" not in popup_choices(state)


async def test_waiting_for_the_popup_to_come_to_rest_finds_its_choices(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_format_picker(driver, html_server)

    placement = await await_popup_in_place(driver.page, modal_scope=modal_scope_source())
    state = await perceive(driver.page, driver)

    assert placement == "waited"
    assert {"DOLBY CINEMA 2D", "IMAX 3D", "Select all"} <= popup_choices(state)


async def test_the_agent_browser_reads_a_sliding_popup_once_it_is_in_place(
    driver: PlaywrightDriver, html_server: str, settings: Settings
) -> None:
    """The fix on the path every read the planner acts on takes."""
    # No load grace, so the wait being tested is the only one in play.
    browser = agent_browser(driver, settings, grace_ms=0)
    await open_format_picker(driver, html_server)

    state = await browser.perceive()

    assert "DOLBY CINEMA 2D" in popup_choices(state)


async def test_with_no_popup_open_the_check_costs_one_round_trip(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "simple_form.html")

    started = time.monotonic()
    placement = await await_popup_in_place(driver.page, modal_scope=modal_scope_source())

    assert placement == "none"
    assert time.monotonic() - started < 0.3


async def test_a_spinner_inside_a_popup_does_not_hold_the_read(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """An animation that never ends is not the popup still arriving."""
    await open_format_picker(driver, html_server, "?spinner=1")
    assert await await_popup_in_place(driver.page, modal_scope=modal_scope_source()) == "waited"

    # At rest now, with the spinner still turning: the next look answers at once.
    started = time.monotonic()
    placement = await await_popup_in_place(driver.page, modal_scope=modal_scope_source())

    assert placement == "ready"
    assert time.monotonic() - started < 0.4


async def test_a_panel_that_never_arrives_holds_the_read_no_longer_than_the_timeout(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_format_picker(driver, html_server, "?mode=stuck")

    started = time.monotonic()
    placement = await await_popup_in_place(
        driver.page, modal_scope=modal_scope_source(), timeout_ms=600
    )

    assert placement == "timeout"
    assert time.monotonic() - started < 1.2
