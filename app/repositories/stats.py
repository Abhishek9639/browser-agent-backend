"""Aggregate queries behind the stats endpoint (spec section 9).

One place for the SQL, so the service above it does arithmetic and role checks
and nothing else. Every query takes the same optional owner filter: a user sees
their own work, an administrator asking for it sees everyone's.

Counting decisions worth knowing about:

* **Only terminal tasks count towards rates.** A task still running is not a
  failure, and including it would make the completion rate dip whenever
  somebody submits something.
* **Recovery is counted per rescued step, not per attempt.** A step saved on
  the third rung is one success, not one success and two failures. Counting
  attempts once produced a recovery rate of 36% for a run that actually
  rescued 80% of what broke.
* **A replayed step is one with no ``llm_decisions`` row**, which is what the
  cache leaves behind. There is no boolean for it: the absence of a model call
  is the evidence, and it is the same thing the cache tests assert on.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, distinct, func, select

from app.domain.enums import StepStatus, TaskStatus
from app.models.approval import Approval
from app.models.task import LLMDecision, Task, TaskStep
from app.repositories.base import BaseRepository

__all__ = ["DayCount", "StatsRepository", "TaskTotals"]


@dataclass(frozen=True, slots=True)
class TaskTotals:
    """Terminal tasks, and how they ended."""

    total: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    rejected: int = 0


@dataclass(frozen=True, slots=True)
class DayCount:
    """One day of the trend."""

    day: date
    total: int
    completed: int


class StatsRepository(BaseRepository[Task]):
    model = Task

    # ----------------------------------------------------------- the basics

    async def task_totals(self, owner_id: uuid.UUID | None) -> TaskTotals:
        stmt = self._scoped(
            select(Task.status, func.count()).where(Task.finished_at.is_not(None)),
            owner_id,
        ).group_by(Task.status)

        counts = {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}
        return TaskTotals(
            total=sum(counts.values()),
            completed=counts.get(TaskStatus.COMPLETED, 0),
            failed=counts.get(TaskStatus.EXECUTION_FAILED, 0),
            cancelled=counts.get(TaskStatus.CANCELLED, 0),
            timed_out=counts.get(TaskStatus.TIMED_OUT, 0),
            rejected=counts.get(TaskStatus.REJECTED, 0),
        )

    async def median_duration_seconds(self, owner_id: uuid.UUID | None) -> float | None:
        """The median of a completed task's wall clock.

        Median rather than mean, which is what spec section 9 asks for and the
        right choice anyway: one task that sat waiting for an approval would
        drag an average somewhere nobody's task has ever been.
        """
        seconds = func.extract("epoch", Task.finished_at - Task.created_at)
        stmt = self._scoped(
            select(func.percentile_cont(0.5).within_group(seconds)).where(
                Task.finished_at.is_not(None)
            ),
            owner_id,
        )
        value = await self.session.scalar(stmt)
        return float(value) if value is not None else None

    # --------------------------------------------------------- the KPI rates

    async def recovery_episodes(self, owner_id: uuid.UUID | None) -> tuple[int, int]:
        """Steps that needed rescuing, and steps that were rescued.

        A step is an episode if it has any recovery attempt against it, and a
        success if it ended `VERIFIED`. That is the section 9 definition: the
        denominator is induced failures, not ladder rungs.
        """
        attempted = (
            select(distinct(TaskStep.id))
            .join(Task, Task.id == TaskStep.task_id)
            .where(TaskStep.recovery_attempts.any())
        )
        episodes = await self.session.scalar(
            select(func.count()).select_from(self._scoped(attempted, owner_id).subquery())
        )

        rescued_rows = (
            select(distinct(TaskStep.id))
            .join(Task, Task.id == TaskStep.task_id)
            .where(TaskStep.recovery_attempts.any(), TaskStep.status == StepStatus.VERIFIED)
        )
        rescued = await self.session.scalar(
            select(func.count()).select_from(self._scoped(rescued_rows, owner_id).subquery())
        )
        return int(episodes or 0), int(rescued or 0)

    async def replayed_tasks(self, owner_id: uuid.UUID | None) -> int:
        """Terminal tasks that replayed at least one cached step.

        Identified by a step with no ``llm_decisions`` row, because that is
        exactly what a replayed step leaves behind. `tasks.cache_flow_id` is
        not the test: it is set both when a task replays a flow and when a task
        stores one, so counting it would report every completed task as a hit.
        """
        replayed_step = (
            select(TaskStep.task_id)
            .outerjoin(LLMDecision, LLMDecision.step_id == TaskStep.id)
            .where(LLMDecision.id.is_(None))
            .subquery()
        )
        stmt = self._scoped(
            select(func.count(distinct(Task.id))).where(
                Task.finished_at.is_not(None),
                Task.id.in_(select(replayed_step.c.task_id)),
            ),
            owner_id,
        )
        return int(await self.session.scalar(stmt) or 0)

    async def approvals_requested(self, owner_id: uuid.UUID | None) -> int:
        stmt = self._scoped(
            select(func.count(Approval.id)).join(Task, Task.id == Approval.task_id),
            owner_id,
        )
        return int(await self.session.scalar(stmt) or 0)

    # ----------------------------------------------------------- the billing

    async def step_and_vision_counts(self, owner_id: uuid.UUID | None) -> tuple[int, int]:
        """Steps taken, and how many of them used vision.

        Credits are priced per step with a surcharge for vision, so these two
        numbers are what the cost line is built from. Computed rather than
        stored: the ledger lives in the payments service, and this endpoint
        reports what this service can see.
        """
        steps = await self.session.scalar(
            self._scoped(
                select(func.count(TaskStep.id)).join(Task, Task.id == TaskStep.task_id),
                owner_id,
            )
        )
        vision = await self.session.scalar(
            self._scoped(
                select(func.count(LLMDecision.id))
                .join(TaskStep, TaskStep.id == LLMDecision.step_id)
                .join(Task, Task.id == TaskStep.task_id)
                .where(LLMDecision.used_vision.is_(True)),
                owner_id,
            )
        )
        return int(steps or 0), int(vision or 0)

    # ------------------------------------------------------------- the trend

    async def daily(self, owner_id: uuid.UUID | None, *, since: datetime) -> list[DayCount]:
        """Tasks per day since a cutoff, and how many of them completed.

        Grouped by the creation date rather than the finish date, so a task
        appears on the day the user submitted it, which is the day they will
        look for it.
        """
        day = func.date(Task.created_at).label("day")
        completed = func.count().filter(Task.status == TaskStatus.COMPLETED)
        stmt = (
            self._scoped(
                select(day, func.count(), completed).where(Task.created_at >= since),
                owner_id,
            )
            .group_by(day)
            .order_by(day)
        )

        rows = (await self.session.execute(stmt)).all()
        return [DayCount(day=row[0], total=int(row[1]), completed=int(row[2])) for row in rows]

    # ------------------------------------------------------------- internals

    @staticmethod
    def _scoped[T: tuple[Any, ...]](stmt: "Select[T]", owner_id: uuid.UUID | None) -> "Select[T]":
        """Restrict to one user's tasks, or leave it global.

        ``None`` means global and is only ever reached through an explicit
        administrator request; every other caller passes an id. Written as one
        helper so the scope cannot be forgotten on one query out of eight.
        """
        if owner_id is None:
            return stmt
        return stmt.where(Task.created_by == owner_id)
