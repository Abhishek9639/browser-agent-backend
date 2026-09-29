"""The billing outbox (spec section 7, "Backend integration").

A grant or a settle that is lost because the payments service happened to be
restarting is money that silently went missing: credits somebody paid for and
never received, or work somebody used and was never charged for. Neither is
acceptable, and neither is solvable by retrying in the request, because the
request is over.

So the call is written to this table in the same transaction as the business
change it belongs to. The user row and its signup grant commit together, or
neither does. The terminal status and its settle commit together, or neither
does. A worker then delivers what is here, retrying with a growing delay until
the far side accepts it.

The one call that is not here is ``reserve``, which a user waits for and must be
told about now.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

__all__ = [
    "BillingOutbox",
    "OutboxKind",
    "grant_key",
    "release_key",
    "reserve_key",
    "settle_key",
]


class OutboxKind(StrEnum):
    """Which internal call this row stands for.

    A plain string column rather than a Postgres enum. The payments contract is
    the thing that decides what kinds exist, and adding one should not need a
    migration in this repository.
    """

    GRANT_SIGNUP = "GRANT_SIGNUP"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"


class BillingOutbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One undelivered call to the payments service."""

    __tablename__ = "billing_outbox"
    __table_args__ = (
        # The worker's only query: what is due and not yet done, oldest first.
        # Partial on `done_at IS NULL`, so the index stays the size of the
        # backlog rather than the size of the history.
        Index(
            "ix_billing_outbox_pending",
            "next_attempt_at",
            postgresql_where=text("done_at IS NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Everything the call needs, in the shape the client sends. Kept whole
    # rather than as columns per kind: this row is a queued request, and a
    # schema per kind would mean a migration every time the contract grows.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The idempotency key the call will carry. Stored rather than derived, so a
    # retry is provably the same request even if the derivation changes.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the last attempt failed. One line, for somebody reading the backlog.
    last_error: Mapped[str | None] = mapped_column(Text)

    @property
    def delivered(self) -> bool:
        return self.done_at is not None


def grant_key(user_id: uuid.UUID) -> str:
    """One signup grant per user, ever, however many times it is delivered."""
    return f"signup:{user_id}"


def reserve_key(task_id: uuid.UUID) -> str:
    """One reservation per task.

    ``reservations.task_id`` is unique on the payments side, so a retried
    reserve returns the same hold rather than making a second one.
    """
    return f"reserve:{task_id}"


def settle_key(reservation_id: uuid.UUID) -> str:
    """One settle per reservation. A second delivery is a double charge."""
    return f"settle:{reservation_id}"


def release_key(reservation_id: uuid.UUID) -> str:
    return f"release:{reservation_id}"
