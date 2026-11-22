"""B08: the Playwright driver, against local fixture pages only.

Every page here is a file in tests/fixtures/html served from 127.0.0.1. The
suite never reaches the internet, so it is deterministic, offline-capable, and
any failure is our bug rather than someone else's outage.
"""

from dataclasses import replace

import pytest

from app.agent.browser.actions import (
    ClickAction,
    CloseTabAction,
    DoneAction,
    ExtractAction,
    GoBackAction,
    NavigateAction,
    NewTabAction,
    ScrollAction,
    SelectAction,
    SwitchTabAction,
    TypeAction,
    WaitAction,
)
from app.agent.browser.driver import (
    ActionResult,
    BrowserDriver,
    PlaywrightDriver,
    host_is_allowed,
)
from app.agent.browser.errors import (
    ActionErrorCode,
    ActionNotAllowedError,
    DomainNotAllowedError,
)
from app.agent.perception.perceive import perceive
from app.core.config import Settings

pytestmark = pytest.mark.browser


async def goto(driver: PlaywrightDriver, html_server: str, page: str) -> ActionResult:
    return await driver.execute(NavigateAction(url=f"{html_server}/{page}"))


# ------------------------------------------------------------------ contract


def test_playwright_driver_satisfies_the_protocol() -> None:
    """The orchestrator depends on the protocol, so a fake can replace this."""
    assert isinstance(PlaywrightDriver(Settings()), BrowserDriver)


async def test_launch_applies_the_sandbox_settings(driver: PlaywrightDriver) -> None:
    state = await driver.capture_state()

    assert state.viewport_width == 1280
    assert state.viewport_height == 800


async def test_locale_and_timezone_are_pinned(driver: PlaywrightDriver, html_server: str) -> None:
    """Pinned so a laptop and CI format dates and currency identically."""
    await goto(driver, html_server, "simple_form.html")

    locale = await driver.page.evaluate("() => navigator.language")
    zone = await driver.page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")

    assert locale == "en-IN"
    # Chromium reports ICU's canonical name, which is still the legacy spelling
    # for this zone. Same zone, different label.
    assert zone in {"Asia/Kolkata", "Asia/Calcutta"}


# ------------------------------------------------------------------ actions


async def test_navigate(driver: PlaywrightDriver, html_server: str) -> None:
    result = await goto(driver, html_server, "simple_form.html")

    assert result.ok
    assert result.url_after.endswith("simple_form.html")
    assert result.duration_ms >= 0
    assert (await driver.capture_state()).title == "Simple form"


async def test_click(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(ClickAction(selector="#visible"))

    assert result.ok
    assert await driver.page.inner_text("#log") == "clicked"


async def test_type(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")

    result = await driver.execute(TypeAction(selector="#from", value="Delhi"))

    assert result.ok
    assert await driver.page.input_value("#from") == "Delhi"


async def test_type_clears_first_by_default(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")

    await driver.execute(TypeAction(selector="#to", value="Mumbai"))

    assert await driver.page.input_value("#to") == "Mumbai"


async def test_type_can_submit(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")

    await driver.execute(TypeAction(selector="#from", value="Delhi", press_enter=True))

    assert await driver.page.inner_text("#result") == "Searched: Delhi"


async def test_select(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "select.html")

    result = await driver.execute(SelectAction(selector="#sort", value="price"))

    assert result.ok
    assert await driver.page.inner_text("#chosen") == "price"


async def test_scroll(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "long_page.html")
    assert (await driver.capture_state()).scroll_y == 0

    await driver.execute(ScrollAction(direction="down", pixels=800))

    assert (await driver.capture_state()).scroll_y > 0


async def test_scroll_up_returns(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "long_page.html")
    await driver.execute(ScrollAction(direction="down", pixels=1200))

    await driver.execute(ScrollAction(direction="up", pixels=1200))

    assert (await driver.capture_state()).scroll_y == 0


async def test_extract_whole_page(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "select.html")

    result = await driver.execute(ExtractAction())

    assert result.ok
    assert result.extracted is not None
    assert "Sort results" in result.extracted


async def test_extract_one_element(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "select.html")

    result = await driver.execute(ExtractAction(selector="h1"))

    assert result.extracted == "Sort results"


async def test_wait(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(WaitAction(seconds=0.2))

    assert result.ok
    assert result.duration_ms >= 150


async def test_go_back(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")
    await driver.execute(ClickAction(selector="#next"))
    assert driver.page.url.endswith("buttons.html")

    result = await driver.execute(GoBackAction())

    assert result.ok
    assert result.url_after.endswith("simple_form.html")


async def test_done_is_terminal_and_touches_nothing(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "select.html")
    before = driver.page.url

    result = await driver.execute(DoneAction(final_answer="Rs 4,210"))

    assert result.ok
    assert result.extracted == "Rs 4,210"
    assert driver.page.url == before


# ------------------------------------------------------------------ tabs


async def test_new_tab_and_switch(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")
    first = (await driver.capture_state()).active_tab_id

    await driver.execute(NewTabAction(url=f"{html_server}/select.html"))
    state = await driver.capture_state()

    assert len(state.tab_ids) == 2
    assert state.active_tab_id != first
    assert state.url.endswith("select.html")

    await driver.execute(SwitchTabAction(tab_id=first))

    assert (await driver.capture_state()).url.endswith("simple_form.html")


async def test_close_tab(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "simple_form.html")
    await driver.execute(NewTabAction(url=f"{html_server}/select.html"))

    result = await driver.execute(CloseTabAction())

    assert result.ok
    assert len((await driver.capture_state()).tab_ids) == 1


async def test_switching_to_an_unknown_tab_fails_cleanly(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "simple_form.html")

    result = await driver.execute(SwitchTabAction(tab_id="t99"))

    assert not result.ok
    assert result.error_code is ActionErrorCode.TAB_NOT_FOUND


async def test_a_popup_becomes_a_tracked_tab(driver: PlaywrightDriver, html_server: str) -> None:
    """Spec section 4.8 mentions "open in new tab" as a chaos case."""
    await goto(driver, html_server, "buttons.html")

    await driver.execute(ClickAction(selector="#newtab"))
    await driver.execute(WaitAction(seconds=0.3))

    assert len((await driver.capture_state()).tab_ids) == 2


# ------------------------------------------------------------------ allow-list


async def test_an_action_outside_the_allow_list_never_reaches_the_browser(
    driver: PlaywrightDriver,
) -> None:
    class Sneaky:
        action = "download_everything"

    with pytest.raises(ActionNotAllowedError):
        await driver.execute(Sneaky())  # type: ignore[arg-type]


# ------------------------------------------------------------------ domains


@pytest.mark.parametrize(
    ("url", "allowed", "expected"),
    [
        ("http://localhost:8200/x", ("localhost",), True),
        ("http://localhost:9999/x", ("localhost",), True),
        ("http://localhost:8200/x", ("localhost:8200",), True),
        ("http://localhost:9999/x", ("localhost:8200",), False),
        ("http://127.0.0.1:5000/x", ("127.0.0.1",), True),
        ("https://example.test/x", ("localhost",), False),
        # Substring matching would pass this, which is the point of parsing.
        ("https://evil-localhost.test/x", ("localhost",), False),
        ("https://localhost.evil.test/x", ("localhost",), False),
        ("http://LOCALHOST:8200/x", ("localhost",), True),
        ("http://localhost/x", (), False),
        ("not-a-url", ("localhost",), False),
    ],
)
def test_host_matching(url: str, allowed: tuple[str, ...], expected: bool) -> None:
    assert host_is_allowed(url, allowed) is expected


async def test_navigating_outside_the_sandbox_is_refused(driver: PlaywrightDriver) -> None:
    """Raised, not reported: a sandbox breach must not be retried."""
    with pytest.raises(DomainNotAllowedError):
        await driver.execute(NavigateAction(url="https://example.com/"))


async def test_a_refused_navigation_does_not_move_the_page(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "simple_form.html")

    with pytest.raises(DomainNotAllowedError):
        await driver.execute(NavigateAction(url="https://example.com/"))

    assert driver.page.url.endswith("simple_form.html")


async def test_new_tab_is_also_sandboxed(driver: PlaywrightDriver) -> None:
    with pytest.raises(DomainNotAllowedError):
        await driver.execute(NewTabAction(url="https://example.com/"))


async def test_an_empty_allow_list_blocks_everything(
    browser_settings: Settings, html_server: str
) -> None:
    """Fail closed: a misconfigured list must not mean "allow all"."""
    locked = browser_settings.model_copy(update={"AGENT_ALLOWED_DOMAINS": []})
    instance = PlaywrightDriver(locked)
    await instance.launch()
    try:
        with pytest.raises(DomainNotAllowedError):
            await instance.execute(NavigateAction(url=f"{html_server}/select.html"))
    finally:
        await instance.close()


# ------------------------------------------------------------------ errors


async def test_missing_element_maps_to_element_not_found(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(ClickAction(selector="#does-not-exist"))

    assert not result.ok
    assert result.error_code is ActionErrorCode.TIMEOUT or (
        result.error_code is ActionErrorCode.ELEMENT_NOT_FOUND
    )
    assert result.error_message


async def test_a_disabled_control_is_not_interactable(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(ClickAction(selector="#disabled"))

    assert not result.ok
    assert result.error_code in {ActionErrorCode.NOT_INTERACTABLE, ActionErrorCode.TIMEOUT}


async def test_no_playwright_exception_escapes(driver: PlaywrightDriver, html_server: str) -> None:
    """The whole point of the layer: one vocabulary above the driver."""
    await goto(driver, html_server, "buttons.html")

    for action in (
        ClickAction(selector="#nope"),
        TypeAction(selector="#nope", value="x"),
        SelectAction(selector="#nope", value="x"),
        ExtractAction(selector="#nope"),
    ):
        result = await driver.execute(action)
        assert not result.ok
        assert result.error_code is not None
        assert result.error_code is not ActionErrorCode.UNKNOWN, action


async def test_a_failed_action_still_records_urls(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The verifier compares before and after even on failure."""
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(ClickAction(selector="#nope"))

    assert result.url_before.endswith("buttons.html")
    assert result.url_after.endswith("buttons.html")
    assert not result.url_changed


async def test_error_messages_are_trimmed(driver: PlaywrightDriver, html_server: str) -> None:
    """Playwright dumps page snippets; spec section 10 forbids storing those."""
    await goto(driver, html_server, "buttons.html")

    result = await driver.execute(ClickAction(selector="#nope"))

    assert result.error_message is not None
    assert len(result.error_message) <= 200
    assert "\n" not in result.error_message


# ------------------------------------------------------------------ isolation


async def test_contexts_do_not_share_cookies(browser_settings: Settings, html_server: str) -> None:
    """Spec section 4.13: one task's session can never leak into another's."""
    first = PlaywrightDriver(browser_settings)
    second = PlaywrightDriver(browser_settings)
    await first.launch()
    await second.launch()
    try:
        await first.execute(NavigateAction(url=f"{html_server}/simple_form.html"))
        await first.page.evaluate("() => { document.cookie = 'task=one; path=/'; }")
        assert "task=one" in await first.page.evaluate("() => document.cookie")

        await second.execute(NavigateAction(url=f"{html_server}/simple_form.html"))

        assert await second.page.evaluate("() => document.cookie") == ""
    finally:
        await first.close()
        await second.close()


async def test_contexts_do_not_share_local_storage(
    browser_settings: Settings, html_server: str
) -> None:
    first = PlaywrightDriver(browser_settings)
    second = PlaywrightDriver(browser_settings)
    await first.launch()
    await second.launch()
    try:
        await first.execute(NavigateAction(url=f"{html_server}/select.html"))
        await first.page.evaluate("() => localStorage.setItem('k', 'from-first')")

        await second.execute(NavigateAction(url=f"{html_server}/select.html"))

        assert await second.page.evaluate("() => localStorage.getItem('k')") is None
    finally:
        await first.close()
        await second.close()


async def test_downloads_are_refused(driver: PlaywrightDriver, html_server: str) -> None:
    """Spec section 4.13: the agent never writes to the host disk.

    Tested by behaviour rather than by reading the flag back: click a real
    download link and assert nothing lands. With accept_downloads=False
    Chromium either never offers the download or cancels the transfer, so both
    outcomes count as refused.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    await goto(driver, html_server, "download.html")

    try:
        async with driver.page.expect_download(timeout=2000) as pending:
            await driver.page.locator("#dl").click()
    except PlaywrightTimeoutError:
        return  # never offered

    download = await pending.value
    assert await download.failure() is not None, "the download was allowed to complete"


async def test_close_is_idempotent(browser_settings: Settings) -> None:
    instance = PlaywrightDriver(browser_settings)
    await instance.launch()

    await instance.close()
    await instance.close()  # must not raise


# ------------------------------------------------------- inside an iframe


@pytest.mark.browser
async def test_a_button_inside_an_iframe_can_be_clicked(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The gap the benchmark measured.

    `page.locator` only ever sees the main frame, so an element in a
    same-origin iframe was perceived perfectly well and then could not be
    touched. The whole iframe chaos configuration failed on this.
    """
    await driver.execute(NavigateAction(url=f"{html_server}/iframe.html"))
    state = await perceive(driver.page, driver)

    inner = next(element for element in state.elements if element.frame_id != "main")
    result = await driver.execute(ClickAction(selector=f'[data-agent-idx="{inner.index}"]'))

    assert result.ok, result.error_message


@pytest.mark.browser
async def test_extracting_reads_through_every_frame(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/iframe.html"))

    result = await driver.execute(ExtractAction())

    assert result.ok
    text = result.extracted or ""
    # The outer page and the frame's contents, because a page that renders
    # into an iframe has almost nothing in its own body.
    assert "Results in a frame" in text
    assert "Outside the frame" in text
    assert len(text.strip()) > len("Results in a frame Outside the frame")


@pytest.mark.browser
async def test_the_main_frame_still_wins_when_both_match(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await driver.execute(NavigateAction(url=f"{html_server}/iframe.html"))

    # A selector matching in the main frame must not start resolving into a
    # child frame: the main frame is searched first and that is the behaviour
    # every page without an iframe relies on.
    result = await driver.execute(ClickAction(selector="#outer"))

    assert result.ok


# ------------------------------------------------------------------ focus


async def test_visible_browser_raises_the_tab_it_acts_on(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """A headed run must put the active tab in front of the user."""
    raised: list[str] = []
    real = driver.page.bring_to_front

    async def spy() -> None:
        raised.append("front")
        await real()

    driver._config = replace(driver._config, headless=False)
    await driver.execute(NewTabAction(url=f"{html_server}/select.html"))
    driver.page.bring_to_front = spy  # type: ignore[method-assign]
    await driver._focus()

    assert raised == ["front"]


async def test_headless_never_tries_to_raise_a_tab(driver: PlaywrightDriver) -> None:
    called = False

    async def spy() -> None:
        nonlocal called
        called = True

    driver.page.bring_to_front = spy  # type: ignore[method-assign]
    await driver._focus()

    assert not called


# ------------------------------------------------------------------ own browser


async def test_attached_driver_uses_the_users_browser_and_leaves_it_running(
    browser_settings: Settings, html_server: str
) -> None:
    """CDP mode opens its own tab in the user's context and closes only that."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        user_browser = await pw.chromium.launch(args=["--remote-debugging-port=9333"])
        try:
            context = await user_browser.new_context()
            existing = await context.new_page()
            await existing.goto(f"{html_server}/buttons.html")

            attached = browser_settings.model_copy(
                update={"AGENT_CDP_URL": "http://127.0.0.1:9333"}
            )
            instance = PlaywrightDriver(attached)
            await instance.launch()
            try:
                assert instance.attached
                await instance.execute(NavigateAction(url=f"{html_server}/select.html"))
            finally:
                await instance.close()

            # The user's own tab and browser survive the task.
            assert user_browser.is_connected()
            assert not existing.is_closed()
        finally:
            await user_browser.close()


# ------------------------------------------------------------------ indicator


async def test_a_visible_run_draws_a_frame_that_is_hidden_for_screenshots(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The person sees the blue frame; the model's screenshot does not."""
    driver._config = replace(driver._config, headless=False)
    await goto(driver, html_server, "buttons.html")
    await driver.set_indicator("working")

    present = await driver.page.evaluate("!!document.getElementById('__baa_indicator__')")
    label = await driver.page.evaluate("document.getElementById('__baa_indicator__').textContent")
    await driver.hide_indicator(True)
    hidden = await driver.page.evaluate(
        "document.getElementById('__baa_indicator__').style.display"
    )

    assert present
    assert label == "Agent is working"
    assert hidden == "none"


async def test_waiting_switches_the_frame_to_amber_and_it_survives_navigation(
    driver: PlaywrightDriver, html_server: str
) -> None:
    driver._config = replace(driver._config, headless=False)
    await goto(driver, html_server, "buttons.html")
    await driver.set_indicator("waiting")

    await goto(driver, html_server, "select.html")
    label = await driver.page.evaluate("document.getElementById('__baa_indicator__')?.textContent")

    assert label == "Waiting for you to sign in"


async def test_headless_runs_draw_no_frame(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "buttons.html")
    await driver.set_indicator("working")

    assert not await driver.page.evaluate("!!document.getElementById('__baa_indicator__')")


# ------------------------------------------------------------------ full screen


def test_a_full_screen_visible_launch_asks_for_a_maximised_window() -> None:
    from app.agent.browser.driver import launch_args

    assert launch_args(True, False) == ["--start-maximized"]
    assert launch_args(True, True) == []  # headless has no window to maximise
    assert launch_args(False, False) == []


async def test_the_viewport_is_measured_when_the_window_has_no_fixed_size(
    browser_settings: Settings, html_server: str
) -> None:
    """A maximised window reports no viewport, so the driver reads it from the page."""
    settings = browser_settings.model_copy(update={"AGENT_FULLSCREEN": True})
    instance = PlaywrightDriver(settings)
    await instance.launch()
    try:
        await goto(instance, html_server, "buttons.html")
        size = await instance.viewport_size()
        snapshot = await instance.capture_state()
    finally:
        await instance.close()

    assert size["width"] > 0 and size["height"] > 0
    assert snapshot.viewport_width == size["width"]


# ------------------------------------------------------------------ pointer and scrolling


async def test_a_visible_run_shows_a_pointer_that_glides_to_what_it_clicks(
    driver: PlaywrightDriver, html_server: str
) -> None:
    driver._config = replace(driver._config, headless=False)
    await goto(driver, html_server, "buttons.html")
    await driver.set_indicator("working")

    result = await driver.execute(ClickAction(selector="#visible"))

    assert result.ok
    placed = await driver.page.evaluate(
        "() => { const c = document.getElementById('__baa_cursor__');"
        " return c ? c.style.transform : null; }"
    )
    box = await driver.page.locator("#visible").bounding_box()
    assert placed is not None and placed.startswith("translate(")
    assert box is not None
    x = float(placed.removeprefix("translate(").split("px")[0])
    assert abs(x - (box["x"] + box["width"] / 2)) < 2


async def test_a_visible_scroll_is_smooth_and_has_landed_before_it_returns(
    driver: PlaywrightDriver, html_server: str
) -> None:
    driver._config = replace(driver._config, headless=False)
    await goto(driver, html_server, "long_page.html")

    result = await driver.execute(ScrollAction(direction="down", pixels=400))

    assert result.ok
    assert await driver.page.evaluate("window.scrollY") >= 380


async def test_headless_runs_draw_no_pointer(driver: PlaywrightDriver, html_server: str) -> None:
    await goto(driver, html_server, "buttons.html")
    await driver.set_indicator("working")

    assert not await driver.page.evaluate("!!document.getElementById('__baa_cursor__')")


# ------------------------------------------------------------------ typing and scrolling anywhere


async def test_typing_into_a_wrapper_finds_the_field_inside_it(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "widget_input.html")

    result = await driver.execute(TypeAction(selector="#wrapper", value="02/11/2026"))

    assert result.ok, result.error_message
    assert await driver.page.input_value("#masked") == "02/11/2026"


async def test_typing_into_a_combobox_wrapper_fills_the_input_inside_it(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The wrapper carries role=combobox; fill() on it would throw."""
    await goto(driver, html_server, "combobox.html")

    result = await driver.execute(TypeAction(selector="#where-to", value="Delhi"))

    assert result.ok, result.error_message
    assert await driver.page.input_value("#where-to-input") == "Delhi"


async def test_typing_into_a_combobox_lands_in_the_popup_it_opens(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "combobox.html")

    result = await driver.execute(TypeAction(selector="#from", value="Bangalore"))

    assert result.ok, result.error_message
    assert await driver.page.input_value("#popup-input") == "Bangalore"


async def test_a_widget_that_discards_a_fill_still_gets_the_text(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The field empties anything that did not come from the keyboard."""
    await goto(driver, html_server, "widget_input.html")

    result = await driver.execute(TypeAction(selector="#masked", value="02112026"))

    assert result.ok, result.error_message
    assert await driver.page.input_value("#masked") == "02112026"


async def test_typing_into_a_button_is_a_clear_failure_not_unknown(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "widget_input.html")

    result = await driver.execute(TypeAction(selector="#swap", value="hello"))

    assert not result.ok
    assert result.error_code is ActionErrorCode.NOT_INTERACTABLE


async def test_a_page_that_scrolls_an_inner_panel_can_be_scrolled_and_measured(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "inner_scroll.html")
    before = await driver.capture_state()

    result = await driver.execute(ScrollAction(direction="down", pixels=500))
    after = await driver.capture_state()

    assert result.ok
    assert await driver.page.evaluate("document.getElementById('pane').scrollTop") >= 400
    assert before.scroll_y == 0
    assert after.scroll_y >= 400


async def test_a_picker_left_open_over_the_next_control_is_closed_before_clicking(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The calendar stays open after the date is typed and covers the Search button."""
    await goto(driver, html_server, "open_picker.html")
    await driver.execute(TypeAction(selector="#date", value="02/11/2026"))
    assert (
        await driver.page.evaluate("getComputedStyle(document.getElementById('picker')).display")
        == "block"
    )

    result = await driver.execute(ClickAction(selector="#go"))

    assert result.ok, result.error_message
    assert await driver.page.text_content("#log") == "searched"


# ------------------------------------------------------------------ links that open a new tab


async def test_a_link_that_opens_a_new_tab_is_followed(
    driver: PlaywrightDriver, html_server: str
) -> None:
    """The listing opens beside the results; the agent must look at the listing."""
    await goto(driver, html_server, "popup_link.html")

    result = await driver.execute(ClickAction(selector="#blank"))

    assert result.ok, result.error_message
    assert len(driver.sessions.tab_ids) == 2
    assert driver.page.url.endswith("detail.html")
    assert result.url_after.endswith("detail.html")


async def test_a_script_that_opens_a_window_is_followed_too(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "popup_link.html")

    result = await driver.execute(ClickAction(selector="#script"))

    assert result.ok, result.error_message
    assert driver.page.url.endswith("detail.html")


async def test_an_ordinary_click_stays_on_the_page(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "popup_link.html")

    result = await driver.execute(ClickAction(selector="#same"))

    assert result.ok
    assert len(driver.sessions.tab_ids) == 1
    assert driver.page.url.endswith("popup_link.html")


async def test_a_new_tab_outside_the_allowed_domains_is_closed_and_refused(
    browser_settings: Settings, html_server: str
) -> None:
    import contextlib

    locked = browser_settings.model_copy(update={"AGENT_ALLOWED_DOMAINS": ["127.0.0.1:1"]})
    instance = PlaywrightDriver(locked)
    await instance.launch()
    try:
        # The first page is outside the list as well, so open it without the guard.
        await instance.page.goto(f"{html_server}/popup_link.html")
        with contextlib.suppress(Exception):
            await instance.execute(ClickAction(selector="#blank"))
        assert len(instance.sessions.tab_ids) == 1
    finally:
        await instance.close()


async def test_a_window_closed_under_the_task_is_reported_as_closed_not_unlaunched(
    driver: PlaywrightDriver, html_server: str
) -> None:
    from app.agent.browser.errors import BrowserClosedError

    await goto(driver, html_server, "buttons.html")
    await driver.page.close()
    import asyncio

    await asyncio.sleep(0.2)

    with pytest.raises(BrowserClosedError):
        _ = driver.page

    # An action on a closed window raises the same error rather than a vague one.
    with pytest.raises(BrowserClosedError):
        await driver.execute(ClickAction(selector="#visible"))


# ------------------------------------------------------- a site that says no


async def test_a_site_that_refuses_this_browser_is_tried_in_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 for Chromium, 200 for Firefox: the driver moves to Firefox and carries on."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setattr("app.agent.browser.driver.RENDER_WAIT_S", 1.0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            firefox = "Firefox" in (self.headers.get("User-Agent") or "")
            body = b"<title>in</title><h1>welcome</h1>" if firefox else b"denied"
            self.send_response(200 if firefox else 403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    settings = Settings(
        AGENT_HEADLESS=True,
        AGENT_BROWSER_FALLBACKS=["firefox"],
        AGENT_ALLOWED_DOMAINS=["127.0.0.1"],
    )
    driver = PlaywrightDriver(settings)
    try:
        await driver.launch()
        result = await driver.execute(NavigateAction(url=url))

        assert result.ok, result.error_message
        assert "Firefox" in await driver.page.evaluate("navigator.userAgent")
        assert "welcome" in await driver.page.inner_text("h1")
    finally:
        await driver.close()
        server.shutdown()


async def test_a_refusal_with_no_other_browser_to_try_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setattr("app.agent.browser.driver.RENDER_WAIT_S", 1.0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(
        AGENT_HEADLESS=True, AGENT_BROWSER_FALLBACKS=[], AGENT_ALLOWED_DOMAINS=["127.0.0.1"]
    )
    driver = PlaywrightDriver(settings)
    try:
        await driver.launch()
        result = await driver.execute(NavigateAction(url=f"http://127.0.0.1:{server.server_port}/"))

        assert not result.ok
        assert "403" in (result.error_message or "")
    finally:
        await driver.close()
        server.shutdown()


async def test_a_403_that_still_draws_the_page_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed-out visitors often get a 403 and the page anyway; the agent should look at it."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setattr("app.agent.browser.driver.RENDER_WAIT_S", 1.0)
    body = (
        "<title>Practice problem</title><p>" + "Sign in to see the full problem. " * 20
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(
        AGENT_HEADLESS=True, AGENT_BROWSER_FALLBACKS=[], AGENT_ALLOWED_DOMAINS=["127.0.0.1"]
    )
    driver = PlaywrightDriver(settings)
    try:
        await driver.launch()
        result = await driver.execute(NavigateAction(url=f"http://127.0.0.1:{server.server_port}/"))

        assert result.ok, result.error_message
        assert await driver.page.title() == "Practice problem"
    finally:
        await driver.close()
        server.shutdown()


# --------------------------------------------------------------- code editors


async def test_a_program_is_inserted_whole_not_typed_into_an_auto_indenting_editor(
    driver: PlaywrightDriver, html_server: str
) -> None:
    program = (
        "def solve(a, b):\n"
        "    if a > b:\n"
        "        return a - b\n"
        "    return a + b\n"
        "\n"
        "print(solve(*map(int, input().split())))"
    )
    await goto(driver, html_server, "code_editor.html")

    result = await driver.execute(TypeAction(selector="#editor", value=program, clear_first=True))

    assert result.ok, result.error_message
    assert await driver.page.input_value("#editor") == program


async def test_inserting_a_program_replaces_what_the_editor_held(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "code_editor.html")
    await driver.page.fill("#editor", "old = 1\nold = 2")

    await driver.execute(TypeAction(selector="#editor", value="x = 1\ny = 2", clear_first=True))

    assert await driver.page.input_value("#editor") == "x = 1\ny = 2"


async def test_unavailable_fallback_preserves_existing_page(
    driver: PlaywrightDriver,
    html_server: str,
) -> None:
    await goto(driver, html_server, "buttons.html")
    original_page = driver.page
    await driver.page.evaluate("window.savedState = 'preserved'")
    assert not await driver._switch_browser("not-an-installed-browser")
    assert driver.page is original_page
    assert await driver.page.evaluate("window.savedState") == "preserved"
    assert await driver.page.title() == "Buttons"


@pytest.mark.parametrize("initial,value", [("New ", "Delhi"), ("", ""), ("दिल्ली", " स्टेशन")])
async def test_input_appends_without_erasing_existing_text(
    driver: PlaywrightDriver, initial: str, value: str
) -> None:
    await driver.page.set_content('<input id="address">')
    await driver.page.fill("#address", initial)
    result = await driver.execute(TypeAction(selector="#address", value=value, clear_first=False))
    assert result.ok, result.error_message
    assert await driver.page.input_value("#address") == initial + value


async def test_input_truncation_is_reported_before_enter(driver: PlaywrightDriver) -> None:
    await driver.page.set_content(
        '<input id="address" maxlength="3" onkeydown="if(event.key===\'Enter\') window.sent=true">'
    )
    result = await driver.execute(
        TypeAction(selector="#address", value="New Delhi", press_enter=True)
    )
    assert not result.ok
    assert result.error_code is ActionErrorCode.NOT_INTERACTABLE
    assert not await driver.page.evaluate("!!window.sent")


async def test_wrapper_ignores_hidden_input_clones(driver: PlaywrightDriver) -> None:
    await driver.page.set_content(
        '<div id="wrapper"><input style="display:none"><input id="visible"></div>'
    )
    result = await driver.execute(TypeAction(selector="#wrapper", value="Delhi"))
    assert result.ok, result.error_message
    assert await driver.page.input_value("#visible") == "Delhi"


async def test_noneditable_click_does_not_erase_an_unrelated_field(
    driver: PlaywrightDriver,
) -> None:
    await driver.page.set_content('<input id="keep" value="Keep me"><div id="map">Map</div>')
    await driver.page.focus("#keep")
    result = await driver.execute(TypeAction(selector="#map", value="New Delhi"))
    assert not result.ok
    assert await driver.page.input_value("#keep") == "Keep me"


async def test_scroll_uses_small_focused_panel_even_with_scrollable_page(
    driver: PlaywrightDriver,
) -> None:
    await driver.page.set_content(
        '<body style="height:3000px"><div id="rides" style="height:180px;width:250px;'
        'overflow:auto"><input id="pickup"><div style="height:1500px">Rides</div></div>'
    )
    await driver.page.focus("#pickup")
    result = await driver.execute(ScrollAction(pixels=400))
    assert result.ok
    assert await driver.page.evaluate("document.querySelector('#rides').scrollTop") == 400
    assert await driver.page.evaluate("window.scrollY") == 0
    assert (await driver.capture_state()).scroll_y == 400


async def test_scroll_uses_dialog_instead_of_background(driver: PlaywrightDriver) -> None:
    await driver.page.set_content(
        '<body style="height:3000px"><div role="dialog" id="dialog" style="position:fixed;'
        'top:40px;left:40px;width:250px;height:180px;overflow:auto;background:white">'
        '<div style="height:1500px">Options</div></div>'
    )
    result = await driver.execute(ScrollAction(pixels=400))
    assert result.ok
    assert await driver.page.evaluate("document.querySelector('#dialog').scrollTop") == 400
    assert await driver.page.evaluate("window.scrollY") == 0
    assert (await driver.capture_state()).scroll_y == 400


async def test_scroll_uses_autocomplete_popup_controlled_by_focused_field(
    driver: PlaywrightDriver,
) -> None:
    await driver.page.set_content(
        '<body style="height:3000px"><input id="address" aria-expanded="true" '
        'aria-controls="suggestions"><div id="suggestions" role="listbox" '
        'style="width:250px;height:180px;overflow:auto"><div style="height:1500px">'
        "Address suggestions</div></div>"
    )
    await driver.page.focus("#address")
    result = await driver.execute(ScrollAction(pixels=400))
    assert result.ok
    assert await driver.page.evaluate("document.querySelector('#suggestions').scrollTop") == 400
    assert await driver.page.evaluate("window.scrollY") == 0


async def test_cab_details_and_ride_options_without_booking(
    driver: PlaywrightDriver, html_server: str
) -> None:
    await goto(driver, html_server, "cab_form.html")
    result = await driver.execute(TypeAction(selector="#pickup", value="New Delhi Railway Station"))
    assert result.ok, result.error_message
    assert (await driver.execute(ClickAction(selector="#suggestion"))).ok
    assert await driver.page.input_value("#pickup") == "New Delhi Railway Station"
    result = await driver.execute(TypeAction(selector="#dropoff", value="India Gate"))
    assert result.ok, result.error_message
    assert (await driver.execute(ClickAction(selector="#see-rides"))).ok
    max_scroll = await driver.page.locator("#rides").evaluate(
        "el => el.scrollHeight - el.clientHeight"
    )
    assert max_scroll > 0
    result = await driver.execute(ScrollAction(pixels=600))
    assert result.ok, result.error_message
    assert (await driver.capture_state()).scroll_y == min(600, max_scroll)
    assert await driver.page.evaluate("window.scrollY") == 0
    assert (await driver.execute(ClickAction(selector="#ride-option"))).ok
    assert await driver.page.evaluate("window.selectedRide") == "Demo ride"
    assert not await driver.page.evaluate("!!window.booked")
