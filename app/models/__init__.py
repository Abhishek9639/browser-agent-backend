"""ORM models.

Importing this package registers every model on ``Base.metadata``; Alembic's
``env.py`` relies on that for autogenerate. Every new model module is imported
here and listed in ``__all__``.

Tables come from spec section 5, with one addition: ``refresh_tokens``, which
refresh rotation needs (see the note in that section of the spec).
"""

from app.db.base import Base
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.cache import CachedFlow
from app.models.task import LLMDecision, RecoveryAttempt, Task, TaskStep, Verification
from app.models.user import RefreshToken, User

__all__ = [
    "Approval",
    "AuditEvent",
    "Base",
    "CachedFlow",
    "LLMDecision",
    "RecoveryAttempt",
    "RefreshToken",
    "Task",
    "TaskStep",
    "User",
    "Verification",
]
