"""Unit of Work: one session, every repository, one transaction.

The rule, in one line: **whoever opens the session commits it, and nobody else
does.** Repositories flush; they never commit. Services take a ``UnitOfWork``
and never commit either.

In a request, ``get_session`` opens the session inside
:meth:`app.db.session.Database.session`, which commits on a clean return and
rolls back on an exception. One request is one transaction, so a handler that
raises halfway through leaves nothing half-written.

In background work there is no request, so the caller opens the session itself
and the same context manager commits it::

    async with database.session() as session:
        uow = UnitOfWork(session)
        step = await uow.task_steps.add(...)
        await uow.audit.record(...)
    # committed here, or rolled back if the block raised

There is deliberately no ``commit()`` on this class. Having one invites a
service to commit half a change, which is exactly the bug the pattern exists to
prevent.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionDep
from app.repositories.approval import ApprovalRepository
from app.repositories.audit import AuditEventRepository
from app.repositories.cache import CachedFlowRepository
from app.repositories.stats import StatsRepository
from app.repositories.task import (
    LlmDecisionRepository,
    RecoveryAttemptRepository,
    TaskRepository,
    TaskStepRepository,
    VerificationRepository,
)
from app.repositories.user import RefreshTokenRepository, UserRepository

__all__ = ["UnitOfWork", "UnitOfWorkDep", "get_unit_of_work"]


class UnitOfWork:
    """Every repository, sharing one session.

    Repositories are built lazily and cached, so injecting this costs nothing
    for the ones a given handler never touches.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @property
    def users(self) -> UserRepository:
        return self._repository("_users", UserRepository)

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        return self._repository("_refresh_tokens", RefreshTokenRepository)

    @property
    def tasks(self) -> TaskRepository:
        return self._repository("_tasks", TaskRepository)

    @property
    def task_steps(self) -> TaskStepRepository:
        return self._repository("_task_steps", TaskStepRepository)

    @property
    def llm_decisions(self) -> LlmDecisionRepository:
        return self._repository("_llm_decisions", LlmDecisionRepository)

    @property
    def verifications(self) -> VerificationRepository:
        return self._repository("_verifications", VerificationRepository)

    @property
    def recovery_attempts(self) -> RecoveryAttemptRepository:
        return self._repository("_recovery_attempts", RecoveryAttemptRepository)

    @property
    def approvals(self) -> ApprovalRepository:
        return self._repository("_approvals", ApprovalRepository)

    @property
    def audit_events(self) -> AuditEventRepository:
        return self._repository("_audit_events", AuditEventRepository)

    @property
    def cached_flows(self) -> CachedFlowRepository:
        return self._repository("_cached_flows", CachedFlowRepository)

    @property
    def stats(self) -> StatsRepository:
        return self._repository("_stats", StatsRepository)

    async def flush(self) -> None:
        """Send pending changes so server defaults and ids are readable.

        Still inside the transaction; still not committed.
        """
        await self.session.flush()

    def _repository[RepositoryT](self, attribute: str, factory: type[RepositoryT]) -> RepositoryT:
        cached = getattr(self, attribute, None)
        if cached is None:
            cached = factory(self.session)  # type: ignore[call-arg]
            setattr(self, attribute, cached)
        return cached


def get_unit_of_work(session: SessionDep) -> UnitOfWork:
    """FastAPI dependency. The session it wraps is committed at the request boundary."""
    return UnitOfWork(session)


UnitOfWorkDep = Annotated[UnitOfWork, Depends(get_unit_of_work)]
