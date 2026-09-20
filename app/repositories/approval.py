"""Approval repository (spec section 5, ``approvals``)."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.domain.enums import ApprovalStatus
from app.models.approval import Approval
from app.repositories.base import BaseRepository

__all__ = ["ApprovalRepository"]


class ApprovalRepository(BaseRepository[Approval]):
    model = Approval

    async def get_pending_for_task(self, task_id: uuid.UUID) -> Approval | None:
        """The approval a task is currently parked on, if any.

        A task waits on at most one approval at a time: the orchestrator stops
        the loop at ``AWAITING_APPROVAL``. The ordering makes the behaviour
        deterministic even if a stale PENDING row somehow survives.
        """
        stmt = (
            select(Approval)
            .where(
                Approval.task_id == task_id,
                Approval.approval_status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        approval: Approval | None = await self.session.scalar(stmt)
        return approval

    async def list_for_task(self, task_id: uuid.UUID) -> Sequence[Approval]:
        """Every approval raised for a task, oldest first."""
        stmt = (
            select(Approval).where(Approval.task_id == task_id).order_by(Approval.created_at.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_expired(self, cutoff: object) -> Sequence[Approval]:
        """PENDING approvals created before ``cutoff``.

        Feeds the timeout job: an approval past ``APPROVAL_TIMEOUT_SECONDS``
        becomes ``EXPIRED`` and drives its task to ``TIMED_OUT`` (decision A4).
        """
        stmt = select(Approval).where(
            Approval.approval_status == ApprovalStatus.PENDING,
            Approval.created_at < cutoff,
        )
        return list((await self.session.scalars(stmt)).all())
