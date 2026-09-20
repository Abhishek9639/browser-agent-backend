"""Audit trail (spec section 5, ``audit_events``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

__all__ = ["AuditEvent"]


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Who did what, and when.

    Uses ``timestamp`` rather than ``created_at`` because that is the column
    name spec section 5 gives, so this model does not take the usual mixin.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # Named in spec section 5.
        Index("ix_audit_events_task_id_timestamp", "task_id", "timestamp"),
    )

    # Nullable: not every audited action belongs to a task (a login, say).
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    # Nullable: the actor may be the system rather than a person.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # Decision A11: the column keeps the spec's name; the attribute is renamed
    # because ``metadata`` is reserved on a declarative class.
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
