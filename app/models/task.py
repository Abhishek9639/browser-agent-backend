"""Task aggregate.

The task is the root; a step belongs to exactly one task, and a decision,
verification or recovery attempt belongs to exactly one step. Deleting a task
therefore removes the whole tree, which is expressed with ``ON DELETE CASCADE``
in the database rather than left to the ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.domain.enums import (
    ActionType,
    RecoveryOutcome,
    RiskLevel,
    StepStatus,
    TaskStatus,
    VerificationMethod,
)

if TYPE_CHECKING:
    from app.models.approval import Approval
    from app.models.user import User

__all__ = ["LLMDecision", "RecoveryAttempt", "Task", "TaskStep", "Verification"]

# Spec section 5 shows task_number as "T-000123": a six digit, zero padded
# counter. A Postgres sequence makes it gap-tolerant and race-free under
# concurrent inserts, which a SELECT MAX(...) + 1 would not be.
# Bound to the metadata so create_all/drop_all manage it too, which is what
# the integration fixtures use instead of running migrations.
TASK_NUMBER_SEQUENCE = Sequence("task_number_seq", start=1, increment=1, metadata=Base.metadata)
TASK_NUMBER_DEFAULT = text("'T-' || lpad(nextval('task_number_seq')::text, 6, '0')")


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Spec section 5, ``tasks``."""

    __tablename__ = "tasks"

    task_number: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        unique=True,
        server_default=TASK_NUMBER_DEFAULT,
    )
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        enum_column(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.SUBMITTED,
        server_default=TaskStatus.SUBMITTED.value,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_url: Mapped[str | None] = mapped_column(String(2048))
    final_answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # The outline shown before the first action: a forecast, not a contract.
    plan: Mapped[list[str] | None] = mapped_column(JSONB)
    # Lives in the payments service's database, so it is an opaque id here and
    # deliberately not a foreign key: the two services own separate schemas.
    credits_reservation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cache_flow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cached_flows.id", ondelete="SET NULL")
    )
    # What this run cost, at the prices in force when it ended. Written for
    # every terminal task, whether or not billing is enabled: it is the section
    # 9 cost-per-task figure as well as the amount the outbox settles, and a
    # deployment that turns billing on later still wants the history.
    credits_used: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    creator: Mapped[User] = relationship(
        back_populates="tasks",
        lazy="raise",
        foreign_keys=[created_by],
    )
    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="TaskStep.step_number",
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="task",
        lazy="raise",
        cascade="all, delete-orphan",
    )


class TaskStep(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``task_steps``. One executed action."""

    __tablename__ = "task_steps"
    __table_args__ = (
        # Spec section 5 states this explicitly: step numbers are dense per task.
        UniqueConstraint("task_id", "step_number", name="uq_task_steps_task_id_step_number"),
        Index("ix_task_steps_task_id", "task_id"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[ActionType] = mapped_column(
        enum_column(ActionType, "action_type"),
        nullable=False,
    )
    target: Mapped[str | None] = mapped_column(Text)
    target_index: Mapped[int | None] = mapped_column(Integer)
    # Spec section 10: password and card-like values are masked everywhere,
    # including here. The real value never leaves memory.
    value_redacted: Mapped[str | None] = mapped_column(Text)
    tab_id: Mapped[str | None] = mapped_column(String(32))
    selector_used: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    screenshot_ref: Mapped[str | None] = mapped_column(String(512))
    # The item of the task's plan this step worked on, numbered from 1 in the
    # plan as it then stood. Kept so a reconnecting client ticks the same items
    # the live one did. Null without a plan, and for a replayed step.
    plan_step: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[StepStatus] = mapped_column(
        enum_column(StepStatus, "step_status"),
        nullable=False,
        default=StepStatus.PENDING,
        server_default=StepStatus.PENDING.value,
    )

    task: Mapped[Task] = relationship(back_populates="steps", lazy="raise")
    llm_decision: Mapped[LLMDecision | None] = relationship(
        back_populates="step",
        lazy="raise",
        cascade="all, delete-orphan",
        uselist=False,
    )
    verification: Mapped[Verification | None] = relationship(
        back_populates="step",
        lazy="raise",
        cascade="all, delete-orphan",
        uselist=False,
    )
    recovery_attempts: Mapped[list[RecoveryAttempt]] = relationship(
        back_populates="step",
        lazy="raise",
        cascade="all, delete-orphan",
        order_by="RecoveryAttempt.attempt_number",
    )


class LLMDecision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``llm_decisions``.

    Section 9 requires the model name and prompt version on every step, so this
    row is what makes a run reproducible and costable after the fact.
    """

    __tablename__ = "llm_decisions"
    __table_args__ = (Index("ix_llm_decisions_step_id", "step_id"),)

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    reasoning_summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, "risk_level"),
        nullable=False,
    )
    used_vision: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    step: Mapped[TaskStep] = relationship(back_populates="llm_decision", lazy="raise")


class Verification(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``verifications``. The deterministic outcome check (4.7)."""

    __tablename__ = "verifications"
    __table_args__ = (Index("ix_verifications_step_id", "step_id"),)

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    expected_outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actual_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verified: Mapped[bool] = mapped_column(nullable=False)
    verification_method: Mapped[VerificationMethod] = mapped_column(
        enum_column(VerificationMethod, "verification_method"),
        nullable=False,
    )

    step: Mapped[TaskStep] = relationship(back_populates="verification", lazy="raise")


class RecoveryAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``recovery_attempts``. One try from the section 4.8 ladder."""

    __tablename__ = "recovery_attempts"
    __table_args__ = (Index("ix_recovery_attempts_step_id", "step_id"),)

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # One of the seven ordered strategies in spec section 4.8. Kept as text
    # rather than an enum: the ladder is expected to gain rungs, and a new
    # strategy should not need a migration.
    strategy_used: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[RecoveryOutcome] = mapped_column(
        enum_column(RecoveryOutcome, "recovery_outcome"),
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)

    step: Mapped[TaskStep] = relationship(back_populates="recovery_attempts", lazy="raise")
