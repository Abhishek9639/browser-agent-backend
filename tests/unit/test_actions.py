"""B08: the action allow-list as typed models (spec section 4.4)."""

import pytest
from pydantic import ValidationError

from app.agent.browser.actions import (
    ACTION_MODELS,
    MAX_WAIT_SECONDS,
    ClickAction,
    NavigateAction,
    WaitAction,
    parse_action,
)
from app.agent.browser.errors import ActionErrorCode, ActionNotAllowedError
from app.domain.enums import ActionType


def test_every_allow_listed_action_has_a_model() -> None:
    """The union is the allow-list; a gap here means an action cannot run."""
    assert set(ACTION_MODELS) == set(ActionType)


@pytest.mark.parametrize("action_type", list(ActionType))
def test_each_model_declares_its_own_action(action_type: ActionType) -> None:
    model = ACTION_MODELS[action_type]
    assert model.model_fields["action"].default is action_type


# ------------------------------------------------------------------ allow-list


@pytest.mark.parametrize(
    "name",
    ["sudo_rm_rf", "eval", "download", "screenshot", "navigate_to", "CLICK", ""],
)
def test_unknown_actions_are_rejected(name: str) -> None:
    with pytest.raises(ActionNotAllowedError) as caught:
        parse_action({"action": name})

    assert caught.value.code is ActionErrorCode.ACTION_NOT_ALLOWED


def test_a_missing_action_name_is_rejected() -> None:
    with pytest.raises(ActionNotAllowedError):
        parse_action({"url": "https://example.test"})


def test_a_non_string_action_name_is_rejected() -> None:
    with pytest.raises(ActionNotAllowedError):
        parse_action({"action": 42})


def test_allow_list_failure_is_distinct_from_validation_failure() -> None:
    """An invented action is never worth a repair retry; a bad field is."""
    with pytest.raises(ActionNotAllowedError):
        parse_action({"action": "teleport", "url": "https://x.test"})
    with pytest.raises(ValidationError):
        parse_action({"action": "navigate"})


# ------------------------------------------------------------------ navigate


@pytest.mark.parametrize(
    "url",
    ["https://example.test", "http://localhost:8200/skyfare", "https://a.test/path?q=1#x"],
)
def test_navigate_accepts_http_urls(url: str) -> None:
    assert parse_action({"action": "navigate", "url": url}).url == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "ftp://files.test/x",
        "about:blank",
        "//example.test",
        "example.test",
        "",
    ],
)
def test_navigate_rejects_everything_else(url: str) -> None:
    """file:// reads the host disk, javascript: runs in page context, and
    data: smuggles a document past the domain allow-list."""
    with pytest.raises(ValidationError):
        parse_action({"action": "navigate", "url": url})


# ------------------------------------------------------------------ wait


def test_wait_accepts_up_to_the_maximum() -> None:
    assert parse_action({"action": "wait", "seconds": MAX_WAIT_SECONDS}).seconds == 10


@pytest.mark.parametrize("seconds", [0, -1, 10.1, 60, 3600])
def test_wait_rejects_out_of_range(seconds: float) -> None:
    """A plan that wants longer is stalling; the task budget is 300s."""
    with pytest.raises(ValidationError):
        parse_action({"action": "wait", "seconds": seconds})


def test_wait_has_a_sensible_default() -> None:
    assert parse_action({"action": "wait"}).seconds == 1.0


# ------------------------------------------------------------------ others


def test_type_defaults() -> None:
    action = parse_action({"action": "type", "selector": "#from", "value": "Delhi"})

    assert action.clear_first is True
    assert action.press_enter is False


def test_scroll_defaults_and_bounds() -> None:
    assert parse_action({"action": "scroll"}).direction == "down"
    with pytest.raises(ValidationError):
        parse_action({"action": "scroll", "direction": "sideways"})
    with pytest.raises(ValidationError):
        parse_action({"action": "scroll", "pixels": 0})


def test_extract_selector_is_optional() -> None:
    assert parse_action({"action": "extract"}).selector is None


def test_new_tab_url_must_also_be_http() -> None:
    with pytest.raises(ValidationError):
        parse_action({"action": "new_tab", "url": "file:///etc/passwd"})
    assert parse_action({"action": "new_tab"}).url is None


def test_done_may_carry_a_final_answer() -> None:
    assert parse_action({"action": "done", "final_answer": "Rs 4,210"}).final_answer == "Rs 4,210"


def test_actions_reject_unknown_fields() -> None:
    """A typo in a model's output is an error, not a silently ignored field."""
    with pytest.raises(ValidationError):
        parse_action({"action": "click", "selector": "#a", "targetIndex": 4})


def test_actions_are_immutable() -> None:
    """A step row must record what actually ran."""
    action = ClickAction(selector="#a")

    with pytest.raises(ValidationError):
        action.selector = "#b"  # type: ignore[misc]


def test_selector_is_required_for_click() -> None:
    with pytest.raises(ValidationError):
        parse_action({"action": "click"})


def test_navigate_url_is_trimmed() -> None:
    assert NavigateAction(url="  https://x.test  ").url == "https://x.test"


def test_wait_model_can_be_built_directly() -> None:
    assert WaitAction().action is ActionType.WAIT
