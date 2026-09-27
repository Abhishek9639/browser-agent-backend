"""Waiting for a page to stop moving (spec section 4.7).

The point of these is the race, not the helper. A click that triggers a render
is not finished when `click()` returns, and verification that reads the page at
that moment fails a step that worked.
"""

import pytest

from app.agent.browser.actions import ClickAction, NavigateAction
from app.agent.browser.driver import PlaywrightDriver
from app.agent.browser.stability import settle
from app.agent.perception.perceive import perceive
from app.agent.planner.schema import Decision
from app.agent.verification.verifier import DeterministicVerifier

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
