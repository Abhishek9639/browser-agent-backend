"""B04: the domain enums are part of the storage and wire contract."""

import pytest

from app.domain.enums import (
    ActionType,
    ApprovalStatus,
    RecoveryOutcome,
    RiskLevel,
    StepStatus,
    TaskStatus,
    UserRole,
    VerificationMethod,
)


def test_user_roles_match_the_spec() -> None:
    assert {r.value for r in UserRole} == {"TASK_SUBMITTER", "APPROVER", "ADMIN"}


def test_task_statuses_match_section_4_3() -> None:
    assert {s.value for s in TaskStatus} == {
        "SUBMITTED",
        "PLANNING",
        "ACTING",
        "VERIFYING",
        "RECOVERING",
        "AWAITING_APPROVAL",
        "COMPLETED",
        "EXECUTION_FAILED",
        "REJECTED",
        "CANCELLED",
        "TIMED_OUT",
    }


def test_terminal_statuses_match_section_4_3() -> None:
    assert {s.value for s in TaskStatus if s.is_terminal} == {
        "COMPLETED",
        "EXECUTION_FAILED",
        "REJECTED",
        "CANCELLED",
        "TIMED_OUT",
    }


def test_in_flight_statuses_are_not_terminal() -> None:
    assert not TaskStatus.ACTING.is_terminal
    assert not TaskStatus.AWAITING_APPROVAL.is_terminal
    assert not TaskStatus.RECOVERING.is_terminal


def test_action_allow_list_matches_section_4_4_exactly() -> None:
    """Spec section 4.4: anything not in this list is rejected before execution."""
    assert [a.value for a in ActionType] == [
        "navigate",
        "click",
        "type",
        "scroll",
        "select",
        "extract",
        "wait",
        "go_back",
        "new_tab",
        "switch_tab",
        "close_tab",
        "done",
    ]


def test_verification_methods_match_the_section_4_5_schema() -> None:
    assert {m.value for m in VerificationMethod} == {
        "url_contains",
        "element_visible",
        "text_present",
        "value_equals",
        "dom_changed",
        "new_tab_opened",
    }


def test_approval_statuses_match_the_spec() -> None:
    assert {s.value for s in ApprovalStatus} == {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}


def test_step_statuses_match_decision_a8() -> None:
    assert {s.value for s in StepStatus} == {
        "PENDING",
        "AWAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "RECOVERING",
        "VERIFIED",
        "FAILED",
        "SKIPPED",
    }


def test_risk_levels_are_ordered_low_medium_high() -> None:
    assert [r.value for r in RiskLevel] == ["LOW", "MEDIUM", "HIGH"]


def test_recovery_outcomes() -> None:
    assert {o.value for o in RecoveryOutcome} == {"SUCCEEDED", "FAILED", "SKIPPED"}


@pytest.mark.parametrize(
    "enum_type",
    [
        ActionType,
        ApprovalStatus,
        RecoveryOutcome,
        RiskLevel,
        StepStatus,
        TaskStatus,
        UserRole,
        VerificationMethod,
    ],
)
def test_values_are_unique_within_each_enum(enum_type: type) -> None:
    members = list(enum_type)
    assert len({m.value for m in members}) == len(members)
