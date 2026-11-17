"""A decision's pressEnter reaches the typed action, and only for type."""

from app.agent.browser.action_builder import build_action
from app.agent.browser.actions import TypeAction
from app.agent.planner.schema import Decision


def _decision(**overrides: object) -> Decision:
    payload: dict[str, object] = {
        "action": "type",
        "targetIndex": 7,
        "target": "Search",
        "value": "fullstack internship",
        "reasoning": "the search box is empty",
        "expectedOutcome": {"type": "value_equals", "value": "fullstack internship"},
    }
    return Decision.model_validate(payload | overrides)


def test_press_enter_defaults_to_off() -> None:
    action = build_action(_decision(), None, selector='[data-agent-idx="7"]')
    assert isinstance(action, TypeAction)
    assert action.press_enter is False


def test_press_enter_is_carried_onto_the_type_action() -> None:
    action = build_action(_decision(pressEnter=True), None, selector='[data-agent-idx="7"]')
    assert isinstance(action, TypeAction)
    assert action.press_enter is True
