"""The task lifecycle table, checked against spec section 4.3."""

from dataclasses import dataclass

import pytest

from app.domain.enums import TaskStatus
from app.domain.state_machine import (
    TRANSITIONS,
    IllegalTransition,
    allowed_from,
    can_transition,
    is_terminal,
    transition,
)

S = TaskStatus

TERMINAL = {S.COMPLETED, S.EXECUTION_FAILED, S.REJECTED, S.CANCELLED, S.TIMED_OUT}

# The table as the spec describes it, written out by hand rather than derived
# from the module. A test that recomputes the thing it checks proves nothing.
EXPECTED: dict[TaskStatus, set[TaskStatus]] = {
    S.SUBMITTED: {S.PLANNING, S.CANCELLED, S.TIMED_OUT},
    S.PLANNING: {
        S.ACTING,
        S.AWAITING_APPROVAL,
        S.COMPLETED,
        S.EXECUTION_FAILED,
        S.CANCELLED,
        S.TIMED_OUT,
    },
    S.ACTING: {S.VERIFYING, S.EXECUTION_FAILED, S.CANCELLED, S.TIMED_OUT},
    S.VERIFYING: {
        S.PLANNING,
        S.RECOVERING,
        S.COMPLETED,
        S.EXECUTION_FAILED,
        S.CANCELLED,
        S.TIMED_OUT,
    },
    S.RECOVERING: {S.ACTING, S.VERIFYING, S.EXECUTION_FAILED, S.CANCELLED, S.TIMED_OUT},
    S.AWAITING_APPROVAL: {S.ACTING, S.REJECTED, S.CANCELLED, S.TIMED_OUT},
    S.COMPLETED: set(),
    S.EXECUTION_FAILED: set(),
    S.REJECTED: set(),
    S.CANCELLED: set(),
    S.TIMED_OUT: set(),
}


@dataclass
class FakeTask:
    """Enough of a task to satisfy the HasStatus protocol."""

    status: TaskStatus = S.SUBMITTED


def test_every_status_appears_in_the_table() -> None:
    assert set(TRANSITIONS) == set(TaskStatus)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_table_matches_the_spec(status: TaskStatus) -> None:
    assert allowed_from(status) == EXPECTED[status]


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in EXPECTED.items() for target in targets],
)
def test_every_allowed_transition_is_taken(current: TaskStatus, target: TaskStatus) -> None:
    task = FakeTask(status=current)

    previous = transition(task, target)

    assert previous is current
    assert task.status is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # The spec's whole reason for having a table: no shortcut past
        # verification, and no answer without having verified one.
        (S.ACTING, S.COMPLETED),
        (S.ACTING, S.PLANNING),
        (S.PLANNING, S.VERIFYING),
        (S.PLANNING, S.RECOVERING),
        # Approval is a gate. Nothing walks around it.
        (S.AWAITING_APPROVAL, S.COMPLETED),
        (S.AWAITING_APPROVAL, S.PLANNING),
        (S.PLANNING, S.REJECTED),
        (S.VERIFYING, S.REJECTED),
        # A submitted task has not been perceived yet.
        (S.SUBMITTED, S.ACTING),
        (S.SUBMITTED, S.COMPLETED),
        # Terminal means terminal, including the tempting retry.
        (S.COMPLETED, S.PLANNING),
        (S.EXECUTION_FAILED, S.RECOVERING),
        (S.EXECUTION_FAILED, S.PLANNING),
        (S.CANCELLED, S.ACTING),
        (S.REJECTED, S.ACTING),
        (S.TIMED_OUT, S.PLANNING),
        (S.COMPLETED, S.CANCELLED),
    ],
)
def test_forbidden_transitions_raise(current: TaskStatus, target: TaskStatus) -> None:
    task = FakeTask(status=current)

    with pytest.raises(IllegalTransition) as caught:
        transition(task, target)

    assert caught.value.current is current
    assert caught.value.requested is target
    # The status is left alone, so a caller that swallows the error does not
    # end up with a half-applied change.
    assert task.status is current


def test_the_error_message_lists_what_was_allowed() -> None:
    task = FakeTask(status=S.ACTING)

    with pytest.raises(IllegalTransition, match="Allowed from ACTING: CANCELLED, EXECUTION_FAILED"):
        transition(task, S.COMPLETED)


def test_the_error_message_handles_a_terminal_status() -> None:
    task = FakeTask(status=S.COMPLETED)

    with pytest.raises(IllegalTransition, match="Allowed from COMPLETED: nothing"):
        transition(task, S.PLANNING)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_is_terminal_agrees_with_the_table(status: TaskStatus) -> None:
    # Two independent statements of the same fact: the enum's own flag, and
    # having nowhere left to go.
    assert is_terminal(status) is (status in TERMINAL)
    assert is_terminal(status) is (allowed_from(status) == frozenset())


def test_cancel_and_timeout_reach_every_live_status() -> None:
    live = [status for status in TaskStatus if not is_terminal(status)]

    for status in live:
        assert can_transition(status, S.CANCELLED)
        assert can_transition(status, S.TIMED_OUT)


def test_can_transition_does_not_mutate() -> None:
    task = FakeTask(status=S.PLANNING)

    assert can_transition(task.status, S.ACTING) is True
    assert can_transition(task.status, S.COMPLETED) is True
    assert can_transition(task.status, S.VERIFYING) is False
    assert task.status is S.PLANNING


def test_a_full_happy_path_run() -> None:
    task = FakeTask()

    for target in (
        S.PLANNING,
        S.ACTING,
        S.VERIFYING,
        S.PLANNING,
        S.ACTING,
        S.VERIFYING,
        S.COMPLETED,
    ):
        transition(task, target)

    assert task.status is S.COMPLETED
    assert is_terminal(task.status)


def test_a_recovery_then_approval_run() -> None:
    task = FakeTask()

    for target in (
        S.PLANNING,
        S.ACTING,
        S.VERIFYING,
        S.RECOVERING,
        S.ACTING,
        S.VERIFYING,
        S.PLANNING,
        S.AWAITING_APPROVAL,
        S.ACTING,
        S.VERIFYING,
        S.COMPLETED,
    ):
        transition(task, target)

    assert task.status is S.COMPLETED


def test_the_table_is_not_writable_through_allowed_from() -> None:
    before = allowed_from(S.ACTING)

    with pytest.raises(AttributeError):
        before.add(S.COMPLETED)  # type: ignore[attr-defined]

    assert allowed_from(S.ACTING) == before
