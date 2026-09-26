"""B14: the decision schema and its cross-field rules (spec section 4.5)."""

import pytest
from pydantic import ValidationError

from app.agent.planner.schema import (
    DECISION_JSON_SCHEMA,
    Decision,
    ExpectedOutcome,
    decision_json_schema,
)
from app.domain.enums import ActionType, RiskLevel, VerificationMethod


def decision(**overrides: object) -> Decision:
    payload: dict[str, object] = {
        "action": "click",
        "targetIndex": 3,
        "target": "Search flights",
        "reasoning": "both city fields are filled",
        "expectedOutcome": {"type": "dom_changed", "value": ""},
    }
    return Decision.model_validate(payload | overrides)


# ------------------------------------------------------------------ shape


def test_a_full_decision_parses() -> None:
    parsed = Decision.model_validate(
        {
            "action": "type",
            "targetIndex": 1,
            "target": "To",
            "value": "Bengaluru",
            "tabId": "t1",
            "reasoning": "the goal names Bengaluru as the destination",
            "expectedOutcome": {"type": "value_equals", "value": "Bengaluru"},
            "riskLevel": "LOW",
            "confidence": 0.92,
            "requiresApproval": False,
            "needsVision": False,
            "finalAnswer": None,
        }
    )

    assert parsed.action is ActionType.TYPE
    assert parsed.target_index == 1
    assert parsed.expected_outcome.type is VerificationMethod.VALUE_EQUALS
    assert parsed.risk_level is RiskLevel.LOW


def test_camel_case_is_the_wire_form() -> None:
    """Spec section 4.5 and the section 6.1 events are both camelCase."""
    dumped = decision().model_dump(by_alias=True)

    assert "targetIndex" in dumped
    assert "expectedOutcome" in dumped
    assert "requiresApproval" in dumped
    assert "target_index" not in dumped


def test_an_expected_outcome_is_required() -> None:
    """Without one the step cannot be verified, so it cannot be trusted."""
    with pytest.raises(ValidationError, match=r"expectedOutcome|expected_outcome"):
        Decision.model_validate({"action": "scroll"})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        decision(somethingElse="x")


def test_decisions_are_immutable() -> None:
    with pytest.raises(ValidationError):
        decision().action = ActionType.DONE  # type: ignore[misc]


@pytest.mark.parametrize("confidence", [-0.1, 1.1, 2])
def test_confidence_is_bounded(confidence: float) -> None:
    with pytest.raises(ValidationError):
        decision(confidence=confidence)


def test_a_negative_index_is_rejected() -> None:
    with pytest.raises(ValidationError):
        decision(targetIndex=-1)


def test_reasoning_is_length_capped() -> None:
    with pytest.raises(ValidationError):
        decision(reasoning="x" * 5000)


# ------------------------------------------------------------ cross-field


@pytest.mark.parametrize("action", ["click", "type", "select"])
def test_element_actions_need_an_index(action: str) -> None:
    """Schema-valid and useless: an action on nothing in particular."""
    payload: dict[str, object] = {"action": action, "value": "x", "targetIndex": None}
    with pytest.raises(ValidationError, match="requires targetIndex"):
        decision(**payload)


@pytest.mark.parametrize("action", ["type", "select"])
def test_value_actions_need_a_value(action: str) -> None:
    with pytest.raises(ValidationError, match="requires a value"):
        decision(action=action, value=None)


def test_a_blank_value_does_not_count(action: str = "type") -> None:
    with pytest.raises(ValidationError, match="requires a value"):
        decision(action=action, value="   ")


def test_navigate_needs_a_url() -> None:
    with pytest.raises(ValidationError, match="http or https"):
        decision(action="navigate", targetIndex=None, value="example.test")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"])
def test_navigate_rejects_dangerous_schemes(url: str) -> None:
    with pytest.raises(ValidationError, match="http or https"):
        decision(action="navigate", targetIndex=None, value=url)


def test_navigate_accepts_http() -> None:
    parsed = decision(action="navigate", targetIndex=None, value="https://sky.test/results")

    assert parsed.value == "https://sky.test/results"


def test_done_needs_a_final_answer() -> None:
    """Otherwise the task ends having answered nothing."""
    with pytest.raises(ValidationError, match="requires finalAnswer"):
        decision(action="done", targetIndex=None)


def test_done_with_an_answer_is_terminal() -> None:
    parsed = decision(action="done", targetIndex=None, finalAnswer="Rs 4,210")

    assert parsed.is_terminal
    assert not decision().is_terminal


def test_switch_tab_needs_a_tab_id() -> None:
    with pytest.raises(ValidationError, match="requires tabId"):
        decision(action="switch_tab", targetIndex=None)


@pytest.mark.parametrize("action", ["scroll", "wait", "go_back", "extract"])
def test_actions_without_a_target_are_fine(action: str) -> None:
    assert decision(action=action, targetIndex=None).action.value == action


# ------------------------------------------------------------ json schema


def test_the_json_schema_matches_the_model_exactly() -> None:
    """A drift here means the model is asked for a shape we then reject."""
    published = set(DECISION_JSON_SCHEMA["properties"])
    model_fields = {(field.alias or name) for name, field in Decision.model_fields.items()}

    assert published == model_fields


def test_every_property_is_required_for_strict_modes() -> None:
    """OpenAI strict mode rejects a schema with optional properties."""
    schema = decision_json_schema()

    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_nullable_fields_are_spelled_as_unions() -> None:
    """Strict modes want ["string", "null"], not anyOf."""
    properties = DECISION_JSON_SCHEMA["properties"]

    assert properties["targetIndex"]["type"] == ["integer", "null"]
    assert properties["finalAnswer"]["type"] == ["string", "null"]


def test_the_action_enum_is_the_allow_list() -> None:
    assert set(DECISION_JSON_SCHEMA["properties"]["action"]["enum"]) == {
        member.value for member in ActionType
    }


def test_the_outcome_enum_matches_the_verifier() -> None:
    outcome = DECISION_JSON_SCHEMA["properties"]["expectedOutcome"]
    assert set(outcome["properties"]["type"]["enum"]) == {
        member.value for member in VerificationMethod
    }
    assert outcome["additionalProperties"] is False


def test_the_schema_descriptions_carry_instructions() -> None:
    """The schema a model sees is part of the prompt."""
    properties = DECISION_JSON_SCHEMA["properties"]

    assert "never invent" in properties["targetIndex"]["description"].lower()
    assert "irreversible" in properties["requiresApproval"]["description"].lower()


def test_an_outcome_value_may_be_empty() -> None:
    """dom_changed and new_tab_opened need no comparison value."""
    assert ExpectedOutcome(type=VerificationMethod.DOM_CHANGED).value == ""
