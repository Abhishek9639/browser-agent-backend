"""Browser driver errors.

Playwright exceptions never leave the driver. Everything the browser can do
wrong is mapped onto one of these codes, so the orchestrator, the recovery
ladder and the ``task_steps`` rows all speak the same small vocabulary
regardless of which automation library sits underneath.
"""

from enum import StrEnum

__all__ = [
    "ActionErrorCode",
    "ActionNotAllowedError",
    "BrowserClosedError",
    "BrowserError",
    "DomainNotAllowedError",
]


class ActionErrorCode(StrEnum):
    """Why an action failed. Stored on the step and shown in the activity log."""

    TIMEOUT = "TIMEOUT"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    NOT_INTERACTABLE = "NOT_INTERACTABLE"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    DOMAIN_NOT_ALLOWED = "DOMAIN_NOT_ALLOWED"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    TAB_LIMIT_REACHED = "TAB_LIMIT_REACHED"
    # Nothing should reach this. If it does, the driver has a gap in its
    # mapping, which is a bug rather than a page behaving badly.
    UNKNOWN = "UNKNOWN"


class BrowserError(Exception):
    """Base for failures the driver raises rather than reports.

    Most failures come back as an unsuccessful ``ActionResult``, because they
    are things a page did and the recovery ladder may be able to work around.
    These are different: they mean the request itself was not allowed, so
    retrying it is pointless and executing it would be unsafe.
    """

    code: ActionErrorCode = ActionErrorCode.UNKNOWN

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ActionNotAllowedError(BrowserError):
    """An action outside the spec section 4.4 allow-list.

    Raised before the browser is touched. An LLM that invents an action, or a
    bug that smuggles one through, must not reach a real page.
    """

    code = ActionErrorCode.ACTION_NOT_ALLOWED


class BrowserClosedError(BrowserError):
    """Every tab the task had is gone: the window was closed under it.

    Not something the agent can retry its way out of, and not a bug in the
    task, so it is reported as what it is rather than as an internal error.
    """

    code = ActionErrorCode.TAB_NOT_FOUND


class DomainNotAllowedError(BrowserError):
    """Navigation outside ``AGENT_ALLOWED_DOMAINS`` (spec section 4.13).

    The sandbox boundary. Page content is untrusted and can try to steer the
    agent somewhere else, so this is checked against the resolved URL every
    time rather than trusted from the plan.
    """

    code = ActionErrorCode.DOMAIN_NOT_ALLOWED
