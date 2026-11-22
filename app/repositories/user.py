"""Identity repositories."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.user import RefreshToken, User
from app.repositories.base import BaseRepository

__all__ = ["RefreshTokenRepository", "UserRepository"]


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Lookup for login. Email is compared case-insensitively.

        Addresses are case-insensitive in practice, and treating them otherwise
        lets one person register the same address twice in different casing.
        """
        stmt = select(User).where(User.email == email.strip().lower())
        user: User | None = await self.session.scalar(stmt)
        return user

    async def email_exists(self, email: str) -> bool:
        return await self.get_by_email(email) is not None


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """The stored record for a presented cookie. Only hashes are stored."""
        # Rotation and logout must serialize on the same row. Without a lock,
        # two requests can both observe an unused token and mint successors.
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        token: RefreshToken | None = await self.session.scalar(stmt)
        return token

    async def list_active_for_user(self, user_id: uuid.UUID) -> Sequence[RefreshToken]:
        """Tokens that are neither revoked nor expired."""
        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def revoke(self, token: RefreshToken, replaced_by: RefreshToken | None = None) -> None:
        """Mark a token spent.

        ``replaced_by`` records the rotation chain, which is what makes reuse of
        an already-rotated token detectable: presenting a revoked token that has
        a successor is the signal that a cookie was stolen.
        """
        token.revoked_at = datetime.now(UTC)
        if replaced_by is not None:
            token.replaced_by_id = replaced_by.id
        await self.session.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke every live token. Used on logout-everywhere and on reuse."""
        tokens = await self.list_active_for_user(user_id)
        now = datetime.now(UTC)
        for token in tokens:
            token.revoked_at = now
        await self.session.flush()
        return len(tokens)
