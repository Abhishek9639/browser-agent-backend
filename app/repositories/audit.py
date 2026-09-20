"""Audit event repository (spec section 5, ``audit_events``)."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.repositories.base import BaseRepository

__all__ = ["AuditEventRepository"]


class AuditEventRepository(BaseRepository[AuditEvent]):
    model = AuditEvent

    async def list_for_task(self, task_id: uuid.UUID) -> Sequence[AuditEvent]:
        """The trail for one task, oldest first. Matches the (task_id, timestamp) index."""
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.task_id == task_id)
            .order_by(AuditEvent.timestamp.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_for_actor(
        self, actor_id: uuid.UUID, *, limit: int = 100
    ) -> Sequence[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.actor_id == actor_id)
            .order_by(AuditEvent.timestamp.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())
