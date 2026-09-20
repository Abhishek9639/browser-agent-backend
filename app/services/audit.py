"""Audit trail service.

Every audited action goes through :meth:`AuditService.record`, so the redaction
rule is applied in one place. Spec section 10 forbids persisting credentials,
typed passwords or screenshots, and ``audit_events.metadata`` is as readable as
a log line: anyone with database access can see it.

The rules come from :mod:`app.core.redaction`, the same module the structlog
processor uses, so an audit row and a log line mask identically.
"""

import uuid
from typing import Any, Final

from app.core.logging import get_logger
from app.core.redaction import redact
from app.models.audit import AuditEvent
from app.repositories.audit import AuditEventRepository

__all__ = ["AuditAction", "AuditService"]

logger = get_logger(__name__)


class AuditAction:
    """The action names this service writes.

    Constants rather than an enum: spec section 5 types ``action`` as text, and
    a new audited action should not need a migration.
    """

    USER_REGISTERED: Final = "USER_REGISTERED"
    USER_LOGGED_IN: Final = "USER_LOGGED_IN"
    USER_LOGGED_OUT: Final = "USER_LOGGED_OUT"
    LOGIN_FAILED: Final = "LOGIN_FAILED"
    # The two below are audit action names, not credentials; S105 matches the word.
    TOKEN_REFRESHED: Final = "TOKEN_REFRESHED"  # noqa: S105
    TOKEN_REUSE_DETECTED: Final = "TOKEN_REUSE_DETECTED"  # noqa: S105

    TASK_CREATED: Final = "TASK_CREATED"
    TASK_CANCELLED: Final = "TASK_CANCELLED"
    TASK_COMPLETED: Final = "TASK_COMPLETED"
    TASK_FAILED: Final = "TASK_FAILED"
    TASK_REPLAYED: Final = "TASK_REPLAYED"

    APPROVAL_REQUESTED: Final = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED: Final = "APPROVAL_GRANTED"
    APPROVAL_REJECTED: Final = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED: Final = "APPROVAL_EXPIRED"

    CREDITS_RESERVED: Final = "CREDITS_RESERVED"
    CREDITS_SETTLED: Final = "CREDITS_SETTLED"
    CREDITS_RELEASED: Final = "CREDITS_RELEASED"


class AuditService:
    """Writes audit rows. Does not commit; see the unit of work."""

    def __init__(self, repository: AuditEventRepository) -> None:
        self._repository = repository

    async def record(
        self,
        action: str,
        *,
        actor_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append one audit event.

        ``actor_id`` is ``None`` when the system acts (the approval timeout job,
        the reservation expiry sweep). ``task_id`` is ``None`` for events that
        are not about a task, such as a login.

        ``metadata`` is redacted before it is stored, not before it is read, so
        a sensitive value never reaches the table in the first place.
        """
        safe_metadata = redact(metadata) if metadata else None

        event = AuditEvent(
            action=action,
            actor_id=actor_id,
            task_id=task_id,
            event_metadata=safe_metadata,
        )
        await self._repository.add(event)

        logger.info("audit_event", action=action, task_id=str(task_id) if task_id else None)
        return event
