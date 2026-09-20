"""Data access.

One repository per aggregate, all sharing :class:`~app.repositories.base.BaseRepository`.
Repositories own queries, not transactions: see :mod:`app.repositories.unit_of_work`
for who commits.
"""

from app.repositories.approval import ApprovalRepository
from app.repositories.audit import AuditEventRepository
from app.repositories.base import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    BaseRepository,
    Page,
    escape_like,
)
from app.repositories.cache import CachedFlowRepository
from app.repositories.task import (
    LlmDecisionRepository,
    RecoveryAttemptRepository,
    TaskRepository,
    TaskStepRepository,
    VerificationRepository,
)
from app.repositories.unit_of_work import UnitOfWork, UnitOfWorkDep, get_unit_of_work
from app.repositories.user import RefreshTokenRepository, UserRepository

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ApprovalRepository",
    "AuditEventRepository",
    "BaseRepository",
    "CachedFlowRepository",
    "LlmDecisionRepository",
    "Page",
    "RecoveryAttemptRepository",
    "RefreshTokenRepository",
    "TaskRepository",
    "TaskStepRepository",
    "UnitOfWork",
    "UnitOfWorkDep",
    "UserRepository",
    "VerificationRepository",
    "escape_like",
    "get_unit_of_work",
]
