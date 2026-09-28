"""The agent loop (spec section 4.2).

Perceive, decide, act, verify, record, repeat. Everything else the agent will
eventually do, cache lookup, visual grounding, risk and approval, recovery,
session management, is a collaborator from :mod:`app.agent.contracts` with a
do-nothing default. The loop reads the same whether those are real or not,
which is the point: the later parts replace a default, not this file.

Two orderings here are load-bearing rather than incidental.

The screenshot is written to storage before the rows that reference it, and
the rows are committed before the step event is published. Storage is not
transactional, so something has to be first, and an orphaned image is a wasted
few kilobytes while a row pointing at a missing image is a broken page. The
commit-then-publish order is what spec section 6.1 relies on when a
reconnecting client asks to be caught up from the database: the database is
never behind the live view, so there is nothing a client can have seen that it
cannot be told again.
"""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast
from urllib.parse import urlsplit

from app.agent.browser.action_builder import build_action, selector_for
from app.agent.browser.driver import ActionResult
from app.agent.browser.errors import BrowserError
from app.agent.browser.screenshots import Screenshot, to_base64
from app.agent.budget import BudgetExceeded, BudgetReason, TaskBudget
from app.agent.contracts import (
    AgentBrowser,
    ApprovalGate,
    Attempt,
    CachedHint,
    CancelSignal,
    DenyApprovals,
    FlowCache,
    Grounder,
    GroundingOutcome,
    NeverCancelled,
    NoCache,
    NoGrounding,
    PageStateSessions,
    Recovery,
    RecoveryContext,
    RiskPolicy,
    SessionManager,
    Verdict,
    Verifier,
)
from app.agent.llm.base import LLMRateLimited, LLMTimeout, LLMUnavailable
from app.agent.perception.dom_extractor import MASK, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.planner import Planner, PlannerOutputError, PlannerResult, StepSummary
from app.agent.planner.schema import Decision
from app.agent.recovery.runner import RecoveryRunner, failure_sentence
from app.agent.safety.risk import DeterministicRiskPolicy, RiskAssessment
from app.agent.verification.verifier import DeterministicVerifier
from app.agent.vision.grounding import ground_reason
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.domain.enums import ActionType, RiskLevel, StepStatus, TaskStatus
from app.domain.state_machine import transition
from app.models.task import LLMDecision, RecoveryAttempt, TaskStep, Verification
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import (
    BoundingBox,
    EventBus,
    FinalEvent,
    RecoveryEvent,
    StatusEvent,
    StepEvent,
    TaskStats,
    Viewport,
)
from app.services.storage import ScreenshotStorage

__all__ = [
    "Orchestrator",
    "RunOutcome",
    "UnitOfWorkFactory",
    "database_unit_of_work",
]

logger = get_logger(__name__)

UnitOfWorkFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]

# Spec section 7: one credit per step, plus one more for a vision-grounded
# one. Nothing is reserved or settled yet; this is the number the live view
# shows, and the billing part will reconcile against it.
CREDITS_PER_STEP: Final = 1
CREDITS_PER_VISION_STEP: Final = 2

# Failure reasons are shown to a user and stored. They are written here rather
# than taken from an exception message, because an exception can carry a URL,
# a selector, or a line of page text, and spec section 10 says none of those
# leave the process.
_SAFE_REASONS: Final[tuple[tuple[type[BaseException], str], ...]] = (
    (PlannerOutputError, "The planner could not produce a valid decision for this page."),
    (BrowserError, "The browser could not carry out the planned action."),
    # These three are ordinary weather rather than a bug in the task, and a
    # user who is told "internal error" has no idea whether to try again.
    (LLMRateLimited, "The model provider is rate limiting this account. Try again shortly."),
    (LLMTimeout, "The model did not answer in time."),
    (LLMUnavailable, "The model provider is unavailable."),
)
_FALLBACK_REASON: Final = "The task stopped after an internal error ({name})."


def database_unit_of_work(database: Database) -> UnitOfWorkFactory:
    """A factory that opens a session, and commits it when the block ends.

    Background work has no request to hang a transaction on, so the loop opens
    its own. The Unit of Work rule still holds: whoever opens the session
    commits it, and here that is this context manager rather than anything
    inside the loop.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        async with database.session() as session:
            yield UnitOfWork(session)

    return factory


@dataclass(slots=True)
class _Tally:
    """Counters the run needs after the loop has thrown its way out.

    ``_iterate`` always leaves through an exception, so nothing it returns is
    ever read. Anything the final event needs has to be accumulated somewhere
    the caller still holds.
    """

    vision_steps: int = 0
    vision_calls: int = 0
    vision_latency_ms: int = 0
    vision_tokens: int = 0
    vision_failures: int = 0


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """How a run ended, for the caller and for the final event."""

    status: TaskStatus
    final_answer: str | None = None
    failure_reason: str | None = None
    steps: int = 0
    duration_ms: int = 0
    credits_used: int = 0
    cache_hit: bool = False
    # Spec section 9 wants a vision invocation rate, its latency and its cost
    # per task. Logged with the run and carried here for the caller.
    vision_calls: int = 0
    vision_latency_ms: int = 0
    vision_tokens: int = 0
    vision_failures: int = 0


class _Stop(Exception):  # noqa: N818
    """Internal: leave the loop with a decided outcome.

    An exception rather than a return value because the loop body has several
    places that end a run, and threading a sentinel back through each of them
    would make the happy path harder to read than the failure paths.
    """

    def __init__(
        self,
        status: TaskStatus,
        *,
        final_answer: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        super().__init__(status.value)
        self.status = status
        self.final_answer = final_answer
        self.failure_reason = failure_reason


class Orchestrator:
    """Runs one task from submitted to finished."""

    def __init__(
        self,
        *,
        browser: AgentBrowser,
        planner: Planner,
        storage: ScreenshotStorage,
        events: EventBus,
        unit_of_work: UnitOfWorkFactory,
        settings: Settings,
        verifier: Verifier | None = None,
        cache: FlowCache | None = None,
        grounder: Grounder | None = None,
        risk_policy: RiskPolicy | None = None,
        approvals: ApprovalGate | None = None,
        recovery: Recovery | None = None,
        sessions: SessionManager | None = None,
        cancel_signal: CancelSignal | None = None,
        budget: TaskBudget | None = None,
    ) -> None:
        self._browser = browser
        self._planner = planner
        self._storage = storage
        self._events = events
        self._unit_of_work = unit_of_work
        self._settings = settings
        self._budget = budget

        # Every one of these is a part that has not been built. The defaults
        # are named after what they do not do, so a stack trace or a log line
        # says which scaffold is still in place.
        self._verifier: Verifier = verifier or DeterministicVerifier()
        self._cache: FlowCache = cache or NoCache()
        self._grounder: Grounder = grounder or NoGrounding()
        self._risk: RiskPolicy = risk_policy or DeterministicRiskPolicy()
        self._approvals: ApprovalGate = approvals or DenyApprovals()
        self._recovery: Recovery = recovery or RecoveryRunner(
            max_attempts=settings.AGENT_MAX_RETRIES_PER_STEP
        )
        self._sessions: SessionManager = sessions or PageStateSessions()

        # Not a stand-in for a later part: a run nobody supervises genuinely
        # has nothing that could cancel it.
        self._cancel: CancelSignal = cancel_signal or NeverCancelled()

    # ------------------------------------------------------------- the run

    async def run(self, task_id: uuid.UUID) -> RunOutcome:
        """Drive one task to a terminal state.

        A final event is published whatever happens, including when the
        settling write itself fails: a client that never receives one is left
        watching a spinner forever, which is a worse failure than the one that
        caused it.
        """
        started = time.monotonic()
        budget = self._budget or TaskBudget.from_settings(self._settings)

        status = TaskStatus.EXECUTION_FAILED
        final_answer: str | None = None
        failure_reason: str | None = _FALLBACK_REASON.format(name="never started")
        hint: CachedHint | None = None
        tally = _Tally()

        try:
            instruction = await self._instruction(task_id)
            await self._set_status(task_id, TaskStatus.PLANNING)
            hint = await self._cache.lookup(instruction)
            await self._iterate(task_id, instruction, hint, budget, tally)
        except _Stop as stop:
            status = stop.status
            final_answer = stop.final_answer
            failure_reason = stop.failure_reason
        except BudgetExceeded as exc:
            status, failure_reason = _budget_outcome(exc)
            logger.info("task_budget_exceeded", task_id=str(task_id), reason=exc.reason.value)
        except Exception as exc:
            if self._cancel.is_cancelled():
                # Closing the context under a running action raises, and that
                # exception is a consequence of the cancel rather than a
                # failure of the task. Reporting it as one would show the user
                # an error for something they asked for.
                status = TaskStatus.CANCELLED
                failure_reason = "The task was cancelled."
            else:
                status = TaskStatus.EXECUTION_FAILED
                failure_reason = _safe_reason(exc)
            # The detail stays here, where it is useful and nobody untrusted
            # reads it. Only the sanitised sentence above reaches the client.
            logger.exception("task_failed", task_id=str(task_id), error=type(exc).__name__)

        duration_ms = int((time.monotonic() - started) * 1000)
        settled = await self._settle(task_id, status, final_answer, failure_reason)

        outcome = RunOutcome(
            status=settled,
            final_answer=final_answer if settled is TaskStatus.COMPLETED else None,
            failure_reason=failure_reason if settled is not TaskStatus.COMPLETED else None,
            steps=budget.steps_used,
            duration_ms=duration_ms,
            credits_used=_credits(budget.steps_used, tally.vision_steps),
            cache_hit=hint is not None,
            vision_calls=tally.vision_calls,
            vision_latency_ms=tally.vision_latency_ms,
            vision_tokens=tally.vision_tokens,
            vision_failures=tally.vision_failures,
        )
        if tally.vision_calls:
            logger.info(
                "task_vision_summary",
                task_id=str(task_id),
                calls=tally.vision_calls,
                failures=tally.vision_failures,
                latency_ms=tally.vision_latency_ms,
                tokens=tally.vision_tokens,
            )
        await self._publish_final(task_id, outcome)
        return outcome

    async def _iterate(
        self,
        task_id: uuid.UUID,
        instruction: str,
        hint: CachedHint | None,
        budget: TaskBudget,
        tally: _Tally,
    ) -> None:
        """The loop proper. Only ever leaves by raising ``_Stop``."""
        history: list[StepSummary] = []
        # Carried between iterations: the state read after one action is the
        # state the next one plans against, so a step costs one perception
        # rather than two.
        state: PageState | None = None

        while True:
            budget.check()
            self._stop_if_cancelled()

            # A cancel can also arrive as a status write from another worker,
            # which the flag above knows nothing about.
            current = await self._current_status(task_id)
            if current.is_terminal:
                raise _Stop(current, failure_reason="The task was stopped.")

            if state is None:
                state = await self._browser.perceive()
            self._stop_if_cancelled()

            planned = await self._planner.decide(instruction, state, history, _hint_text(hint))
            self._stop_if_cancelled()

            grounded = await self._ground(planned.decision, state, tally)
            decision = grounded.decision
            if grounded.used_vision:
                tally.vision_steps += 1

            element = self._resolve_target(decision, state)
            assessment = _assess(self._risk, decision, state)
            risk = RiskLevel.highest(assessment.level, decision.risk_level)
            step_number = budget.consume_step()

            if risk is RiskLevel.HIGH or decision.requires_approval:
                await self._gate(
                    task_id, step_number, decision, risk, element, assessment.reasons, state
                )

            await self._set_status(task_id, TaskStatus.ACTING)
            action = build_action(decision, element)
            result = await self._browser.execute(action)

            await self._set_status(task_id, TaskStatus.VERIFYING)
            self._stop_if_cancelled()

            # Wait for the page to stop moving before reading it. Checking a
            # click against a page that is still assembling itself fails steps
            # that worked, and the agent then recovers from a success.
            await self._browser.settle()
            after = await self._browser.perceive()
            verdict, after, attempts, tried = await self._verify(
                task_id, step_number, decision, element, state, after, result
            )

            shot = await self._browser.screenshot()
            await self._record(
                task_id=task_id,
                step_number=step_number,
                decision=decision,
                planned=planned,
                grounded=grounded,
                element=element,
                state=state,
                verdict=verdict,
                attempts=attempts,
                risk=risk,
                shot=shot,
                after=after,
            )

            history.append(
                StepSummary(
                    step_number=step_number,
                    action=decision.action.value,
                    target=decision.target,
                    verified=verdict.ok,
                    # An extract whose result never reaches the planner is an
                    # action the agent cannot act on. It reads the page and
                    # then has no idea what it read.
                    extracted=result.extracted,
                )
            )

            if not verdict.ok:
                raise _Stop(
                    TaskStatus.EXECUTION_FAILED,
                    failure_reason=_failure_reason(step_number, decision, element, verdict, tried),
                )
            if decision.action is ActionType.DONE:
                raise _Stop(TaskStatus.COMPLETED, final_answer=decision.final_answer)

            # The page as it is now is what the next step plans against.
            state = after
            await self._set_status(task_id, TaskStatus.PLANNING)

    # -------------------------------------------------------- loop helpers

    async def _ground(
        self, decision: Decision, state: PageState, tally: "_Tally"
    ) -> GroundingOutcome:
        """Look at the page, but only when the DOM is not enough.

        The screenshot is taken here rather than on every step, because it is
        the expensive half: grounding costs a frame, a vision call and a
        second or two, and the ordinary step needs none of them.
        """
        reason = ground_reason(
            decision.needs_vision,
            decision.confidence,
            state,
            threshold=self._settings.VISION_CONFIDENCE_THRESHOLD,
            min_elements=self._settings.VISION_MIN_ELEMENTS,
        )
        if reason is None:
            return GroundingOutcome(decision=decision)

        shot = await self._browser.screenshot()
        outcome = await self._grounder.resolve(decision, state, shot.jpeg_bytes, reason)

        tally.vision_calls += 1
        tally.vision_latency_ms += outcome.latency_ms
        tally.vision_tokens += outcome.tokens
        if outcome.error is not None:
            # Spec section 4.9: vision being down degrades to DOM-only. It is
            # recorded and it never fails the task.
            tally.vision_failures += 1

        return outcome

    def _stop_if_cancelled(self) -> None:
        """Leave the loop at a phase boundary if a cancel has arrived.

        Deliberately not checked in the middle of a step: a step that has run
        still has to be recorded, or the activity log disagrees with what the
        browser actually did.
        """
        if self._cancel.is_cancelled():
            raise _Stop(TaskStatus.CANCELLED, failure_reason="The task was cancelled.")

    def _resolve_target(self, decision: Decision, state: PageState) -> ElementInfo | None:
        """Find the element the decision named, or refuse to act.

        The planner checks this too. It is checked again because grounding sits
        between the two and may change the index, and because this is the last
        point before a selector is handed to a browser: spec section 4.11 wants
        the target-existence guard immediately upstream of execution, not
        merely somewhere upstream.
        """
        if decision.target_index is None:
            return None

        element = state.element_at(decision.target_index)
        if element is None:
            raise _Stop(
                TaskStatus.EXECUTION_FAILED,
                failure_reason="The planner chose an element that is not on the page.",
            )
        return element

    async def _gate(
        self,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        risk: RiskLevel,
        element: ElementInfo | None,
        reasons: list[str],
        state: PageState,
    ) -> None:
        """Park on a human.

        Three outcomes, and they are not the same. Approved carries on.
        Rejected is somebody saying no, which is REJECTED. Nobody answering is
        not a rejection: spec section 4.3 and decision A4 send that to
        TIMED_OUT and keep REJECTED for a human who actually looked.
        """
        await self._set_status(task_id, TaskStatus.AWAITING_APPROVAL)
        shot = await self._browser.screenshot()
        answer = await self._approvals.request(
            task_id=task_id,
            step_number=step_number,
            decision=decision,
            risk=risk,
            screenshot=to_base64(shot),
            bbox=_bbox_dict(element),
            # The prompt has to say why. "Click 'Pay now' on SkyFare. Flagged
            # because the control says 'pay now'." is a question somebody can
            # answer; "approve step 6" is homework.
            reasons=reasons,
            site=_site_of(state),
        )
        if answer.approved:
            return

        if answer.expired:
            raise _Stop(TaskStatus.TIMED_OUT, failure_reason="approval timed out")

        raise _Stop(
            TaskStatus.REJECTED,
            failure_reason=answer.reason or "The action was not approved.",
        )

    async def _verify(
        self,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        element: ElementInfo | None,
        before: PageState,
        after: PageState,
        result: ActionResult,
    ) -> tuple[Verdict, PageState, list[Attempt], list[str]]:
        """Check the step, and try to rescue it if the check failed.

        Returns the page as it ended up, which is not always the page that
        failed: a strategy that works has navigated, dismissed something or
        otherwise moved on, and the next step has to plan against that.
        """
        if not result.ok:
            # Nothing happened, so there is nothing to check. Running the
            # verifier anyway invites a lenient check to call a step that never
            # ran a success. Recovery still gets its turn: a click that missed
            # is exactly what spec section 4.8 is for.
            verdict = Verdict(
                ok=False,
                method=decision.expected_outcome.type,
                expected={
                    "type": decision.expected_outcome.type.value,
                    "value": decision.expected_outcome.value,
                },
                actual={
                    "reason": "The action did not run.",
                    "errorCode": result.error_code.value if result.error_code else None,
                },
                detail="The action did not run.",
            )
        else:
            verdict = await self._verifier.check(decision, before, after, result)

        if verdict.ok:
            return verdict, after, [], []

        await self._set_status(task_id, TaskStatus.RECOVERING)
        context = RecoveryContext(
            decision=decision,
            element=element,
            before=before,
            after=after,
            result=result,
            verdict=verdict,
            browser=self._browser,
            verifier=self._verifier,
            on_attempt=self._attempt_announcer(task_id, step_number),
        )
        recovered = await self._recovery.run(context)

        if recovered is not None:
            # Back to verifying: the step did in the end do what it should.
            await self._set_status(task_id, TaskStatus.VERIFYING)
            # `context.after` is where the successful retry left the browser.
            # Returning the state that failed instead would have the next step
            # plan against a page the agent has already navigated away from.
            return recovered, context.after, context.attempts, context.tried

        return verdict, context.after, context.attempts, context.tried

    def _attempt_announcer(
        self, task_id: uuid.UUID, step_number: int
    ) -> Callable[[Attempt], Awaitable[None]]:
        """Publish each rung as it is tried.

        Deliberately live, ahead of the row. A ladder can take several seconds
        and the live view exists to show a person what the agent is doing; the
        durable record is the `recovery_attempts` rows, which are written with
        the step and served by GET /tasks/{id}/recovery-attempts. A step event
        is history and leads with the database; an attempt is commentary on
        one, and the step it belongs to does not exist yet.
        """

        async def announce(attempt: Attempt) -> None:
            await self._events.publish(
                task_id,
                RecoveryEvent(
                    task_id=task_id,
                    step_number=step_number,
                    attempt_number=attempt.number,
                    strategy=attempt.label,
                    outcome=attempt.outcome.value,
                ),
            )

        return announce

    # ------------------------------------------------------------ persistence

    async def _record(
        self,
        *,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        planned: PlannerResult,
        grounded: GroundingOutcome,
        element: ElementInfo | None,
        state: PageState,
        verdict: Verdict,
        attempts: list[Attempt],
        risk: RiskLevel,
        shot: Screenshot,
        after: PageState,
    ) -> None:
        """Write the step, then tell anyone watching.

        The image goes to storage first so the row can carry a reference that
        already resolves. A rolled-back transaction then leaves an orphaned
        file, which `delete_task` sweeps and nothing else notices; the reverse
        order would leave a committed row pointing at nothing.
        """
        sensitive = _is_sensitive(element)
        reference = await self._storage.save(task_id, step_number, shot.jpeg_bytes)
        tab_id = self._sessions.tab_id(state)
        bbox = _bbox_dict(element)

        async with self._unit_of_work() as uow:
            step = await uow.task_steps.add(
                TaskStep(
                    task_id=task_id,
                    step_number=step_number,
                    action=decision.action,
                    target=decision.target,
                    target_index=decision.target_index,
                    value_redacted=_redacted_value(decision, sensitive),
                    tab_id=tab_id,
                    selector_used=selector_for(element),
                    bbox=bbox,
                    screenshot_ref=reference,
                    status=StepStatus.VERIFIED if verdict.ok else StepStatus.FAILED,
                )
            )
            await uow.llm_decisions.add(
                LLMDecision(
                    step_id=step.id,
                    prompt_version=planned.prompt_version,
                    model_name=planned.llm_response.model_name,
                    provider=planned.llm_response.provider,
                    reasoning_summary=decision.reasoning or None,
                    confidence=decision.confidence,
                    risk_level=risk,
                    used_vision=grounded.used_vision,
                    input_tokens=planned.llm_response.usage.input_tokens,
                    output_tokens=planned.llm_response.usage.output_tokens,
                    latency_ms=planned.llm_response.latency_ms,
                    raw_output=_safe_raw_output(planned.llm_response.parsed, sensitive),
                )
            )
            for attempt in attempts:
                await uow.recovery_attempts.add(
                    RecoveryAttempt(
                        step_id=step.id,
                        attempt_number=attempt.number,
                        strategy_used=attempt.strategy,
                        outcome=attempt.outcome,
                        error=attempt.error,
                    )
                )
            await uow.verifications.add(
                Verification(
                    step_id=step.id,
                    # What was compared and what was found, so a step can be
                    # argued about later from the row alone.
                    expected_outcome=verdict.expected,
                    actual_outcome=verdict.actual,
                    verified=verdict.ok,
                    verification_method=verdict.method,
                )
            )
        # Committed. Only now does the live view learn about the step.

        await self._events.publish(
            task_id,
            StepEvent(
                task_id=task_id,
                step_number=step_number,
                action=decision.action.value,
                target=decision.target,
                value=_redacted_value(decision, sensitive),
                verified=verdict.ok,
                screenshot=to_base64(shot),
                bbox=BoundingBox(**bbox) if bbox else None,
                viewport=Viewport(width=shot.viewport_width, height=shot.viewport_height),
                url=after.url,
                tab_id=tab_id,
                reasoning=decision.reasoning or None,
                confidence=decision.confidence,
                used_vision=grounded.used_vision,
            ),
        )

    async def _instruction(self, task_id: uuid.UUID) -> str:
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            return task.instruction

    async def _current_status(self, task_id: uuid.UUID) -> TaskStatus:
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            return TaskStatus(task.status)

    async def _set_status(self, task_id: uuid.UUID, new_status: TaskStatus) -> None:
        """Move the task on, then say so. Persist first, publish second.

        A task that is already terminal was finished by somebody else while
        this step was in flight, which for a cancel is exactly the intent. The
        state machine would refuse the move, and rightly, but that refusal is
        not a bug to report: the loop stops and the other writer wins.
        """
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            existing = TaskStatus(task.status)
            if existing.is_terminal:
                raise _Stop(
                    existing,
                    failure_reason="The task was stopped while a step was running.",
                )
            previous = transition(task, new_status)

        await self._events.publish(
            task_id,
            StatusEvent(task_id=task_id, status=new_status, previous_status=previous),
        )

    async def _settle(
        self,
        task_id: uuid.UUID,
        status: TaskStatus,
        final_answer: str | None,
        failure_reason: str | None,
    ) -> TaskStatus:
        """Write the terminal state, and report what it actually became.

        A task cancelled from the API is already terminal by the time the loop
        notices, and the state machine refuses to move it again. That refusal
        is the right answer, not an error: the cancel won, and the run reports
        the status the database holds rather than the one it wanted.
        """
        try:
            async with self._unit_of_work() as uow:
                task = await uow.tasks.get_or_404(task_id)
                existing = TaskStatus(task.status)
                if existing.is_terminal:
                    return existing

                previous = transition(task, status)
                task.finished_at = datetime.now(UTC)
                if final_answer is not None and status is TaskStatus.COMPLETED:
                    task.final_answer = {"text": final_answer}
                if status is not TaskStatus.COMPLETED:
                    task.failure_reason = failure_reason
        except Exception:
            # The final event still has to go out, so this cannot be fatal.
            logger.exception("task_settle_failed", task_id=str(task_id), target=status.value)
            return status

        await self._events.publish(
            task_id,
            StatusEvent(task_id=task_id, status=status, previous_status=previous),
        )
        return status

    async def _publish_final(self, task_id: uuid.UUID, outcome: RunOutcome) -> None:
        await self._events.publish(
            task_id,
            FinalEvent(
                task_id=task_id,
                status=outcome.status,
                final_answer=outcome.final_answer,
                failure_reason=outcome.failure_reason,
                stats=TaskStats(
                    steps=outcome.steps,
                    duration_ms=outcome.duration_ms,
                    credits_used=outcome.credits_used,
                    cache_hit=outcome.cache_hit,
                ),
            ),
        )


# ----------------------------------------------------------------- helpers


def _failure_reason(
    step_number: int,
    decision: Decision,
    element: ElementInfo | None,
    verdict: Verdict,
    tried: list[str],
) -> str:
    """What a person is told when a step could not be rescued.

    Names the action, the target and what was tried. "Step 3 failed" tells a
    user nothing they can act on; "could not click Search flights, tried
    re-reading the page and the stored selector" tells them the button is
    probably not there any more.
    """
    if tried:
        context = RecoveryContext(
            decision=decision,
            element=element,
            before=_EMPTY_STATE,
            after=_EMPTY_STATE,
            result=_NO_RESULT,
            verdict=verdict,
            browser=None,  # type: ignore[arg-type]
            verifier=None,  # type: ignore[arg-type]
        )
        return f"Step {step_number}: {failure_sentence(context, tried)}"

    return f"Step {step_number} did not do what was expected. {verdict.detail}".strip()


_EMPTY_STATE: Final = PageState(url="")
_NO_RESULT: Final = ActionResult(ok=False, duration_ms=0, url_before="", url_after="")


def _assess(policy: RiskPolicy, decision: Decision, state: PageState) -> RiskAssessment:
    """The policy's verdict, with its reasons when it has any.

    The `RiskPolicy` seam only promises a level, because that is all the loop
    needs to decide. The deterministic policy also knows why, and the approval
    prompt is much better for having it, so it is used when it is there.
    """
    evaluate = getattr(policy, "evaluate", None)
    if callable(evaluate):
        return cast(RiskAssessment, evaluate(decision, state))
    return RiskAssessment(policy.assess(decision, state), [])


def _site_of(state: PageState) -> str:
    """A name for the page a person would recognise."""
    host = urlsplit(state.url).hostname or ""
    return state.title.split("|")[-1].strip() or host


def _hint_text(hint: CachedHint | None) -> str | None:
    return hint.text if hint else None


def _credits(steps: int, vision_steps: int) -> int:
    plain = max(0, steps - vision_steps)
    return plain * CREDITS_PER_STEP + vision_steps * CREDITS_PER_VISION_STEP


def _budget_outcome(exc: BudgetExceeded) -> tuple[TaskStatus, str]:
    """Out of time is a timeout; out of steps is a failure to finish.

    Different states because they mean different things to a user. A task that
    ran out of seconds may well succeed on a retry; one that spent twenty-five
    steps without reaching an answer is stuck, and retrying it unchanged will
    spend twenty-five more.
    """
    if exc.reason is BudgetReason.MAX_TIME:
        return TaskStatus.TIMED_OUT, "The task ran out of time."
    return TaskStatus.EXECUTION_FAILED, "The task ran out of steps before it finished."


def _safe_reason(exc: BaseException) -> str:
    for kind, reason in _SAFE_REASONS:
        if isinstance(exc, kind):
            return reason
    return _FALLBACK_REASON.format(name=type(exc).__name__)


def _is_sensitive(element: ElementInfo | None) -> bool:
    """Would typing into this element produce a value we must never keep?

    The collector already masks what it recognises, so trusting its verdict
    keeps one rule in one place. The input type is checked as well because an
    empty password field has nothing to mask and so arrives unmarked.
    """
    if element is None:
        return False
    return element.is_masked or (element.input_type or "").lower() == "password"


def _redacted_value(decision: Decision, sensitive: bool) -> str | None:
    if decision.value is None:
        return None
    return MASK if sensitive else decision.value


def _safe_raw_output(parsed: dict[str, Any], sensitive: bool) -> dict[str, Any]:
    """The model's own output, with a typed secret taken back out.

    Easy to miss: the decision is stored verbatim for debugging, and a decision
    that types a password carries that password in its ``value`` field. The
    step row is careful about this; the raw output has to be too, or section 10
    is satisfied in one column and broken in the next.
    """
    if not sensitive or "value" not in parsed:
        return parsed
    return {**parsed, "value": MASK}


def _bbox_dict(element: ElementInfo | None) -> dict[str, int] | None:
    if element is None:
        return None
    box = element.bbox
    return {"x": box.x, "y": box.y, "width": box.width, "height": box.height}
