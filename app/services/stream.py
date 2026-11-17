"""Rebuilding a task's event stream from the database (spec section 6.1).

A client that reconnects says how far it got, and the server catches it up from
the rows before handing it the live feed. That only works because the
orchestrator commits a step before publishing it, so the database is never
behind the stream: there is nothing a client can have seen that cannot be
replayed to it.

Replayed events carry ``seq`` 0. The original sequence number is not
recoverable from a row, and inventing one would break the promise that a
sequence is monotonic for a task. Steps are resumed by ``stepNumber``, which is
stored, which is why that is the cursor the client sends.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from app.agent.orchestrator import UnitOfWorkFactory
from app.core.logging import get_logger
from app.domain.enums import ApprovalStatus, StepStatus, TaskStatus, UserRole
from app.models.task import Task, TaskStep
from app.services.events import (
    AnyEvent,
    ApprovalRequiredEvent,
    BoundingBox,
    FinalEvent,
    PlanEvent,
    StatusEvent,
    StepEvent,
    TaskStats,
)
from app.services.storage import ScreenshotStorage

__all__ = ["REPLAY_SEQ", "Access", "Replay", "StreamService", "dedupe_key", "replayed_keys"]

logger = get_logger(__name__)

REPLAY_SEQ = 0


Access = Literal["ok", "forbidden", "missing"]


class Replay:
    """Everything a reconnecting client missed, in order."""

    def __init__(self, events: list[AnyEvent], *, status: TaskStatus, last_step: int) -> None:
        self.events = events
        self.status = status
        self.last_step = last_step

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class StreamService:
    """Turns stored rows back into the events that produced them."""

    def __init__(self, unit_of_work: UnitOfWorkFactory, storage: ScreenshotStorage) -> None:
        self._unit_of_work = unit_of_work
        self._storage = storage

    async def access(self, task_id: uuid.UUID, user_id: uuid.UUID) -> Access:
        """May this user watch this task?

        Checked again at connect time even though the ticket was only issued
        to an owner: a ticket lives for a minute, and an account can lose its
        access in that minute.
        """
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                return "missing"

            user = await uow.users.get(user_id)
            if user is None:
                return "forbidden"
            if user.role is not UserRole.ADMIN and task.created_by != user_id:
                return "forbidden"
            return "ok"

    async def build(self, task_id: uuid.UUID, after_step: int = 0) -> Replay | None:
        """The catch-up for one connection, or None if the task is gone."""
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                return None

            steps = [
                step
                for step in await uow.task_steps.list_for_task(task_id)
                if step.step_number > after_step
            ]
            pending = await uow.approvals.get_pending_for_task(task_id)
            status = TaskStatus(task.status)
            pending_is_open = pending is not None and (
                ApprovalStatus(pending.approval_status) is ApprovalStatus.PENDING
            )
            approval = pending if pending_is_open else None
            snapshot = _snapshot(task, len(await uow.task_steps.list_for_task(task_id)))
            plan = list(task.plan) if task.plan else None

        events: list[AnyEvent] = [
            StatusEvent(task_id=task_id, status=status, seq=REPLAY_SEQ),
        ]
        if plan:
            events.append(PlanEvent(task_id=task_id, steps=plan, seq=REPLAY_SEQ))

        for step in steps:
            events.append(await self._step_event(task_id, step))

        if approval is not None:
            events.append(
                ApprovalRequiredEvent(
                    task_id=task_id,
                    seq=REPLAY_SEQ,
                    approval_id=approval.id,
                    step_number=snapshot.steps,
                    proposed_action=approval.proposed_action,
                    risk_level=approval.risk_flag,
                    reason=approval.reason,
                )
            )

        if status.is_terminal:
            # A client attaching to a finished task gets the whole story and
            # the ending, rather than a stream that never says anything again.
            events.append(
                FinalEvent(
                    task_id=task_id,
                    seq=REPLAY_SEQ,
                    status=status,
                    final_answer=snapshot.final_answer,
                    failure_reason=snapshot.failure_reason,
                    stats=snapshot.stats,
                )
            )

        last_step = steps[-1].step_number if steps else after_step
        return Replay(events, status=status, last_step=last_step)

    async def _step_event(self, task_id: uuid.UUID, step: TaskStep) -> StepEvent:
        return StepEvent(
            task_id=task_id,
            seq=REPLAY_SEQ,
            step_number=step.step_number,
            action=step.action.value,
            target=step.target,
            value=step.value_redacted,
            verified=StepStatus(step.status) is StepStatus.VERIFIED,
            screenshot=await self._screenshot(step),
            bbox=BoundingBox(**step.bbox) if step.bbox else None,
            url=None,
            tab_id=step.tab_id,
            plan_step=step.plan_step,
        )

    async def _screenshot(self, step: TaskStep) -> str | None:
        """The stored frame, or nothing.

        A missing file must not take the whole replay down with it. The step
        still renders in the activity log without its thumbnail, which is the
        same degradation the live stream applies when it sheds load.
        """
        if not step.screenshot_ref:
            return None
        try:
            import base64

            return base64.b64encode(await self._storage.load(step.screenshot_ref)).decode("ascii")
        except Exception:
            logger.warning(
                "replay_screenshot_missing",
                step_number=step.step_number,
                ref=step.screenshot_ref,
            )
            return None


class _Snapshot:
    __slots__ = ("failure_reason", "final_answer", "stats", "steps")

    def __init__(
        self,
        steps: int,
        final_answer: str | None,
        failure_reason: str | None,
        stats: TaskStats,
    ) -> None:
        self.steps = steps
        self.final_answer = final_answer
        self.failure_reason = failure_reason
        self.stats = stats


def _snapshot(task: Task, step_count: int) -> _Snapshot:
    """Read everything the events need before the session closes.

    Touching a detached ORM instance later raises, and a stream that dies on
    its own final event is a bad way to find that out.
    """
    answer = task.final_answer or {}
    finished = task.finished_at or datetime.now(UTC)
    started = task.created_at

    duration_ms = 0
    if started is not None:
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))

    return _Snapshot(
        steps=step_count,
        final_answer=answer.get("text") if isinstance(answer, dict) else None,
        failure_reason=task.failure_reason,
        stats=TaskStats(
            steps=step_count,
            duration_ms=duration_ms,
            credits_used=step_count,
            cache_hit=task.cache_flow_id is not None,
        ),
    )


def dedupe_key(event: AnyEvent) -> tuple[str, object] | None:
    """What makes two events the same thing said twice.

    Only steps and approvals can arrive from both the replay and the live
    stream, because both are stored. A status event is cheap to repeat and a
    heartbeat is meaningless to compare.
    """
    if isinstance(event, StepEvent):
        return ("step", event.step_number)
    if isinstance(event, ApprovalRequiredEvent):
        return ("approval", event.approval_id)
    return None


def replayed_keys(events: Sequence[AnyEvent]) -> set[tuple[str, object]]:
    return {key for key in (dedupe_key(event) for event in events) if key is not None}
