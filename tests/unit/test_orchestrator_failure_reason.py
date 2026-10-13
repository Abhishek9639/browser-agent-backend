"""The failure reason says why an action did not run, not just that it did not."""

from app.agent.browser.driver import ActionResult
from app.agent.browser.errors import ActionErrorCode
from app.agent.orchestrator import _why_not_run


def _failed(code: ActionErrorCode | None, message: str | None) -> ActionResult:
    return ActionResult(
        ok=False,
        duration_ms=1,
        url_before="about:blank",
        url_after="about:blank",
        error_code=code,
        error_message=message,
    )


def test_a_navigation_failure_names_the_address_problem() -> None:
    reason = _why_not_run(
        _failed(
            ActionErrorCode.NAVIGATION_FAILED,
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.internshaala.com/",
        )
    )

    assert "check the address" in reason
    assert "ERR_NAME_NOT_RESOLVED" in reason


def test_an_unknown_failure_still_reads_as_a_sentence() -> None:
    assert _why_not_run(_failed(None, None)) == "The action did not run."
