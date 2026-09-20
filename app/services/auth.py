"""Registration and login.

Spec section 2 makes this service the only identity provider. Spec section 10
forbids logging or storing a password, so nothing here ever puts the plaintext
into a log line, an audit row, or an exception message.
"""

from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import Conflict, InvalidCredentials, RefreshTokenReuse, Unauthorized
from app.core.logging import get_logger
from app.core.security import (
    DUMMY_HASH,
    AccessToken,
    TokenService,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.session import Database
from app.domain.enums import UserRole
from app.models.user import RefreshToken, User
from app.repositories.audit import AuditEventRepository
from app.repositories.unit_of_work import UnitOfWork
from app.services.audit import AuditAction, AuditService

__all__ = ["AuthService"]

logger = get_logger(__name__)


class AuthService:
    """Owns the account lifecycle. Does not commit; see the unit of work."""

    def __init__(
        self,
        uow: UnitOfWork,
        tokens: TokenService,
        database: Database,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._tokens = tokens
        # Used for the two writes that must outlive the rollback a 401 triggers:
        # the failed-login audit, and revoking a chain on reuse detection.
        self._database = database
        self._settings = settings
        self._audit = AuditService(uow.audit_events)

    async def register(self, *, name: str, email: str, password: str) -> User:
        """Create a TASK_SUBMITTER account.

        The email arrives already normalised by the schema, so the uniqueness
        check and the stored value agree.
        """
        if await self._uow.users.email_exists(email):
            raise Conflict("That email is already registered.")

        user = await self._uow.users.add(
            User(
                name=name,
                email=email,
                password_hash=hash_password(password),
                role=UserRole.TASK_SUBMITTER,
            )
        )

        await self._audit.record(
            AuditAction.USER_REGISTERED,
            actor_id=user.id,
            metadata={"email": email},
        )
        logger.info("user_registered", user_id=str(user.id))
        return user

    async def authenticate(self, *, email: str, password: str) -> tuple[User, AccessToken]:
        """Verify credentials and mint an access token.

        An unknown email and a wrong password fail identically, in the same
        error and in roughly the same time: when there is no user we still run
        a verification against a throwaway hash, so the response time does not
        reveal whether the account exists.
        """
        user = await self._uow.users.get_by_email(email)

        if user is None:
            verify_password(password, DUMMY_HASH)
            await self._audit_failed_login(email, reason="unknown_email")
            raise InvalidCredentials

        if not verify_password(password, user.password_hash):
            await self._audit_failed_login(email, reason="bad_password", user_id=str(user.id))
            raise InvalidCredentials

        token = self._tokens.create_access_token(user)

        await self._audit.record(
            AuditAction.USER_LOGGED_IN,
            actor_id=user.id,
            metadata={"email": email},
        )
        logger.info("user_logged_in", user_id=str(user.id))
        return user, token

    async def issue_refresh_token(
        self,
        user: User,
        *,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> str:
        """Mint a refresh token and store only its hash.

        Returns the raw value, which exists exactly once: it goes straight into
        the cookie and is never written down anywhere else.
        """
        raw = generate_refresh_token()
        await self._uow.refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                token_hash=hash_refresh_token(raw),
                expires_at=datetime.now(UTC) + self._settings.refresh_token_ttl,
                user_agent=(user_agent or None),
                ip=(ip or None),
            )
        )
        return raw

    async def rotate_refresh_token(
        self,
        raw_token: str,
        *,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> tuple[User, AccessToken, str]:
        """Exchange a refresh token for a new pair.

        Rotation is single-use: the presented token is revoked and points at its
        successor. Presenting a revoked token again means two parties hold the
        same cookie, so the whole chain is destroyed.
        """
        presented = await self._uow.refresh_tokens.get_by_token_hash(hash_refresh_token(raw_token))

        if presented is None:
            raise Unauthorized("The refresh token is invalid.")

        if presented.revoked_at is not None:
            # Either a stolen cookie is being replayed, or the legitimate client
            # is retrying after a lost response. The two are indistinguishable
            # from here, so assume the worse one.
            await self._handle_reuse(presented)
            raise RefreshTokenReuse

        if presented.expires_at <= datetime.now(UTC):
            raise Unauthorized("The refresh token has expired.")

        user = await self._uow.users.get(presented.user_id)
        if user is None:
            raise Unauthorized("The refresh token is invalid.")

        raw = generate_refresh_token()
        replacement = await self._uow.refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                token_hash=hash_refresh_token(raw),
                expires_at=datetime.now(UTC) + self._settings.refresh_token_ttl,
                user_agent=(user_agent or None),
                ip=(ip or None),
            )
        )
        await self._uow.refresh_tokens.revoke(presented, replaced_by=replacement)

        access = self._tokens.create_access_token(user)
        await self._audit.record(
            AuditAction.TOKEN_REFRESHED,
            actor_id=user.id,
            metadata={"rotated_from": str(presented.id)},
        )
        logger.info("token_refreshed", user_id=str(user.id))
        return user, access, raw

    async def revoke_refresh_token(self, raw_token: str | None) -> None:
        """Log out. Idempotent: an absent or unknown token is not an error.

        Logging out is a request to end a session, and a client that has lost
        its cookie has already achieved that. Failing here would only make the
        frontend handle an error it cannot act on.
        """
        if not raw_token:
            return

        token = await self._uow.refresh_tokens.get_by_token_hash(hash_refresh_token(raw_token))
        if token is None or token.revoked_at is not None:
            return

        await self._uow.refresh_tokens.revoke(token)
        await self._audit.record(
            AuditAction.USER_LOGGED_OUT,
            actor_id=token.user_id,
            metadata={"session_id": str(token.id)},
        )
        logger.info("user_logged_out", user_id=str(token.user_id))

    async def _handle_reuse(self, presented: RefreshToken) -> None:
        """A revoked token came back. Destroy every session for that user.

        This runs in the *request's* transaction, not a separate one. It has to:
        the rows it revokes and the user it references are the request's own
        state, and a second connection cannot see them.

        The write survives the 401 because the route catches
        :class:`RefreshTokenReuse` and returns the response instead of letting
        it propagate, so the request finishes normally and commits. Revoking a
        compromised chain and then discarding the revocation would be worse
        than not detecting the reuse at all.
        """
        # Named session_id, not token_id: these are row ids, not secrets, but
        # the redaction rule matches any key containing "token" and would mask
        # exactly the identifier a security review needs.
        logger.warning(
            "refresh_token_reuse_detected",
            user_id=str(presented.user_id),
            session_id=str(presented.id),
        )
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(presented.user_id)
        await self._audit.record(
            AuditAction.TOKEN_REUSE_DETECTED,
            actor_id=presented.user_id,
            metadata={"session_id": str(presented.id), "sessions_revoked": revoked},
        )

    async def _audit_failed_login(
        self,
        email: str,
        *,
        reason: str,
        user_id: str | None = None,
    ) -> None:
        """Record a failed attempt in its own transaction.

        Unlike reuse handling, this depends on nothing in the request's
        transaction: it writes one row with no actor and no foreign key, so a
        separate connection can do it safely. That matters because the request
        is about to raise, and its transaction is rolled back on the way out.

        A failed login is exactly the event a security review wants, and they
        are precisely the requests that failed. The password is never part of
        the record.
        """
        logger.warning("login_failed", reason=reason, user_id=user_id)
        async with self._database.session() as session:
            audit = AuditService(AuditEventRepository(session))
            await audit.record(
                AuditAction.LOGIN_FAILED,
                metadata={"email": email, "reason": reason},
            )
