"""Turning a planner decision into the typed action the driver runs.

Shared by the loop and by recovery, which needs to replay the same action
against a different selector when the first one did not find its element.
"""

from typing import Final

from pydantic import BaseModel

from app.agent.browser.actions import parse_action
from app.agent.perception.dom_extractor import STAMP_ATTRIBUTE, ElementInfo
from app.agent.planner.schema import Decision
from app.domain.enums import ActionType

__all__ = ["SELECTOR_ACTIONS", "build_action", "selector_for"]

# The actions that name an element, and so are the ones recovery can retry
# against a different locator. Navigating somewhere again is not recovery.
SELECTOR_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}
)


def selector_for(element: ElementInfo | None) -> str | None:
    """Target by the collector's stamp, not by a stored CSS path.

    The stamp survives a re-render that renames every class, which is one of
    the chaos cases in spec section 12.
    """
    if element is None:
        return None
    return f'[{STAMP_ATTRIBUTE}="{element.index}"]'


def build_action(
    decision: Decision,
    element: ElementInfo | None,
    *,
    selector: str | None = None,
) -> BaseModel:
    """The typed action for a decision, optionally against a given selector.

    Routed through ``parse_action`` rather than constructed directly, so the
    section 4.11 allow-list check runs here as well. An action name that is not
    one of the twelve is refused before a browser is involved.
    """
    target = selector if selector is not None else selector_for(element)
    payload: dict[str, object] = {"action": decision.action.value}

    match decision.action:
        case ActionType.NAVIGATE | ActionType.NEW_TAB:
            payload["url"] = decision.value
        case ActionType.CLICK:
            payload["selector"] = target
        case ActionType.TYPE | ActionType.SELECT:
            payload["selector"] = target
            payload["value"] = decision.value
        case ActionType.SCROLL:
            payload["direction"] = decision.value if decision.value in {"up", "down"} else "down"
        case ActionType.EXTRACT:
            payload["selector"] = target
        case ActionType.WAIT:
            payload["seconds"] = _wait_seconds(decision.value)
        case ActionType.SWITCH_TAB | ActionType.CLOSE_TAB:
            payload["tab_id"] = decision.tab_id
        case ActionType.DONE:
            payload["final_answer"] = decision.final_answer
        case ActionType.GO_BACK:
            pass

    return parse_action({key: value for key, value in payload.items() if value is not None})


def _wait_seconds(value: str | None) -> float:
    try:
        return float(value) if value else 1.0
    except ValueError:
        return 1.0
