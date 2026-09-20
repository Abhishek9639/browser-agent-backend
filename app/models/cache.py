"""Cached flows (spec section 5, ``cached_flows``; behaviour in section 4.12)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["CachedFlow"]


class CachedFlow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A verified action sequence, keyed by task signature.

    ``task_signature`` is ``sha256(normalized_instruction + start_domain)``,
    which is 64 hex characters. Replay falls back to the planner on the first
    failed step and updates ``success_rate``; below 0.5 after at least three
    runs the row is invalidated (spec section 4.12, decision A14).
    """

    __tablename__ = "cached_flows"

    task_signature: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    action_sequence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    success_rate: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    run_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
