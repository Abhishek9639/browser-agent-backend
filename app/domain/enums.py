"""Domain enumerations.

Every value here is stored in Postgres as a native enum type. The member value,
not the member name, is what reaches the database, so the strings below are part
of the wire and storage contract: changing one is a migration.

Casing follows the spec. Statuses and roles are uppercase as section 5 writes
them; actions and verification methods are lowercase because that is how the LLM
emits them in the section 4.5 schema.
"""

from enum import StrEnum

__all__ = [
    "ActionType",
    "ApprovalStatus",
    "RecoveryOutcome",
    "RiskLevel",
    "StepStatus",
    "TaskStatus",
    "UserRole",
    "VerificationMethod",
]


class UserRole(StrEnum):
    """Spec section 5, users.role."""

    TASK_SUBMITTER = "TASK_SUBMITTER"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"


class TaskStatus(StrEnum):
    """Spec section 4.3.

    ``RECOVERING`` and ``VERIFYING`` are persisted, not merely shown in the UI,
    per decision A3: a client that reconnects must see the same state the live
    stream was showing.
    """

    SUBMITTED = "SUBMITTED"
    PLANNING = "PLANNING"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        """Spec section 4.3 terminal set. No transition leaves these."""
        return self in _TERMINAL_TASK_STATUSES


_TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.EXECUTION_FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }
)


class StepStatus(StrEnum):
    """Decision A8. Spec section 5 has ``task_steps.status`` but never lists it."""

    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ActionType(StrEnum):
    """Spec section 4.4. Anything not in this list is rejected before execution."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    SELECT = "select"
    EXTRACT = "extract"
    WAIT = "wait"
    GO_BACK = "go_back"
    NEW_TAB = "new_tab"
    SWITCH_TAB = "switch_tab"
    CLOSE_TAB = "close_tab"
    DONE = "done"


class RiskLevel(StrEnum):
    """Spec section 4.5. Final risk is ``max(deterministic, LLM)``; HIGH needs approval."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        """Severity order, for taking the higher of two assessments.

        Needed because these are strings: ``max(HIGH, MEDIUM)`` compares
        alphabetically and answers ``MEDIUM``, quietly turning an action that
        needed a human into one that does not.
        """
        return _RISK_ORDER[self]

    @classmethod
    def highest(cls, *levels: "RiskLevel") -> "RiskLevel":
        """The most severe of the levels given."""
        return max(levels, key=lambda level: level.rank)


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}


class ApprovalStatus(StrEnum):
    """Spec section 5, approvals.approval_status.

    ``EXPIRED`` drives the task to ``TIMED_OUT`` per decision A4; ``REJECTED``
    is reserved for an explicit human rejection.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class VerificationMethod(StrEnum):
    """The ``expectedOutcome.type`` values from the section 4.5 schema.

    Section 4.7 describes the same set in prose ("URL change/contains, element
    visible, text present, input value equals, DOM hash changed, new tab
    opened"), so the schema's six names are treated as canonical.
    """

    URL_CONTAINS = "url_contains"
    ELEMENT_VISIBLE = "element_visible"
    TEXT_PRESENT = "text_present"
    VALUE_EQUALS = "value_equals"
    DOM_CHANGED = "dom_changed"
    NEW_TAB_OPENED = "new_tab_opened"


class RecoveryOutcome(StrEnum):
    """Result of one recovery attempt (spec section 4.8).

    Not enumerated in the spec; these three are what the strategy loop needs to
    record. All attempts failing drives the step to ``EXECUTION_FAILED``.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
