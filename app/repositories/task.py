"""Task aggregate repositories: tasks, steps, and the per-step child records."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import NotFound
from app.domain.enums import TaskStatus, UserRole
from app.models.task import LLMDecision, RecoveryAttempt, Task, TaskStep, Verification
from app.models.user import User
from app.repositories.base import DEFAULT_PAGE_SIZE, BaseRepository, Page, escape_like

__all__ = [
    "LlmDecisionRepository",
    "RecoveryAttemptRepository",
    "TaskRepository",
    "TaskStepRepository",
    "VerificationRepository",
]


class TaskRepository(BaseRepository[Task]):
    model = Task

    async def get_by_number(self, task_number: str) -> Task | None:
        task: Task | None = await self.session.scalar(
            select(Task).where(Task.task_number == task_number)
        )
        return task

    async def get_owned(self, task_id: uuid.UUID, user: User) -> Task:
        """A task the caller is allowed to see, or :class:`NotFound`.

        Someone else's task raises ``NotFound``, not ``Forbidden``. ``Forbidden``
        would confirm the id exists, which lets an attacker enumerate task ids
        and learn how much traffic the system carries. Admins see everything.
        """
        task = await self.get(task_id)
        if task is None:
            raise NotFound(f"Task {task_id} does not exist.")
        if user.role is not UserRole.ADMIN and task.created_by != user.id:
            raise NotFound(f"Task {task_id} does not exist.")
        return task

    async def list_for_user(
        self,
        user: User,
        *,
        status: TaskStatus | Sequence[TaskStatus] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[Task]:
        """The task history screen: ``GET /tasks?status=&from=&to=&q=&page=``.

        Admins see every task; everyone else sees only their own.
        """
        where: list[ColumnElement[bool]] = []

        if user.role is not UserRole.ADMIN:
            where.append(Task.created_by == user.id)

        if status is not None:
            statuses = [status] if isinstance(status, TaskStatus) else list(status)
            if statuses:
                where.append(Task.status.in_(statuses))

        if created_from is not None:
            where.append(Task.created_at >= created_from)
        if created_to is not None:
            where.append(Task.created_at <= created_to)

        if query:
            term = query.strip()
            if term:
                # Escaped, so a search for "100%" does not match everything.
                where.append(Task.instruction.ilike(f"%{escape_like(term)}%", escape="\\"))

        stmt = select(Task).where(*where).order_by(Task.created_at.desc(), Task.id.desc())
        return await self.paginate(stmt, page=page, page_size=page_size)

    async def list_active(self) -> Sequence[Task]:
        """Tasks that are not in a terminal state.

        Used on startup to find work abandoned by a crash, and by the runner to
        enforce the concurrency caps in decision A13.
        """
        terminal = [s for s in TaskStatus if s.is_terminal]
        stmt = select(Task).where(Task.status.not_in(terminal)).order_by(Task.created_at.asc())
        return list((await self.session.scalars(stmt)).all())

    async def count_active_for_user(self, user_id: uuid.UUID) -> int:
        """Backs the per-user concurrency cap (decision A13)."""
        terminal = [s for s in TaskStatus if s.is_terminal]
        return await self.count(Task.created_by == user_id, Task.status.not_in(terminal))


class TaskStepRepository(BaseRepository[TaskStep]):
    model = TaskStep

    async def next_step_number(self, task_id: uuid.UUID) -> int:
        """The next free step number for a task, starting at 1.

        Safe because exactly one worker advances a given task at a time: the
        orchestrator holds the task for the whole loop. If that ever stops being
        true, ``unique(task_id, step_number)`` turns the race into an
        ``IntegrityError`` rather than two steps silently sharing a number.
        """
        stmt = select(func.coalesce(func.max(TaskStep.step_number), 0)).where(
            TaskStep.task_id == task_id
        )
        return int(await self.session.scalar(stmt) or 0) + 1

    async def list_for_task(self, task_id: uuid.UUID) -> Sequence[TaskStep]:
        stmt = (
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_number.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_after(self, task_id: uuid.UUID, step_number: int) -> Sequence[TaskStep]:
        """Steps a reconnecting client has not seen.

        Spec section 6.1: the client reconnects with ``after_step=N`` and the
        server replays what it missed from the database before going live.
        """
        stmt = (
            select(TaskStep)
            .where(TaskStep.task_id == task_id, TaskStep.step_number > step_number)
            .order_by(TaskStep.step_number.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_by_number(self, task_id: uuid.UUID, step_number: int) -> TaskStep | None:
        stmt = select(TaskStep).where(
            TaskStep.task_id == task_id, TaskStep.step_number == step_number
        )
        step: TaskStep | None = await self.session.scalar(stmt)
        return step


class LlmDecisionRepository(BaseRepository[LLMDecision]):
    model = LLMDecision

    async def get_for_step(self, step_id: uuid.UUID) -> LLMDecision | None:
        decision: LLMDecision | None = await self.session.scalar(
            select(LLMDecision).where(LLMDecision.step_id == step_id)
        )
        return decision


class VerificationRepository(BaseRepository[Verification]):
    model = Verification

    async def get_for_step(self, step_id: uuid.UUID) -> Verification | None:
        verification: Verification | None = await self.session.scalar(
            select(Verification).where(Verification.step_id == step_id)
        )
        return verification


class RecoveryAttemptRepository(BaseRepository[RecoveryAttempt]):
    model = RecoveryAttempt

    async def list_for_step(self, step_id: uuid.UUID) -> Sequence[RecoveryAttempt]:
        stmt = (
            select(RecoveryAttempt)
            .where(RecoveryAttempt.step_id == step_id)
            .order_by(RecoveryAttempt.attempt_number.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_for_task(self, task_id: uuid.UUID) -> Sequence[RecoveryAttempt]:
        """Backs ``GET /tasks/{id}/recovery-attempts`` (spec section 6)."""
        stmt = (
            select(RecoveryAttempt)
            .join(TaskStep, TaskStep.id == RecoveryAttempt.step_id)
            .where(TaskStep.task_id == task_id)
            .order_by(TaskStep.step_number.asc(), RecoveryAttempt.attempt_number.asc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def next_attempt_number(self, step_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(RecoveryAttempt.attempt_number), 0)).where(
            RecoveryAttempt.step_id == step_id
        )
        return int(await self.session.scalar(stmt) or 0) + 1
