"""Human-in-the-loop approval (spec sections 4.10 and 6).

Two halves that meet in the middle. A run that hits a high-risk step writes a
PENDING row, announces it, and waits; an HTTP request answers it, writes the
decision and wakes the run.

The waiting is in-process and the answer is in the database, deliberately in
that order. The database is what makes the decision survive a disconnect, a
reconnecting client and a restart; the event is only how the waiting run finds
out quickly. If the event is lost the answer is not, which is the right way
round.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import Conflict, Forbidden, NotFound
from app.core.logging import get_logger
from app.domain.enums import ApprovalStatus, TaskStatus, UserRole
from app.models.approval import Approval
from app.models.user import User
from app.repositories.unit_of_work import UnitOfWork
from app.services.audit import AuditAction, AuditService
from app.services.events import ApprovalResolvedEvent, EventBus

__all__ = ["Answer", "ApprovalRegistry", "ApprovalService"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Answer:
    """A human's decision, as the waiting run receives it."""

    approved: bool
    by: uuid.UUID | None = None
    reason: str | None = None


class ApprovalRegistry:
    """The approvals this process is currently waiting on.

    Per process, like the event bus and the stream tickets, and for the same
    reason: a run waits on the worker it is running on. With more than one
    worker the answer still lands in the database, so the waiting run notices
    on its next look rather than immediately, which is the degradation this
    design is willing to accept until there is a shared channel.
    """

    def __init__(self) -> None:
        self._waiting: dict[uuid.UUID, asyncio.Event] = {}
        self._answers: dict[uuid.UUID, Answer] = {}

    def wait_on(self, approval_id: uuid.UUID) -> asyncio.Event:
        event = asyncio.Event()
        self._waiting[approval_id] = event
        return event

    def resolve(self, approval_id: uuid.UUID, answer: Answer) -> bool:
        """Record the answer and wake the run. True if anyone was waiting."""
        self._answers[approval_id] = answer
        event = self._waiting.get(approval_id)
        if event is None:
            return False
        event.set()
        return True

    def answer_for(self, approval_id: uuid.UUID) -> Answer | None:
        return self._answers.get(approval_id)

    def forget(self, approval_id: uuid.UUID) -> None:
        self._waiting.pop(approval_id, None)
        self._answers.pop(approval_id, None)

    @property
    def waiting(self) -> int:
        return len(self._waiting)


class ApprovalService:
    """The rules about who may answer, and what answering does."""

    def __init__(
        self,
        uow: UnitOfWork,
        registry: ApprovalRegistry,
        events: EventBus,
        *,
        owner_can_approve: bool = True,
        audit: AuditService | None = None,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._events = events
        self._owner_can_approve = owner_can_approve
        self._audit = audit or AuditService(uow.audit_events)

    async def decide(
        self,
        task_id: uuid.UUID,
        user: User,
        *,
        approved: bool,
        reason: str | None = None,
    ) -> Approval:
        """Answer the approval a task is parked on.

        Ownership is checked before anything else, so somebody who cannot see
        the task cannot learn whether it has an approval pending by watching
        which error comes back.
        """
        task = await self._uow.tasks.get_owned(task_id, user)
        self._require_reviewer(user, task.created_by)

        approval = await self._uow.approvals.get_pending_for_task(task_id)
        if approval is None:
            decided = await self._uow.approvals.list_for_task(task_id)
            if decided:
                raise Conflict("That approval has already been answered.")
            raise NotFound("This task is not waiting for an approval.")

        approval.approval_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.approved_by = user.id
        approval.reason = reason or approval.reason
        approval.decided_at = datetime.now(UTC)

        await self._audit.record(
            AuditAction.APPROVAL_GRANTED if approved else AuditAction.APPROVAL_REJECTED,
            actor_id=user.id,
            task_id=task_id,
            metadata={"approvalId": str(approval.id), "reason": reason},
        )
        logger.info(
            "approval_decided",
            task_id=str(task_id),
            approved=approved,
            by=str(user.id),
        )
        return approval

    async def announce(self, approval: Approval, *, approved: bool, by: uuid.UUID) -> None:
        """Publish the decision and wake the waiting run.

        Called after the request's transaction has committed, so a run that
        wakes and re-reads the row sees the answer that woke it.
        """
        await self._events.publish(
            approval.task_id,
            ApprovalResolvedEvent(
                task_id=approval.task_id,
                approval_id=approval.id,
                approved=approved,
                by=by,
                reason=approval.reason,
            ),
        )
        self._registry.resolve(
            approval.id, Answer(approved=approved, by=by, reason=approval.reason)
        )

    def _require_reviewer(self, user: User, owner_id: uuid.UUID) -> None:
        """Spec section 6: APPROVER or ADMIN, or the owner when configured.

        403 rather than 404 here on purpose: by this point the caller has been
        shown the task, so there is nothing left to hide and telling them they
        lack the role is more useful than pretending it does not exist.
        """
        if user.role in {UserRole.APPROVER, UserRole.ADMIN}:
            return
        if self._owner_can_approve and user.id == owner_id:
            return
        raise Forbidden("Approving a high-risk action needs the APPROVER or ADMIN role.")


async def expire_pending(uow: UnitOfWork, task_id: uuid.UUID, reason: str) -> Approval | None:
    """Mark a task's pending approval EXPIRED.

    Used by the timeout and by restart recovery. Spec section 4.3 and decision
    A4: an approval nobody answered expires, and the task times out. REJECTED
    is reserved for a human actually saying no.
    """
    approval = await uow.approvals.get_pending_for_task(task_id)
    if approval is None:
        return None

    approval.approval_status = ApprovalStatus.EXPIRED
    approval.reason = approval.reason or reason
    approval.decided_at = datetime.now(UTC)
    logger.info("approval_expired", task_id=str(task_id), approval_id=str(approval.id))
    return approval


def is_awaiting_approval(status: TaskStatus) -> bool:
    return status is TaskStatus.AWAITING_APPROVAL
