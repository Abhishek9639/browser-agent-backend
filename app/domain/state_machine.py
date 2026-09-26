"""The task lifecycle (spec section 4.3).

One table, one function, one place to change. The spec is explicit that
transitions are enforced here and that illegal ones raise, which matters
because status is what the UI renders, what the runner schedules on, and what
decides whether credits get settled. A task that slips from ``ACTING`` straight
to ``COMPLETED`` without verifying is a bug that would otherwise surface as a
wrong answer rather than an error.

Raising rather than ignoring is deliberate. A rejected transition is a
programming mistake, and a silent no-op would leave the task stuck in a state
nobody chose while the loop carried on.
"""

from collections.abc import Mapping
from typing import Final, Protocol

from app.core.logging import get_logger
from app.domain.enums import TaskStatus

__all__ = [
    "TRANSITIONS",
    "HasStatus",
    "IllegalTransition",
    "allowed_from",
    "can_transition",
    "is_terminal",
    "transition",
]

logger = get_logger(__name__)

# Reachable from anywhere that is not already finished.
#
# CANCELLED: the user may stop a task at any point (POST /tasks/{id}/cancel).
# TIMED_OUT: either budget in spec section 4.2 can expire mid-step, and an
# approval that is never answered lands here too, per decision A4.
_ALWAYS_AVAILABLE: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}
)

_TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    # Accepted, queued, not yet picked up by the runner. EXECUTION_FAILED is
    # reachable without ever planning: a task whose browser will not launch,
    # or one left queued by a crash, has failed before it began. Without this
    # edge those tasks have no legal terminal state and stay live forever.
    TaskStatus.SUBMITTED: frozenset({TaskStatus.PLANNING, TaskStatus.EXECUTION_FAILED}),
    # Perceiving and asking the planner. A decision can be to finish, so
    # COMPLETED is reachable without ever touching the browser. A planner that
    # cannot produce a valid decision fails the task here.
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.ACTING,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.COMPLETED,
            TaskStatus.EXECUTION_FAILED,
        }
    ),
    # Executing one action. Always followed by verification: the whole point of
    # section 4.7 is that the agent does not take its own word for an outcome.
    TaskStatus.ACTING: frozenset({TaskStatus.VERIFYING, TaskStatus.EXECUTION_FAILED}),
    # Checking the outcome. Back to PLANNING for the next step, on to RECOVERING
    # when the check failed, or COMPLETED when the step was the last one.
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.RECOVERING,
            TaskStatus.COMPLETED,
            TaskStatus.EXECUTION_FAILED,
        }
    ),
    # Working through the section 4.8 ladder. A strategy either re-acts or
    # re-verifies; exhausting the ladder fails the step.
    TaskStatus.RECOVERING: frozenset(
        {TaskStatus.ACTING, TaskStatus.VERIFYING, TaskStatus.EXECUTION_FAILED}
    ),
    # Parked on a human. Approve resumes into ACTING, an explicit rejection is
    # the only route to REJECTED, and an unanswered approval expires to
    # TIMED_OUT through _ALWAYS_AVAILABLE.
    TaskStatus.AWAITING_APPROVAL: frozenset({TaskStatus.ACTING, TaskStatus.REJECTED}),
    # Terminal states. Nothing leaves them.
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.EXECUTION_FAILED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.TIMED_OUT: frozenset(),
}

TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = {
    status: targets if not targets else targets | _ALWAYS_AVAILABLE
    for status, targets in _TRANSITIONS.items()
}


class IllegalTransition(RuntimeError):
    """A status change the lifecycle does not allow."""

    def __init__(self, current: TaskStatus, requested: TaskStatus) -> None:
        self.current = current
        self.requested = requested
        allowed = ", ".join(sorted(status.value for status in TRANSITIONS[current])) or "nothing"
        super().__init__(
            f"Cannot move a task from {current.value} to {requested.value}. "
            f"Allowed from {current.value}: {allowed}."
        )


class HasStatus(Protocol):
    """Anything with a task status.

    A protocol rather than the ORM model, so the state machine stays in the
    domain layer and can be exercised without a database.
    """

    status: TaskStatus


def is_terminal(status: TaskStatus) -> bool:
    """Has the task finished, whatever the outcome?"""
    return status.is_terminal


def allowed_from(status: TaskStatus) -> frozenset[TaskStatus]:
    """Every status reachable in one step. Empty for a terminal status."""
    return TRANSITIONS[status]


def can_transition(current: TaskStatus, requested: TaskStatus) -> bool:
    """Non-raising form, for a caller that wants to ask before acting."""
    return requested in TRANSITIONS[current]


def transition(task: HasStatus, new_status: TaskStatus) -> TaskStatus:
    """Move a task to ``new_status``, or raise.

    Returns the previous status, because that is what the section 6.1
    ``status`` event carries and the caller would otherwise have to remember to
    capture before the assignment.
    """
    current = TaskStatus(task.status)

    if not can_transition(current, new_status):
        raise IllegalTransition(current, new_status)

    task.status = new_status
    logger.info("task_status_changed", **{"from": current.value, "to": new_status.value})
    return current
