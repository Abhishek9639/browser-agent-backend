"""B11: one full perception pass against real pages."""

import pytest

from app.agent.browser.actions import NavigateAction, ScrollAction
from app.agent.browser.driver import PlaywrightDriver
from app.agent.perception.perceive import perceive
from app.agent.perception.state import PageState, compute_dom_hash
from app.agent.perception.untrusted import wrap_page_state

pytestmark = pytest.mark.browser


async def look_at(driver: PlaywrightDriver, html_server: str, page: str) -> PageState:
    await driver.execute(NavigateAction(url=f"{html_server}/{page}"))
    return await perceive(driver.page, driver)


# ------------------------------------------------------------------ state


async def test_state_is_populated(driver: PlaywrightDriver, html_server: str) -> None:
    state = await look_at(driver, html_server, "flight_search.html")

    assert state.url.endswith("flight_search.html")
    assert state.title == "SkyFare search"
    assert state.tabs == ["t1"]
    assert state.active_tab_id == "t1"
    assert state.element_count == 11
    assert state.summary_text
    assert state.dom_hash
    assert state.captured_at


async def test_summary_matches_the_spec_shape(driver: PlaywrightDriver, html_server: str) -> None:
    state = await look_at(driver, html_server, "flight_search.html")

    assert "URL: " in state.summary_text
    assert '[6] button "Search flights"' in state.summary_text
    assert 'a "Booking help" href=/help?topic=booking' in state.summary_text


async def test_scroll_position_is_reported(driver: PlaywrightDriver, html_server: str) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/long_page.html"))
    await driver.execute(ScrollAction(direction="down", pixels=900))

    state = await perceive(driver.page, driver)

    assert state.scroll.y > 0
    assert state.scroll.max_y > 0


async def test_element_at_backs_the_target_existence_check(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """Spec section 4.11: an index that was never offered is rejected."""
    state = await look_at(driver, html_server, "flight_search.html")

    assert state.element_at(0) is not None
    assert state.element_at(9999) is None


# ------------------------------------------------------------------ dom hash


async def test_the_same_page_hashes_the_same(driver: PlaywrightDriver, html_server: str) -> None:
    first = await look_at(driver, html_server, "flight_search.html")
    second = await look_at(driver, html_server, "flight_search.html")

    assert first.dom_hash == second.dom_hash


async def test_different_pages_hash_differently(driver: PlaywrightDriver, html_server: str) -> None:
    first = await look_at(driver, html_server, "flight_search.html")
    second = await look_at(driver, html_server, "results.html")

    assert first.dom_hash != second.dom_hash


async def test_scrolling_does_not_change_the_hash(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """Otherwise the section 4.7 dom_changed check fires on every scroll."""
    await driver.execute(NavigateAction(url=f"{html_server}/long_page.html"))
    before = await perceive(driver.page, driver)
    await driver.execute(ScrollAction(direction="down", pixels=600))

    after = await perceive(driver.page, driver)

    assert before.dom_hash == after.dom_hash


async def test_adding_an_element_changes_the_hash(
    driver: PlaywrightDriver, html_server: str
) -> None:
    before = await look_at(driver, html_server, "dynamic.html")
    await driver.page.locator("#add").click()

    after = await perceive(driver.page, driver)

    assert before.dom_hash != after.dom_hash


def test_the_hash_is_order_sensitive() -> None:
    """Two pages with the same controls rearranged are different pages."""
    from app.agent.perception.dom_extractor import BoundingBox, ElementInfo

    def make(index: int, name: str) -> ElementInfo:
        return ElementInfo(
            index=index,
            tag="button",
            name=name,
            bbox=BoundingBox(x=0, y=0, width=10, height=10),
            in_viewport=True,
        )

    forward = [make(0, "A"), make(1, "B")]
    backward = [make(0, "B"), make(1, "A")]

    assert compute_dom_hash("u", forward) != compute_dom_hash("u", backward)


# ------------------------------------------------------------------ safety


async def test_hidden_injection_never_reaches_the_summary(
    driver: PlaywrightDriver, html_server: str
) -> None:
    state = await look_at(driver, html_server, "hidden_injection.html")

    assert "ignore previous" not in state.summary_text.lower()
    assert "admin mode" not in state.summary_text.lower()
    assert state.element_count == 1


async def test_the_whole_prompt_payload_is_fenced_and_clean(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The end to end property: what the model receives carries no secret and
    cannot close its own fence."""
    state = await look_at(driver, html_server, "password_form.html")

    payload = wrap_page_state(state.summary_text)

    assert payload.count("</page_state>") == 1
    for secret in ("hunter2", "4111111111111111", "908112"):
        assert secret not in payload


async def test_modal_state_is_visible_to_the_planner(
    driver: PlaywrightDriver, html_server: str
) -> None:
    state = await look_at(driver, html_server, "cookie_modal.html")

    assert "(covered)" in state.summary_text
    assert "Accept all" in state.summary_text


# ------------------------------------------------------------------ frames


async def test_frame_elements_appear_in_the_summary(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/iframe.html"))
    # Navigating returns at DOMContentLoaded, which can beat the frame's own
    # load; the agent reads only after the page settles.
    await driver.page.wait_for_load_state("load")
    state = await perceive(driver.page, driver)

    assert "Inside the frame" in state.summary_text
    assert "Outside the frame" in state.summary_text


async def test_tabs_are_listed(driver: PlaywrightDriver, html_server: str) -> None:
    from app.agent.browser.actions import NewTabAction

    await driver.execute(NavigateAction(url=f"{html_server}/flight_search.html"))
    await driver.execute(NewTabAction(url=f"{html_server}/results.html"))

    state = await perceive(driver.page, driver)

    assert len(state.tabs) == 2
    assert "(active)" in state.summary_text


async def test_perceive_works_without_a_session(driver: PlaywrightDriver, html_server: str) -> None:
    """The session is optional, so the loop can be exercised without a driver."""
    await driver.execute(NavigateAction(url=f"{html_server}/results.html"))

    state = await perceive(driver.page)

    assert state.tabs == []
    assert state.active_tab_id == "main"
    assert state.element_count > 0
