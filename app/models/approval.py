"""Approval aggregate: the human-in-the-loop gate (spec BR-008, section 4.10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, enum_column
from app.domain.enums import ApprovalStatus, RiskLevel

if TYPE_CHECKING:
    from app.models.task import Task, TaskStep
    from app.models.user import User

__all__ = ["Approval"]


class Approval(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``approvals``.

    Raised when the risk policy returns HIGH, or the planner sets
    ``requiresApproval``. The task parks in ``AWAITING_APPROVAL`` until this row
    is decided or expires.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # Spec section 5 names this composite index: the approvals queue is read
        # per task, newest first.
        Index("ix_approvals_task_id_created_at", "task_id", "created_at"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable: an approval can be raised for a proposed action before the step
    # row exists.
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE")
    )
    risk_flag: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, "risk_level"),
        nullable=False,
    )
    proposed_action: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="approvals", lazy="raise")
    step: Mapped[TaskStep | None] = relationship(lazy="raise")
    approver: Mapped[User | None] = relationship(
        back_populates="approvals_decided",
        lazy="raise",
        foreign_keys=[approved_by],
    )
