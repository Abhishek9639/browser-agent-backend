"""Task business rules (spec section 6).

Everything a task endpoint decides lives here: what makes an instruction
acceptable, who may see a task, what a duplicate is, when a cancel is allowed.
The routers translate HTTP to a call and a response model and nothing else, so
the rules can be read in one place and tested without an HTTP stack.

Like every service, this one does not commit. The request's unit of work owns
the transaction.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.agent.browser.driver import host_is_allowed
from app.agent.cache.flow import decode_steps
from app.agent.cache.signature import task_signature
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.domain.enums import TaskStatus, UserRole
from app.domain.state_machine import transition
from app.models.task import LLMDecision, RecoveryAttempt, Task, TaskStep, Verification
from app.models.user import User
from app.repositories.base import DEFAULT_PAGE_SIZE, Page
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.task import normalise_instruction
from app.services.audit import AuditAction, AuditService
from app.services.billing import BillingService
from app.services.storage import ScreenshotStorage

__all__ = ["CacheMatch", "StepDetail", "TaskService"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CacheMatch:
    """What is known about the cached flow for a task's signature."""

    signature: str
    flow_id: uuid.UUID | None = None
    step_count: int = 0
    run_count: int = 0
    success_rate: float = 0.0
    eligible: bool = False
    last_used_at: datetime | None = None

    @property
    def found(self) -> bool:
        return self.flow_id is not None


@dataclass(frozen=True, slots=True)
class StepDetail:
    """A step with everything recorded about it.

    Assembled from four queries rather than one eager-loaded join: the models
    are `lazy="raise"` on purpose, so anything that wants a relation has to ask
    for it and a stray lazy load in a request cannot go unnoticed.
    """

    step: TaskStep
    decision: LLMDecision | None
    verification: Verification | None
    recovery_attempts: Sequence[RecoveryAttempt]


class TaskService:
    """Owns the task lifecycle as far as the API is concerned."""

    def __init__(
        self,
        uow: UnitOfWork,
        settings: Settings,
        storage: ScreenshotStorage,
        audit: AuditService | None = None,
        billing: BillingService | None = None,
    ) -> None:
        self._uow = uow
        self._settings = settings
        self._storage = storage
        self._audit = audit or AuditService(uow.audit_events)
        self._billing = billing or BillingService(uow, settings)

    # ------------------------------------------------------------- create

    async def create(
        self,
        user: User,
        *,
        instruction: str,
        start_url: str | None = None,
    ) -> Task:
        """Accept a task and queue it.

        The row is written in `SUBMITTED`. Nothing starts here: the runner
        picks it up after the request's transaction commits, because a task
        that began running inside the request would be working on a row that
        might still be rolled back.
        """
        text = normalise_instruction(instruction)
        if not text:
            raise ValidationFailed("An instruction cannot be blank.")

        url = self._check_start_url(start_url or self._settings.AGENT_DEFAULT_START_URL)

        duplicate = await self._uow.tasks.find_active_duplicate(user.id, text)
        if duplicate is not None:
            # The id is in the message on purpose: the client's next move is to
            # open the task it already has, and it cannot do that without it.
            raise Conflict(f"You already have a task running for this instruction: {duplicate.id}.")

        task = await self._uow.tasks.add(Task(instruction=text, created_by=user.id, start_url=url))

        # Synchronous, unlike every other billing call. A user submitting a task
        # has to be told now whether they can afford it, so an insufficient
        # balance is a 402 on this response and an unreachable payments service
        # is a 503: a task that cannot be paid for must not run, and one whose
        # payment is unknown must not either (spec section 7).
        #
        # Held before the commit, so a task row never exists without its
        # reservation. The other order is worse: the client would be told the
        # task was accepted and then charged for it separately, or not at all.
        # A reservation whose transaction then rolls back is released by the
        # payments expiry job, which is what that job is for.
        reservation = await self._billing.reserve_for_task(user.id, task.id)
        if reservation is not None:
            task.credits_reservation_id = reservation.reservation_id

        await self._audit.record(
            AuditAction.TASK_CREATED,
            actor_id=user.id,
            task_id=task.id,
        )
        logger.info("task_created", task_id=str(task.id), user_id=str(user.id))
        return task

    async def cache_match(self, task_id: uuid.UUID, user: User) -> "CacheMatch":
        """Is there a cached flow for this task's signature (spec section 4.12)?

        Answered for any task, including one still running: the point is to let
        a client explain why a task is about to be fast, or why it was not.
        """
        task = await self._uow.tasks.get_owned(task_id, user)
        signature = task_signature(task.instruction, task.start_url)
        flow = await self._uow.cached_flows.get_by_signature(signature)

        if flow is None:
            return CacheMatch(signature=signature)

        return CacheMatch(
            signature=signature,
            flow_id=flow.id,
            step_count=len(decode_steps(flow.action_sequence)),
            run_count=flow.run_count,
            success_rate=flow.success_rate,
            eligible=flow.success_rate >= self._settings.CACHE_MIN_SUCCESS_RATE,
            last_used_at=flow.updated_at,
        )

    async def replay(self, task_id: uuid.UUID, user: User) -> Task:
        """Run this task's flow again, as a new task.

        A new row rather than a reset of the old one. A finished task is a
        record of what happened, and re-running it in place would overwrite the
        history somebody is looking at. The new task is bound to the flow, which
        is what makes the replay unconditional: the user asked for this
        sequence, not for the cache's opinion of its success rate.
        """
        original = await self._uow.tasks.get_owned(task_id, user)
        status = TaskStatus(original.status)
        if not status.is_terminal:
            raise Conflict("That task is still running. Wait for it to finish, or cancel it.")

        signature = task_signature(original.instruction, original.start_url)
        flow = await self._uow.cached_flows.get_by_signature(signature)
        if flow is None:
            raise NotFound("There is no cached flow for that task.")

        duplicate = await self._uow.tasks.find_active_duplicate(user.id, original.instruction)
        if duplicate is not None:
            raise Conflict(f"You already have a task running for this instruction: {duplicate.id}.")

        task = await self._uow.tasks.add(
            Task(
                instruction=original.instruction,
                created_by=user.id,
                start_url=original.start_url,
                cache_flow_id=flow.id,
            )
        )
        reservation = await self._billing.reserve_for_task(user.id, task.id)
        if reservation is not None:
            task.credits_reservation_id = reservation.reservation_id

        await self._audit.record(
            AuditAction.TASK_CREATED,
            actor_id=user.id,
            task_id=task.id,
            metadata={"replayOf": str(original.id), "flowId": str(flow.id)},
        )
        logger.info(
            "task_replay_created",
            task_id=str(task.id),
            replay_of=str(original.id),
            flow_id=str(flow.id),
        )
        return task

    def _check_start_url(self, start_url: str | None) -> str | None:
        """Refuse a start URL outside the sandbox (spec section 4.13).

        Checked here rather than in the schema because the allow-list is
        configuration, and a schema has no way to read it.
        """
        if not start_url:
            return None
        if not host_is_allowed(start_url, tuple(self._settings.AGENT_ALLOWED_DOMAINS)):
            raise ValidationFailed(
                "That start URL is not in the allowed domain list for this deployment."
            )
        return start_url

    # --------------------------------------------------------------- read

    async def get(self, task_id: uuid.UUID, user: User) -> Task:
        """One task, if it is yours. 404 rather than 403 for someone else's."""
        return await self._uow.tasks.get_owned(task_id, user)

    async def list(
        self,
        user: User,
        *,
        status: Sequence[TaskStatus] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[Task]:
        if created_from is not None and created_to is not None and created_from > created_to:
            # Silently returning an empty page reads as "you have no tasks",
            # which sends somebody looking for the tasks rather than for the
            # typo in their date range.
            raise ValidationFailed("`from` cannot be later than `to`.")

        return await self._uow.tasks.list_for_user(
            user,
            status=status,
            created_from=created_from,
            created_to=created_to,
            query=query,
            page=page,
            page_size=page_size,
        )

    async def list_steps(self, task_id: uuid.UUID, user: User) -> Sequence[TaskStep]:
        await self._uow.tasks.get_owned(task_id, user)
        return await self._uow.task_steps.list_for_task(task_id)

    async def list_recovery_attempts(
        self, task_id: uuid.UUID, user: User
    ) -> Sequence[RecoveryAttempt]:
        """Every attempt on the task, in step and then attempt order.

        Across the whole task rather than per step, because the question this
        answers is "where did this run struggle", and that is not a question
        you ask one step at a time.
        """
        await self._uow.tasks.get_owned(task_id, user)
        return await self._uow.recovery_attempts.list_for_task(task_id)

    async def get_step(self, task_id: uuid.UUID, step_id: uuid.UUID, user: User) -> StepDetail:
        step = await self._step_of(task_id, step_id, user)
        return StepDetail(
            step=step,
            decision=await self._uow.llm_decisions.get_for_step(step.id),
            verification=await self._uow.verifications.get_for_step(step.id),
            recovery_attempts=await self._uow.recovery_attempts.list_for_step(step.id),
        )

    async def screenshot(self, task_id: uuid.UUID, step_id: uuid.UUID, user: User) -> bytes:
        """The step's image, for the owner or an admin (spec section 10)."""
        step = await self._step_of(task_id, step_id, user)
        if not step.screenshot_ref:
            raise NotFound("That step has no screenshot.")
        return await self._storage.load(step.screenshot_ref)

    async def _step_of(self, task_id: uuid.UUID, step_id: uuid.UUID, user: User) -> TaskStep:
        """Ownership is checked on the task, then the step is pinned to it.

        Both halves matter. Without the first, anyone could read anyone's
        steps; without the second, a valid step id from your own task could be
        used to read a step belonging to someone else's.
        """
        await self._uow.tasks.get_owned(task_id, user)

        step = await self._uow.task_steps.get(step_id)
        if step is None or step.task_id != task_id:
            raise NotFound(f"Step {step_id} does not exist on this task.")
        return step

    # ------------------------------------------------------------- cancel

    async def cancel(self, task_id: uuid.UUID, user: User) -> Task:
        """Stop a running task.

        Writing `CANCELLED` here is what actually stops it. The runner's flag
        makes the loop notice sooner, but the orchestrator also refuses to move
        a task that is already terminal, so the cancel holds even if the signal
        never arrives, or arrives at a worker that is not running this task.
        """
        task = await self._uow.tasks.get_owned(task_id, user)

        if TaskStatus(task.status).is_terminal:
            raise Conflict(f"This task has already finished ({task.status.value}).")

        previous = transition(task, TaskStatus.CANCELLED)
        task.finished_at = datetime.now(UTC)
        task.failure_reason = "Cancelled by the user."

        await self._audit.record(
            AuditAction.TASK_CANCELLED,
            actor_id=user.id,
            task_id=task.id,
            metadata={"previousStatus": previous.value},
        )
        logger.info("task_cancelled", task_id=str(task.id), previous=previous.value)
        return task

    # -------------------------------------------------------------- admin

    def may_read_screenshots(self, user: User) -> bool:
        """Spec section 10: screenshots are served only to the owner or an admin."""
        return user.role is UserRole.ADMIN
