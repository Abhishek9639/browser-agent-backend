"""The billing outbox repository (spec section 7)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.billing import BillingOutbox, OutboxKind
from app.repositories.base import BaseRepository

__all__ = ["BillingOutboxRepository"]


class BillingOutboxRepository(BaseRepository[BillingOutbox]):
    model = BillingOutbox

    async def enqueue(
        self,
        kind: OutboxKind,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> BillingOutbox | None:
        """Queue a call, or return ``None`` if it is already queued.

        ``ON CONFLICT DO NOTHING`` on the idempotency key rather than a read
        followed by a write. Two workers settling the same reservation at once
        would otherwise race into a unique violation, and the loser would take
        a transaction down with it: the transaction it would take down is the
        one writing the task's terminal status.

        ``None`` means somebody already queued this exact call, which is a
        success from the caller's point of view.
        """
        statement = (
            insert(BillingOutbox)
            .values(kind=kind.value, payload=payload, idempotency_key=idempotency_key)
            .on_conflict_do_nothing(index_elements=[BillingOutbox.idempotency_key])
            .returning(BillingOutbox)
        )
        result = await self.session.scalars(statement)
        row = result.one_or_none()
        if row is not None:
            await self.session.flush()
        return row

    async def claim_due(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> Sequence[BillingOutbox]:
        """The undelivered calls that are due, locked for this worker.

        ``FOR UPDATE SKIP LOCKED`` so two workers share the backlog instead of
        fighting over the front of it. Without ``SKIP LOCKED`` the second worker
        blocks on the first one's row and the pair runs at the speed of one.
        """
        moment = now or datetime.now(UTC)
        statement = (
            select(BillingOutbox)
            .where(BillingOutbox.done_at.is_(None), BillingOutbox.next_attempt_at <= moment)
            .order_by(BillingOutbox.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.scalars(statement)
        return list(result.all())

    async def mark_done(self, row: BillingOutbox, *, now: datetime | None = None) -> BillingOutbox:
        row.done_at = now or datetime.now(UTC)
        row.attempts += 1
        row.last_error = None
        await self.session.flush()
        return row

    async def mark_failed(
        self,
        row: BillingOutbox,
        *,
        error: str,
        backoff_seconds: float,
        now: datetime | None = None,
    ) -> BillingOutbox:
        """Record a failed attempt and when to try again.

        Exponential on the attempt count, capped at an hour. Capped because an
        uncapped exponential reaches next week by the twentieth attempt, and a
        settle that is delivered next week is a settle nobody can reconcile.
        """
        moment = now or datetime.now(UTC)
        row.attempts += 1
        row.last_error = error[:500]
        delay = min(backoff_seconds * (2 ** (row.attempts - 1)), 3600.0)
        row.next_attempt_at = moment + timedelta(seconds=delay)
        await self.session.flush()
        return row

    async def pending_count(self) -> int:
        """How much is waiting. For the metrics and for a human wondering."""
        statement = select(BillingOutbox).where(BillingOutbox.done_at.is_(None))
        result = await self.session.scalars(statement)
        return len(list(result.all()))

    async def for_key(self, idempotency_key: str) -> BillingOutbox | None:
        statement = select(BillingOutbox).where(BillingOutbox.idempotency_key == idempotency_key)
        row: BillingOutbox | None = await self.session.scalar(statement)
        return row

    async def for_task(self, task_id: uuid.UUID) -> Sequence[BillingOutbox]:
        """Every queued call about one task, for the tests and for support."""
        statement = select(BillingOutbox).order_by(BillingOutbox.created_at)
        result = await self.session.scalars(statement)
        wanted = str(task_id)
        return [row for row in result.all() if str(row.payload.get("taskId")) == wanted]
