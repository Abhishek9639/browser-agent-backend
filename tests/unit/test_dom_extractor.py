"""B10: the DOM collector (spec sections 4.6 and 4.11).

Every page is a local fixture. The collector is the agent's eyes, so what it
refuses to report matters as much as what it reports: spec section 4.11 keeps
hidden content out precisely because that is where injected instructions live.
"""

import pytest

from app.agent.browser.actions import NavigateAction
from app.agent.browser.driver import PlaywrightDriver
from app.agent.perception.dom_extractor import (
    MASK,
    ElementInfo,
    PageNotReadyError,
    collector_source,
    extract,
    locator_for_index,
)

pytestmark = pytest.mark.browser


async def open_page(driver: PlaywrightDriver, html_server: str, page: str) -> list[ElementInfo]:
    await driver.execute(NavigateAction(url=f"{html_server}/{page}"))
    return await extract(driver.page)


def named(elements: list[ElementInfo], name: str) -> ElementInfo:
    return next(element for element in elements if element.name == name)


# ------------------------------------------------------------------ basics


async def test_finds_every_control_on_a_form(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    tags = {element.tag for element in elements}
    assert {"input", "select", "textarea", "button", "a"} <= tags


async def test_indices_are_sequential_from_zero(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert [element.index for element in elements] == list(range(len(elements)))


async def test_accessible_name_comes_from_the_label(
    driver: PlaywrightDriver, html_server: str
) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "From").input_type == "text"


async def test_placeholder_is_reported(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "To").placeholder == "Arrival city"


async def test_values_are_reported(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "From").value == "Delhi"


async def test_href_is_path_and_query_only(driver: PlaywrightDriver, html_server: str) -> None:
    """The origin is already known from the page URL; full URLs bloat the prompt."""
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "Booking help").href == "/help?topic=booking"
    assert "http" not in (named(elements, "Booking help").href or "")


async def test_disabled_state(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "Reset").disabled
    assert not named(elements, "Search flights").disabled


async def test_checked_state(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "Direct only").checked is True


async def test_expanded_state(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "results.html")

    assert named(elements, "More filters").expanded is False


async def test_bbox_and_viewport_flag(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")
    button = named(elements, "Search flights")

    assert button.bbox.width > 0
    assert button.bbox.height > 0
    assert button.in_viewport


async def test_selector_paths_are_populated(driver: PlaywrightDriver, html_server: str) -> None:
    """Stored so recovery can fall back to them when the stamp is gone."""
    elements = await open_page(driver, html_server, "flight_search.html")
    button = named(elements, "Search flights")

    assert button.css_path
    assert button.xpath.startswith("/html")


async def test_text_is_clipped_to_eighty_characters(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/flight_search.html"))
    await driver.page.evaluate(
        "() => { document.getElementById('submit').textContent = 'x'.repeat(300); }"
    )

    elements = await extract(driver.page)

    assert all(len(element.text) <= 80 for element in elements)


async def test_whitespace_is_collapsed(driver: PlaywrightDriver, html_server: str) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/flight_search.html"))
    await driver.page.evaluate(
        "() => { document.getElementById('submit').textContent = '  Search \\n\\n   flights  '; }"
    )

    elements = await extract(driver.page)

    assert any(element.text == "Search flights" for element in elements)


# ------------------------------------------------- prompt injection defence


async def test_no_hidden_element_is_reported(driver: PlaywrightDriver, html_server: str) -> None:
    """Spec section 4.11. This is the security test of the part.

    The fixture hides seven controls seven different ways, each saying
    "ignore previous instructions". An element the model never sees is an
    element it can never be steered by.
    """
    elements = await open_page(driver, html_server, "hidden_injection.html")

    assert len(elements) == 1
    assert elements[0].name == "Confirm booking"


@pytest.mark.parametrize(
    "element_id",
    ["x1", "x2", "x3", "x4", "x5", "x6", "x7"],
)
async def test_each_hiding_technique_is_defeated(
    driver: PlaywrightDriver, html_server: str, element_id: str
) -> None:
    """display:none, visibility, opacity, off-screen, tiny, aria-hidden, hidden."""
    await open_page(driver, html_server, "hidden_injection.html")

    stamped = await driver.page.get_attribute(f"#{element_id}", "data-agent-idx")

    assert stamped is None, f"#{element_id} was offered to the planner"


async def test_a_sliver_sized_control_is_not_reported(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """width:0;height:0 on a button still renders about 16x6 from its border.

    Taking "zero size" literally let that through, which is a free hiding
    place for an injected control.
    """
    await open_page(driver, html_server, "hidden_injection.html")

    box = await driver.page.evaluate(
        "() => { const r = document.getElementById('x5').getBoundingClientRect();"
        " return { w: r.width, h: r.height }; }"
    )

    assert box["w"] > 0 and box["h"] > 0, "the fixture must still render something"
    assert await driver.page.get_attribute("#x5", "data-agent-idx") is None


async def test_injected_text_never_appears_in_any_field(
    driver: PlaywrightDriver, html_server: str
) -> None:
    elements = await open_page(driver, html_server, "hidden_injection.html")

    blob = " ".join(
        f"{element.name} {element.text} {element.value or ''}" for element in elements
    ).lower()

    assert "ignore previous" not in blob
    assert "admin mode" not in blob


# ------------------------------------------------------------------ masking


@pytest.mark.parametrize("label", ["Password", "Card number", "CVV", "OTP", "PIN"])
async def test_secret_values_are_masked(
    driver: PlaywrightDriver, html_server: str, label: str
) -> None:
    """Masked at collection, so the real value cannot reach a prompt or a log."""
    elements = await open_page(driver, html_server, "password_form.html")

    assert named(elements, label).value == MASK


async def test_ordinary_values_are_not_masked(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "password_form.html")

    assert named(elements, "Email").value == "traveller@example.test"


async def test_no_real_secret_survives_anywhere_in_the_payload(
    driver: PlaywrightDriver, html_server: str
) -> None:
    elements = await open_page(driver, html_server, "password_form.html")

    payload = " ".join(element.model_dump_json() for element in elements)

    for secret in ("hunter2", "4111111111111111", "737", "908112", "4429"):
        assert secret not in payload, secret


# ------------------------------------------------------------------ covering


async def test_elements_behind_a_modal_are_kept_and_flagged(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The recovery ladder dismisses overlays, so it must see what is behind."""
    elements = await open_page(driver, html_server, "cookie_modal.html")

    assert named(elements, "Search flights").covered is True
    assert named(elements, "Accept all").covered is False


async def test_the_modal_itself_is_reachable(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "cookie_modal.html")

    assert {"Accept all", "Reject non-essential"} <= {element.name for element in elements}


# ------------------------------------------------------------------ frames


async def test_same_origin_frames_are_walked(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "iframe.html")

    assert named(elements, "Inside the frame").frame_id != "main"
    assert named(elements, "Outside the frame").frame_id == "main"


async def test_frame_bboxes_are_offset_into_the_top_level_viewport(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """One coordinate space, so nothing downstream redoes the arithmetic."""
    elements = await open_page(driver, html_server, "iframe.html")
    inner = named(elements, "Inside the frame")

    frame_box = await driver.page.locator("#same").bounding_box()

    assert frame_box is not None
    assert inner.bbox.y >= frame_box["y"]


# ------------------------------------------------------------------ pointer


async def test_a_pointer_styled_div_is_offered(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    assert named(elements, "Styled like a button").tag == "div"


async def test_a_pointer_wrapper_around_a_real_button_is_not_duplicated(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """Keep the meaningful target, drop the wrapper."""
    elements = await open_page(driver, html_server, "flight_search.html")

    matching = [e for e in elements if "Real button inside pointer wrapper" in e.name]

    assert len(matching) == 1
    assert matching[0].tag == "button"


# ------------------------------------------------------------------ stamping


async def test_every_element_is_stamped_with_its_index(
    driver: PlaywrightDriver, html_server: str
) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")

    for element in elements:
        count = await driver.page.locator(f'[data-agent-idx="{element.index}"]').count()
        assert count == 1, element.index


async def test_stale_stamps_are_cleared_between_runs(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """Otherwise an old index resolves to the wrong element after a re-render."""
    await open_page(driver, html_server, "flight_search.html")
    await driver.page.evaluate(
        "() => { document.getElementById('help').remove();"
        " document.getElementById('from').remove(); }"
    )

    elements = await extract(driver.page)
    total_stamped = await driver.page.locator("[data-agent-idx]").count()

    assert total_stamped == len(elements)


async def test_locator_for_index_resolves(driver: PlaywrightDriver, html_server: str) -> None:
    elements = await open_page(driver, html_server, "flight_search.html")
    button = named(elements, "Search flights")

    locator = await locator_for_index(driver.page, button.index)

    assert await locator.inner_text() == "Search flights"


async def test_locator_for_index_reaches_into_a_frame(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """page.locator only sees the main frame; the helper searches all of them."""
    elements = await open_page(driver, html_server, "iframe.html")
    inner = named(elements, "Inside the frame")

    locator = await locator_for_index(driver.page, inner.index)

    assert await locator.inner_text() == "Inside the frame"


async def test_locator_for_an_unknown_index_is_a_clear_error(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await open_page(driver, html_server, "flight_search.html")

    with pytest.raises(PageNotReadyError, match="Re-perceive"):
        await locator_for_index(driver.page, 9999)


# ------------------------------------------------------------------ dynamic


async def test_content_added_after_load_is_found_on_re_extraction(
    driver: PlaywrightDriver, html_server: str
) -> None:
    before = await open_page(driver, html_server, "dynamic.html")
    await driver.page.locator("#add").click()

    after = await extract(driver.page)

    assert len(after) == len(before) + 1
    assert any(element.name == "Appeared later" for element in after)


async def test_the_stamp_survives_a_class_rename(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """A chaos case from spec section 12: shuffled class names and ids."""
    elements = await open_page(driver, html_server, "dynamic.html")
    target = named(elements, "Add a button")
    await driver.page.locator("#shuffle").click()

    locator = await locator_for_index(driver.page, target.index)

    assert await locator.inner_text() == "Add a button"


# ------------------------------------------------------------------ plumbing


def test_the_collector_is_loaded_as_a_package_resource() -> None:
    source = collector_source()

    assert "data-agent-idx" in source
    assert source.lstrip().startswith(("/**", "()"))


async def test_a_page_that_cannot_be_read_raises_clearly() -> None:
    class Broken:
        async def evaluate(self, expression: str, arg: object = None) -> object:
            from playwright.async_api import Error as PlaywrightError

            raise PlaywrightError("Execution context was destroyed")

    with pytest.raises(PageNotReadyError, match="Could not read the page"):
        await extract(Broken())


async def test_a_slow_page_times_out() -> None:
    class Slow:
        async def evaluate(self, expression: str, arg: object = None) -> object:
            import asyncio

            await asyncio.sleep(5)
            return []

    with pytest.raises(PageNotReadyError, match="did not respond"):
        await extract(Slow(), timeout_ms=50)
