"""Required popup choices must survive background clicks and automatic cleanup."""

import pytest

from app.agent.browser.actions import ClickAction, ScrollAction, TypeAction
from app.agent.browser.driver import PlaywrightDriver
from app.agent.browser.errors import ActionErrorCode
from app.agent.llm.mock import MockLLM
from app.agent.perception.overlays import requires_popup_attention, safe_closers
from app.agent.perception.perceive import perceive
from app.agent.planner.planner import Planner
from app.agent.recovery.strategies import DismissOverlay


@pytest.mark.browser
@pytest.mark.parametrize("suffix", ["", "?plain=1"])
async def test_required_popup_is_read_filled_and_completed_before_background(
    driver: PlaywrightDriver, html_server: str, suffix: str
) -> None:
    await driver.page.goto(f"{html_server}/required_popup.html{suffix}")
    await driver.execute(ClickAction(selector="#book"))
    state = await perceive(driver.page)
    assert state.modal_open
    assert requires_popup_attention(state)
    assert "Select language and format" in state.modal_text
    assert "POPUP TEXT" in state.planner_summary
    assert safe_closers(state) == []
    assert DismissOverlay.closers(state) == []
    book = next(e for e in state.elements if e.name == "Book tickets")
    assert book.covered and not book.in_modal
    assert (
        Planner(MockLLM())._validate(
            {
                "action": "click",
                "targetIndex": book.index,
                "target": "Book tickets",
                "reasoning": "Try the background again",
                "confidence": 1,
                "expectedOutcome": {"type": "dom_changed", "value": ""},
            },
            state,
            advisory=False,
        )
        is not None
    )

    # Backstop for stale cached actions: never Escape or click through the modal.
    result = await driver.execute(ClickAction(selector="#book"))
    assert not result.ok and result.error_code is ActionErrorCode.NOT_INTERACTABLE
    assert await driver.page.locator("#veil").is_visible()
    assert await driver.page.locator("body").get_attribute("data-opens") == "1"
    assert (await driver.execute(TypeAction(selector="#seats", value="2"))).ok
    assert (await driver.execute(ClickAction(selector="#english"))).ok
    assert not (await perceive(driver.page)).modal_open
    assert (await driver.execute(ClickAction(selector="#book"))).ok
    assert await driver.page.locator("#result").inner_text() == "Screenings ready: English 3D for 2"
    assert await driver.page.locator("body").get_attribute("data-opens") == "2"


@pytest.mark.browser
async def test_a_fullscreen_app_shell_is_not_a_popup(driver: PlaywrightDriver) -> None:
    await driver.page.set_content(
        '<div style="position:fixed;inset:0;z-index:1000;background:white">'
        '<button id="ordinary">Ordinary page action</button></div>'
    )
    assert not (await perceive(driver.page)).modal_open
    assert (await driver.execute(ClickAction(selector="#ordinary"))).ok


@pytest.mark.browser
@pytest.mark.parametrize("suffix", ["", "?plain=1"])
async def test_scroll_stays_inside_the_required_popup(
    driver: PlaywrightDriver, html_server: str, suffix: str
) -> None:
    await driver.page.goto(f"{html_server}/required_popup.html{suffix}")
    await driver.page.evaluate(
        """() => {
          document.body.style.minHeight = '2000px';
          const panel = document.getElementById('panel');
          panel.style.height = '120px'; panel.style.overflowY = 'auto';
        }"""
    )
    await driver.execute(ClickAction(selector="#book"))
    assert (await driver.execute(ScrollAction(pixels=120))).ok
    assert await driver.page.locator("#panel").evaluate("el => el.scrollTop") > 0
    assert await driver.page.evaluate("window.scrollY") == 0
    assert await driver.page.locator("#veil").is_visible()
