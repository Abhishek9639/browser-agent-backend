"""Registration and login.

Spec section 2 makes this service the only identity provider. Spec section 10
forbids logging or storing a password, so nothing here ever puts the plaintext
into a log line, an audit row, or an exception message.
"""

from app.core.errors import Conflict, InvalidCredentials
from app.core.logging import get_logger
from app.core.security import DUMMY_HASH, AccessToken, TokenService, hash_password, verify_password
from app.db.session import Database
from app.domain.enums import UserRole
from app.models.user import User
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
    ) -> None:
        self._uow = uow
        self._tokens = tokens
        # Only used to audit failures, which must outlive the rollback that a
        # 401 triggers. See _audit_failed_login.
        self._database = database
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

    async def _audit_failed_login(
        self,
        email: str,
        *,
        reason: str,
        user_id: str | None = None,
    ) -> None:
        """Record a failed attempt in its own transaction.

        The request is about to raise, and the request's transaction is rolled
        back on the way out, so writing this through the request's session would
        throw the record away. A failed login is exactly the event a security
        review wants, so it gets a transaction that survives the 401.

        The password is never part of the record.
        """
        logger.warning("login_failed", reason=reason, user_id=user_id)
        async with self._database.session() as session:
            audit = AuditService(AuditEventRepository(session))
            await audit.record(
                AuditAction.LOGIN_FAILED,
                metadata={"email": email, "reason": reason},
            )
