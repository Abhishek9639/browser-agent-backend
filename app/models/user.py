"""Identity aggregate: users and their refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, enum_column
from app.domain.enums import UserRole

if TYPE_CHECKING:
    from app.models.approval import Approval
    from app.models.task import Task

__all__ = ["RefreshToken", "User"]


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Spec section 5, ``users``."""

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # argon2/bcrypt digest. Never the password, never logged.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "user_role"),
        nullable=False,
        default=UserRole.TASK_SUBMITTER,
        server_default=UserRole.TASK_SUBMITTER.value,
    )

    tasks: Mapped[list[Task]] = relationship(
        back_populates="creator",
        lazy="raise",
        foreign_keys="Task.created_by",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        lazy="raise",
        cascade="all, delete-orphan",
    )
    approvals_decided: Mapped[list[Approval]] = relationship(
        back_populates="approver",
        lazy="raise",
        foreign_keys="Approval.approved_by",
    )


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One issued refresh cookie.

    Not in spec section 5. Section 2 requires a 7 day httpOnly refresh cookie,
    and rotating one safely means the server has to know which tokens it has
    issued, which have been used, and what replaced them. Without a stored
    record, a stolen refresh token cannot be revoked and reuse cannot be
    detected.

    Only the hash is stored, for the same reason passwords are hashed: a dump of
    this table must not let anyone mint sessions.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when this token is rotated, so reuse of an old token is detectable.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(String(45))  # fits IPv6

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")
