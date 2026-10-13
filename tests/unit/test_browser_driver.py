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
