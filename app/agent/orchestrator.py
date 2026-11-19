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

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.agent.browser.action_builder import build_action, selector_for
from app.agent.browser.actions import ClickAction
from app.agent.browser.driver import ActionResult
from app.agent.browser.errors import ActionErrorCode, BrowserClosedError, BrowserError
from app.agent.browser.screenshots import Screenshot, to_base64
from app.agent.budget import BudgetExceeded, BudgetReason, TaskBudget
from app.agent.cache.flow import Locator, ReplayStep, hint_for
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
    ReplayPlan,
    RiskPolicy,
    SessionManager,
    Verdict,
    Verifier,
)
from app.agent.llm.base import LLMRateLimited, LLMTimeout, LLMUnavailable
from app.agent.perception.dom_extractor import MASK, ElementInfo, PageNotReadyError
from app.agent.perception.overlays import is_blocked, safe_closers
from app.agent.perception.state import PageState
from app.agent.planner.clarify import with_answers
from app.agent.planner.outline import MAX_STEP_CHARS
from app.agent.planner.planner import (
    Planner,
    PlannerOutputError,
    PlannerResult,
    StepSummary,
    input_target_problem,
    not_editable_problem,
)
from app.agent.planner.schema import Decision, ExpectedOutcome
from app.agent.recovery.runner import RecoveryRunner, failure_sentence
from app.agent.safety.payment import is_payment_page, pays, wants_a_purchase
from app.agent.safety.risk import DeterministicRiskPolicy, RiskAssessment
from app.agent.verification.verifier import DeterministicVerifier, page_changed
from app.agent.vision.grounding import ground_reason
from app.core import metrics
from app.core.config import Settings
from app.core.logging import get_logger, step_context, task_context
from app.db.session import Database
from app.domain.credits import CreditPrices, Usage, credits_for_usage
from app.domain.enums import ActionType, RiskLevel, StepStatus, TaskStatus, VerificationMethod
from app.domain.state_machine import transition
from app.models.billing import OutboxKind, release_key, settle_key
from app.models.task import LLMDecision, RecoveryAttempt, Task, TaskStep, Verification
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import (
    BoundingBox,
    EventBus,
    FinalEvent,
    PlanEvent,
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

# Failure reasons are shown to a user and stored. They are written here rather
# than taken from an exception message, because an exception can carry a URL,
# a selector, or a line of page text, and spec section 10 says none of those
# leave the process.
_SAFE_REASONS: Final[tuple[tuple[type[BaseException], str], ...]] = (
    (PlannerOutputError, "The planner could not produce a valid decision for this page."),
    (
        BrowserClosedError,
        "The browser window was closed while the task was running. "
        "Run it again and leave the window open.",
    ),
    (BrowserError, "The browser could not carry out the planned action."),
    # A page, or a browser that was slow to start, did not answer in time. Both
    # Playwright's own timeout and asyncio's land here, since neither is a bug.
    (PlaywrightTimeoutError, "The page took too long to respond. Try the task again."),
    (TimeoutError, "Something took too long to respond. Try the task again."),
    # The page was still changing after every read perception made of it.
    (
        PageNotReadyError,
        "The page kept changing and could not be read. Try the task again.",
    ),
    # These three are ordinary weather rather than a bug in the task, and a
    # user who is told "internal error" has no idea whether to try again.
    (LLMRateLimited, "The model provider is rate limiting this account. Try again shortly."),
    (LLMTimeout, "The model did not answer in time."),
    (LLMUnavailable, "The model provider is unavailable."),
)
_BLOCKED_STATUS: Final = re.compile(r"HTTP (401|403|429)")
_NOT_RUN_HINTS: Final[dict[ActionErrorCode, str]] = {
    ActionErrorCode.NAVIGATION_FAILED: "the page could not be loaded; check the address",
    ActionErrorCode.TIMEOUT: "the page or element did not respond in time",
    ActionErrorCode.ELEMENT_NOT_FOUND: "the element was not on the page",
    ActionErrorCode.NOT_INTERACTABLE: "the element could not be clicked or typed into",
    ActionErrorCode.TAB_NOT_FOUND: "that tab no longer exists",
    ActionErrorCode.TAB_LIMIT_REACHED: "too many tabs are open",
}


def _why_not_run(result: ActionResult) -> str:
    """A sentence for the failure reason: what stopped the action, in plain words.

    The driver already reduces Playwright's message to one line with no page
    content, so it is safe to show. Without it every failure read the same.
    """
    hint = _NOT_RUN_HINTS.get(result.error_code) if result.error_code else None
    if result.error_code is ActionErrorCode.NAVIGATION_FAILED and _BLOCKED_STATUS.search(
        result.error_message or ""
    ):
        # Not a typo in the address: the site turned the automated browser away.
        # Checking the address, as the generic hint says, sends the person the
        # wrong way.
        return (
            f"The site refused to open for the agent's browser ({result.error_message}). "
            "Sites with bot protection do this. Run the agent in your own Chrome "
            "(set AGENT_CDP_URL and start scripts/chrome_debug.ps1) and try again."
        )
    base = "The action did not run"
    if hint:
        base = f"{base}: {hint}"
    if result.error_message:
        base = f"{base} ({result.error_message})"
    return f"{base}."


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
    # Replay, for the final event and for the flow's own statistics.
    replayed_steps: int = 0
    replayed_all: bool = False
    # The verified sequence, in a form another run could replay. Accumulated
    # as the loop goes because a completed task is the only thing worth
    # caching, and by then the states it was built from are gone.
    cacheable: list[ReplayStep] = field(default_factory=list)
    # The person answered questions about a page mid-run. The flow then holds
    # their answers (a date, a trip type), and the cache is looked up before
    # any page is read, so it would replay those answers for somebody else.
    asked_on_page: bool = False


@dataclass(slots=True)
class _PageSurvey:
    """Which pages have been read for questions, and which questions were skipped."""

    seen: set[str] = field(default_factory=set)
    # Asked and left unanswered. Not asked again on the next page: the person
    # already said they have no preference.
    skipped: list[str] = field(default_factory=list)


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
    replayed_steps: int = 0
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
        self._running_budget: TaskBudget | None = None

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
        with task_context(task_id):
            return await self._run(task_id)

    async def _run(self, task_id: uuid.UUID) -> RunOutcome:
        """The run itself, with the task id already bound for logging."""
        started = time.monotonic()
        budget = self._budget or TaskBudget.from_settings(self._settings)
        self._running_budget = budget

        status = TaskStatus.EXECUTION_FAILED
        final_answer: str | None = None
        failure_reason: str | None = _FALLBACK_REASON.format(name="never started")
        plan: ReplayPlan | None = None
        instruction = ""
        tally = _Tally()

        try:
            instruction = await self._instruction(task_id)
            await self._set_status(task_id, TaskStatus.PLANNING)
            pages = _PageSurvey()
            if self._settings.AGENT_CLARIFY:
                instruction = await self._clarify(task_id, instruction, pages)
            plan = await self._cache.plan(task_id, instruction)
            # Spec section 6: the outline is made in the loop, once the agent
            # has opened the site, read its form and had its questions answered.
            preview = plan is None and self._settings.AGENT_PLAN_PREVIEW
            await self._iterate(
                task_id, instruction, plan, budget, tally, preview=preview, pages=pages
            )
        except _Stop as stop:
            status = stop.status
            final_answer = stop.final_answer
            failure_reason = stop.failure_reason
        except BudgetExceeded as exc:
            status, failure_reason = _budget_outcome(exc)
            logger.info("task_budget_exceeded", reason=exc.reason.value)
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
            logger.exception("task_failed", error=type(exc).__name__)

        duration_ms = int((time.monotonic() - started) * 1000)
        usage = Usage(
            steps=budget.steps_used,
            vision_steps=tally.vision_steps,
            replayed_steps=tally.replayed_steps,
        )
        settled = await self._settle(task_id, status, final_answer, failure_reason, usage)
        await self._remember(task_id, instruction, settled, plan, tally)

        outcome = RunOutcome(
            status=settled,
            final_answer=final_answer if settled is TaskStatus.COMPLETED else None,
            failure_reason=failure_reason if settled is not TaskStatus.COMPLETED else None,
            steps=budget.steps_used,
            duration_ms=duration_ms,
            credits_used=credits_for_usage(usage, CreditPrices.from_settings(self._settings)),
            cache_hit=plan is not None,
            replayed_steps=tally.replayed_steps,
            vision_calls=tally.vision_calls,
            vision_latency_ms=tally.vision_latency_ms,
            vision_tokens=tally.vision_tokens,
            vision_failures=tally.vision_failures,
        )
        if tally.vision_calls:
            logger.info(
                "task_vision_summary",
                calls=tally.vision_calls,
                failures=tally.vision_failures,
                latency_ms=tally.vision_latency_ms,
                tokens=tally.vision_tokens,
            )
        _publish_metrics(outcome)
        await self._publish_final(task_id, outcome)
        return outcome

    async def _iterate(
        self,
        task_id: uuid.UUID,
        instruction: str,
        plan: ReplayPlan | None,
        budget: TaskBudget,
        tally: _Tally,
        *,
        preview: bool = False,
        pages: _PageSurvey | None = None,
    ) -> None:
        """The loop proper. Only ever leaves by raising ``_Stop``."""
        pages = pages or _PageSurvey()
        outline: list[str] = []
        # The plan item the last step worked on, numbered from 1.
        plan_at: int | None = None
        history: list[StepSummary] = []
        login_pauses = 0
        payment_pauses = 0
        detail_pauses = 0
        idle_scrolls = 0
        idle_waits = 0
        idle_extracts = 0
        refused_sites = 0
        missed_forecasts = 0
        planner_retries = 0
        purchase = wants_a_purchase(instruction)
        # Carried between iterations: the state read after one action is the
        # state the next one plans against, so a step costs one perception
        # rather than two.
        state: PageState | None = None
        hint: CachedHint | None = None

        if plan is not None:
            # Replay what worked before, then let the planner take over from
            # wherever that stopped. It always stops somewhere: the final
            # answer is never cached, so the last word is always the model's.
            state, hint = await self._replay(task_id, plan, budget, tally, history)

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

            state = await self._clear_overlays(state)

            if _login_wall(state, instruction):
                # The agent does not create accounts or guess credentials. It
                # hands the browser to the person, then looks at the page again.
                if login_pauses >= MAX_LOGIN_PAUSES:
                    raise _Stop(
                        TaskStatus.EXECUTION_FAILED,
                        failure_reason="The site still needs you to sign in or sign up.",
                    )
                login_pauses += 1
                await self._wait_for_login(task_id, len(history) + 1, state)
                state = None
                continue

            if purchase and payment_pauses == 0 and is_payment_page(state):
                # The booking has reached the till, and paying is the person's
                # to do. Only before the first hand-off: the confirmation the
                # person comes back to can still sit under /payment and name
                # the card they paid with, and that is a page to read, not a
                # second till. From then on the action check below is the guard.
                payment_pauses += 1
                await self._wait_for_payment(task_id, len(history) + 1, state)
                state = None
                continue

            if _on_a_site(state):
                # Read the page's form, ask about what nobody has said, then
                # plan, in that order: questions and a plan made from the task
                # text alone never knew the site also offers a round trip.
                for_task, instruction = await self._ask_about_page(
                    task_id, instruction, state, pages, tally
                )
                if preview and (
                    for_task
                    or not self._settings.AGENT_CLARIFY
                    or budget.steps_used >= PLAN_BY_STEP
                ):
                    preview = False
                    outline = await self._publish_outline(task_id, instruction, state)

            try:
                planned = await self._planner.decide(
                    instruction, state, history, _hint_text(hint), outline, plan_at
                )
            except PlannerOutputError:
                if planner_retries >= MAX_PLANNER_RETRIES:
                    raise
                # Two rejected answers against one reading of the page. The page
                # may still have been building itself (IRCTC lays out its form
                # after it loads), and the model's next answer is not its last
                # one: look again once before giving the task up.
                planner_retries += 1
                logger.info("planner_retry_on_a_fresh_page", retry=planner_retries)
                await self._browser.settle()
                state = None
                continue
            self._stop_if_cancelled()

            grounded = await self._ground(planned.decision, state, tally)
            decision = grounded.decision
            if grounded.used_vision:
                tally.vision_steps += 1
            if decision.target_index != planned.decision.target_index and (
                input_target_problem(decision, state) or not_editable_problem(decision, state)
            ):
                # Grounding moved the target onto an unnamed or covered input
                # that the planner was already refused for. Keep the planner's.
                logger.info(
                    "grounding_override_refused",
                    planned=planned.decision.target_index,
                    grounded=decision.target_index,
                )
                decision = planned.decision

            element = self._resolve_target(decision, state)

            if pays(decision, element):
                # Whatever the page looked like, the agent does not press Pay
                # or type a card. An approval would not do here: approving
                # lets the agent press it, and paying is never the agent's.
                if payment_pauses >= MAX_PAYMENT_PAUSES:
                    raise _Stop(
                        TaskStatus.EXECUTION_FAILED,
                        failure_reason=(
                            f"The booking on {_site_of(state) or 'the site'} was left at the "
                            "payment step: it was handed to you to pay twice, and the page "
                            "still asked for payment afterwards. The agent never pays itself. "
                            "Next time, pay in the browser window first, then continue."
                        ),
                    )
                payment_pauses += 1
                await self._wait_for_payment(task_id, len(history) + 1, state)
                state = None
                continue

            if decision.action is ActionType.WAIT and decision.requires_approval:
                # The planner needs something only the person has: a traveller's
                # name, a phone number, an address the goal never gave. It may
                # not invent one, so the person types it in the browser.
                if detail_pauses >= MAX_DETAIL_PAUSES:
                    raise _Stop(
                        TaskStatus.EXECUTION_FAILED,
                        failure_reason=(
                            "The site still needs details the agent was not given. "
                            "Add them to the task and run it again."
                        ),
                    )
                detail_pauses += 1
                await self._wait_for_details(task_id, len(history) + 1, state, decision.target)
                state = None
                continue

            # Before the step runs, so a newly added item is on the person's
            # list by the time the step that starts it appears.
            outline, plan_at = await self._follow_plan(task_id, outline, planned.decision, plan_at)

            assessment = _assess(self._risk, decision, state)
            risk = RiskLevel.highest(assessment.level, decision.risk_level)
            step_number = budget.consume_step()

            with step_context(step_number):
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
                # click against a page that is still assembling itself fails
                # steps that worked, and the agent then recovers from a success.
                await self._browser.settle()
                after = await self._browser.perceive()
                # An action that ran and moved the page hit its target, so if
                # the check still fails it is the planner's forecast that was
                # wrong. The recovery ladder fixes locators, not forecasts: it
                # would only re-click the same element three ways, at a settle
                # and often a ten-second timeout each, and a second click can
                # close the dropdown the first one opened. The planner, which
                # will see the page as it now is, gets the step back instead.
                forecast_only = (
                    result.ok
                    and decision.action in _FORECAST_ACTIONS
                    and missed_forecasts < MAX_MISSED_FORECASTS
                    and (
                        page_changed(state, after)
                        or decision.expected_outcome.type
                        in (
                            VerificationMethod.TEXT_PRESENT,
                            VerificationMethod.URL_CONTAINS,
                        )
                    )
                )
                verdict, after, attempts, tried = await self._verify(
                    task_id,
                    step_number,
                    decision,
                    element,
                    state,
                    after,
                    result,
                    rescue=not forecast_only,
                )
                missed = forecast_only and not verdict.ok

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
                    plan_step=plan_at,
                )

            if verdict.ok and decision.action is not ActionType.DONE:
                # Everything but the answer. A cached `done` would replay last
                # week's price as this week's, and a confident wrong answer is
                # the one failure a user cannot detect.
                tally.cacheable.append(_cacheable(decision, element))

            history.append(
                StepSummary(
                    step_number=step_number,
                    action=decision.action.value,
                    target=_history_target(decision, result, verdict),
                    verified=verdict.ok,
                    note=_history_note(verdict, missed=missed, action=decision.action),
                    # An extract whose result never reaches the planner is an
                    # action the agent cannot act on. It reads the page and
                    # then has no idea what it read.
                    extracted=result.extracted,
                )
            )

            if (
                not verdict.ok
                and login_pauses < MAX_LOGIN_PAUSES
                and _login_wall(after, instruction)
            ):
                # The click that failed was probably blocked by a sign-in
                # prompt. Ask the person to sign in, then carry on from the page
                # as they leave it.
                login_pauses += 1
                await self._wait_for_login(task_id, step_number, after)
                state = None
                continue

            if (
                not verdict.ok
                and decision.action is ActionType.SCROLL
                and idle_scrolls < MAX_IDLE_SCROLLS
            ):
                # A scroll that moved nothing is an answer, not a broken step:
                # there is no more page that way. Not judged by the page's
                # measured bottom: IRCTC's results page sat at its footer while
                # its height said otherwise, and the task was failed for it.
                # The step stays recorded as unverified, so the planner is told
                # it moved nothing, and it picks something else to do.
                idle_scrolls += 1
                state = after
                await self._return_to_planning(task_id)
                continue

            if (
                not verdict.ok
                and decision.action is ActionType.WAIT
                and idle_waits < MAX_IDLE_WAITS
            ):
                # The page had already finished loading, so the wait changed
                # nothing. That is news for the planner, not a broken step, and
                # it used to end the task: a planner that could not find a date
                # in a calendar waited "to see the page" and the run was over.
                idle_waits += 1
                state = after
                await self._return_to_planning(task_id)
                continue

            if (
                not verdict.ok
                and decision.action is ActionType.EXTRACT
                and idle_extracts < MAX_IDLE_EXTRACTS
            ):
                # The extracted element had no text. Tell the planner rather than
                # failing the task, so it can try another element or the whole page.
                idle_extracts += 1
                state = after
                await self._return_to_planning(task_id)
                continue

            if (
                not verdict.ok
                and decision.action is ActionType.NAVIGATE
                and refused_sites < MAX_REFUSED_SITES
                and not _address_in_goal(decision.value, instruction)
            ):
                # The agent chose this site itself, and the site would not open
                # for it (a 403 from bot protection, a 404, a timeout). That is
                # a reason to pick another site, not to give up the task. The
                # history line names the address, so it does not pick it again.
                refused_sites += 1
                state = None
                await self._return_to_planning(task_id)
                continue

            if missed:
                # The action ran and the page moved, so the target was right;
                # what did not come true is the planner's guess at what would
                # appear next ("2024" in a calendar it had not yet seen). The
                # ladder cannot fix a wrong guess, only re-click. The step
                # stays recorded as unverified and the planner, which now sees
                # the page as it really is, decides what to do from there.
                missed_forecasts += 1
                logger.info("forecast_missed", step=step_number, action=decision.action.value)
                state = after
                await self._return_to_planning(task_id)
                continue

            if not verdict.ok:
                raise _Stop(
                    TaskStatus.EXECUTION_FAILED,
                    failure_reason=_failure_reason(step_number, decision, element, verdict, tried),
                )
            idle_scrolls = 0
            idle_waits = 0
            idle_extracts = 0
            missed_forecasts = 0
            planner_retries = 0
            if decision.action is ActionType.DONE:
                raise _Stop(TaskStatus.COMPLETED, final_answer=decision.final_answer)

            # The page as it is now is what the next step plans against.
            state = after
            await self._set_status(task_id, TaskStatus.PLANNING)

    async def _replay(
        self,
        task_id: uuid.UUID,
        plan: ReplayPlan,
        budget: TaskBudget,
        tally: _Tally,
        history: list[StepSummary],
    ) -> tuple[PageState | None, CachedHint | None]:
        """Run the cached steps until one of them does not work.

        Every step still goes through the same action building, risk
        assessment, approval gate and verifier as a planned one, because a
        cached step is only evidence that this worked once. What it skips is
        the planner, which is the whole saving: no model call, no tokens, no
        second of latency per step.

        The first failure ends the replay and hands the page to the planner.
        It does not fail the task: a flow that has gone stale halfway is worth
        the steps it got right, and the planner can carry on from there.
        """
        state: PageState | None = None
        done = 0
        # The run reached here in PLANNING. Tracked rather than inferred,
        # because the state machine allows ACTING only from PLANNING and a
        # replay of several steps passes through the same phases each time:
        # the decision comes from the cache instead of the model, but deciding
        # is still the phase it happens in.
        phase = TaskStatus.PLANNING

        for step in plan.steps:
            budget.check()
            self._stop_if_cancelled()
            if step.needs_planner:
                # A masked value, which was never cached and never will be.
                break

            if phase is not TaskStatus.PLANNING:
                await self._set_status(task_id, TaskStatus.PLANNING)
                phase = TaskStatus.PLANNING

            if state is None:
                state = await self._browser.perceive()

            element = step.locator.resolve(state) if step.locator is not None else None
            if step.locator is not None and element is None:
                logger.info(
                    "replay_target_missing",
                    step=step.action.value,
                    target=step.locator.described,
                )
                break

            decision = step.as_decision(element)
            if pays(decision, element):
                # A flow cached before the agent stopped at payment can still
                # hold the click that paid. The planner takes over from here,
                # and the loop hands the payment to the person.
                logger.info("replay_reached_payment", step=step.action.value)
                break
            assessment = _assess(self._risk, decision, state)
            risk = RiskLevel.highest(assessment.level, decision.risk_level)
            step_number = budget.consume_step()

            with step_context(step_number):
                if risk is RiskLevel.HIGH or decision.requires_approval:
                    # A cached step can be a purchase. The gate does not care where
                    # the decision came from, and it must not: a flow that could
                    # replay a payment unattended would be the worst thing in here.
                    await self._gate(
                        task_id, step_number, decision, risk, element, assessment.reasons, state
                    )

                await self._set_status(task_id, TaskStatus.ACTING)
                phase = TaskStatus.ACTING
                result = await self._browser.execute(build_action(decision, element))

                await self._set_status(task_id, TaskStatus.VERIFYING)
                phase = TaskStatus.VERIFYING
                await self._browser.settle()
                after = await self._browser.perceive()
                verdict, after, attempts, _tried = await self._verify(
                    task_id, step_number, decision, element, state, after, result
                )

                shot = await self._browser.screenshot()
                await self._record(
                    task_id=task_id,
                    step_number=step_number,
                    decision=decision,
                    planned=None,
                    grounded=GroundingOutcome(decision=decision),
                    element=element,
                    state=state,
                    verdict=verdict,
                    attempts=attempts,
                    risk=risk,
                    shot=shot,
                    after=after,
                    replayed=True,
                )
                history.append(
                    StepSummary(
                        step_number=step_number,
                        action=decision.action.value,
                        target=decision.target,
                        verified=verdict.ok,
                        extracted=result.extracted,
                    )
                )

                state = after
                if not verdict.ok:
                    logger.info("replay_step_failed", step=step.action.value)
                    break

            tally.replayed_steps += 1
            tally.cacheable.append(_cacheable(decision, element))
            done += 1

        tally.replayed_all = done == len(plan.steps)
        remaining = plan.steps[done:]
        logger.info(
            "replay_finished",
            flow_id=str(plan.flow_id),
            replayed=done,
            of=len(plan.steps),
        )

        # Back to planning from wherever the last step left the task. Read from
        # the record, not from `phase`: a failed verification moves the task to
        # RECOVERING inside `_verify`, which this loop's own tracking never sees.
        await self._return_to_planning(task_id)
        text = hint_for(remaining)
        hint = CachedHint(text=text, flow_id=plan.flow_id) if text else None
        return state, hint

    async def _queue_settlement(
        self,
        uow: UnitOfWork,
        task: Task,
        usage: Usage | None,
    ) -> None:
        """Queue the charge for a run that has just ended.

        Writes a row rather than calling anybody: the payments service is a
        separate process and this is inside the transaction that finishes the
        task, so an HTTP call here would hold that transaction open for the
        length of somebody else's outage.

        Nothing happens when billing is off, when the task holds no reservation,
        or when there is no usage to charge, and none of those is an error: the
        first is the default, the second is every task submitted while billing
        was off, and the third is a run that stopped before its first step.

        The charge itself is on the row either way; only the call to the
        payments service is conditional.
        """
        if not self._settings.BILLING_ENABLED or usage is None:
            return

        reservation_id = task.credits_reservation_id
        if reservation_id is None:
            return

        actual = task.credits_used or 0
        # A run that took no steps is released rather than settled at zero. Both
        # leave the wallet in the same place; the ledger on the other side keeps
        # "cost nothing" and "measured at nothing" apart.
        if actual <= 0:
            await uow.billing_outbox.enqueue(
                OutboxKind.RELEASE,
                {"reservationId": str(reservation_id), "taskId": str(task.id)},
                idempotency_key=release_key(reservation_id),
            )
            return

        await uow.billing_outbox.enqueue(
            OutboxKind.SETTLE,
            {
                "reservationId": str(reservation_id),
                "taskId": str(task.id),
                "actualAmount": actual,
            },
            idempotency_key=settle_key(reservation_id),
        )
        logger.info("billing_settlement_queued", credits=actual, steps=usage.steps)

    async def _remember(
        self,
        task_id: uuid.UUID,
        instruction: str,
        settled: TaskStatus,
        plan: ReplayPlan | None,
        tally: _Tally,
    ) -> None:
        """Store what worked, and fold the replay into the flow's statistics.

        After the status is settled and before the final event, so the event
        reports what the cache actually holds. Neither call may take the run
        down with it: the task is over and its answer is already written, and
        losing a cache entry is not worth turning a completed task into a
        failed one.
        """
        try:
            if settled is TaskStatus.COMPLETED and not tally.asked_on_page:
                await self._cache.save(task_id, instruction, tuple(tally.cacheable))
            if plan is not None:
                await self._cache.settle(plan, replayed_all=tally.replayed_all)
        except Exception as exc:
            logger.warning("cache_write_failed", error_type=type(exc).__name__)

    # -------------------------------------------------------- loop helpers

    async def _ground(
        self, decision: Decision, state: PageState, tally: "_Tally"
    ) -> GroundingOutcome:
        """Look at the page, but only when the DOM is not enough.

        The screenshot is taken here rather than on every step, because it is
        the expensive half: grounding costs a frame, a vision call and a
        second or two, and the ordinary step needs none of them.
        """
        if decision.action not in _GROUNDED_ACTIONS:
            # Grounding finds the element to act on. A navigation, a scroll or
            # a `done` has none, and photographing the page for one is a vision
            # call spent on nothing.
            return GroundingOutcome(decision=decision)
        reason = ground_reason(
            decision.needs_vision,
            decision.confidence,
            state,
            threshold=self._settings.VISION_CONFIDENCE_THRESHOLD,
            min_elements=self._settings.VISION_MIN_ELEMENTS,
            target_index=decision.target_index,
        )
        if reason is None:
            return GroundingOutcome(decision=decision)

        shot = await self._browser.screenshot()
        outcome = await self._grounder.resolve(decision, state, shot.jpeg_bytes, reason)

        tally.vision_calls += 1
        tally.vision_latency_ms += outcome.latency_ms
        tally.vision_tokens += outcome.tokens
        metrics.vision_invocations_total.labels(
            reason=reason.name.lower(),
            outcome="declined" if outcome.error is not None else "resolved",
        ).inc()
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

    async def _clear_overlays(self, state: PageState) -> PageState:
        """Close a popup that is covering the page before planning against it.

        A sign-up promo or cookie banner marks everything under it covered, so
        the planner would be choosing among controls nobody can reach. Only
        dismissals are clicked (never "Accept"), a couple of tries at most, and
        a click that does not clear the popup stops the attempts rather than
        looping.
        """
        for attempt in range(MAX_OVERLAY_CLEARS):
            closers = safe_closers(state)
            selector = selector_for(closers[0]) if closers else None
            if selector is None:
                if attempt == 0 and is_blocked(state):
                    # Something covers the page and nothing on it says "close".
                    # Escape closes a well-built dialog and does nothing to a
                    # page without one, so it is safe to try once.
                    await self._browser.press("Escape")
                    await self._browser.settle()
                    after_escape = await self._browser.perceive()
                    logger.info("overlay_escape", cleared=not is_blocked(after_escape))
                    return after_escape
                return state
            result = await self._browser.execute(ClickAction(selector=selector))
            logger.info("overlay_dismissed", ok=result.ok, label=closers[0].name[:40])
            await self._browser.settle()
            after = await self._browser.perceive()
            cleared = len(safe_closers(after)) < len(closers) or not safe_closers(after)
            state = after
            if not result.ok or not cleared:
                return state
        return state

    async def _return_to_planning(self, task_id: uuid.UUID) -> None:
        """Walk the task back to PLANNING along the edges the state machine allows.

        ACTING and RECOVERING both lead to VERIFYING, which leads to PLANNING.
        A task already in PLANNING (a flow whose first step could not be
        resolved never left it) needs nothing: the machine refuses a move to
        the state a task is already in.
        """
        onward = {
            TaskStatus.ACTING: TaskStatus.VERIFYING,
            TaskStatus.RECOVERING: TaskStatus.VERIFYING,
            TaskStatus.VERIFYING: TaskStatus.PLANNING,
        }
        current = await self._current_status(task_id)
        while current is not TaskStatus.PLANNING and current in onward:
            current = onward[current]
            await self._set_status(task_id, current)

    async def _wait_for_login(self, task_id: uuid.UUID, step_number: int, state: PageState) -> None:
        """Hand the browser to the person to sign in or sign up, and wait."""
        prompt = (
            f"{_site_of(state) or 'This site'} needs you to sign in or sign up. Do it "
            "in the browser window, then continue here and the agent will carry on."
        )
        if not self._settings.AGENT_CDP_URL:
            # Google and others refuse sign-in to a browser under automation,
            # whoever is typing. Saying why, and what works, beats a person
            # retrying a password that was never the problem.
            prompt += (
                " If the site says this browser is not secure, it is refusing the agent's "
                "own browser: run the agent in your Chrome instead (scripts/chrome_debug.ps1 "
                "and AGENT_CDP_URL) and sign in there once."
            )
        await self._hand_over(
            task_id,
            step_number,
            state,
            prompt=prompt,
            target=HANDOFF_SIGN_IN,
            reasoning="A sign-in page or form is blocking the task.",
            wait_seconds=self._settings.AGENT_LOGIN_WAIT_SECONDS,
        )

    async def _wait_for_payment(
        self, task_id: uuid.UUID, step_number: int, state: PageState
    ) -> None:
        """Hand the browser to the person to pay, and wait.

        The task stays open while they do: when it ends the driver closes every
        tab it opened, and a booking left on the payment page would go with it.
        Rejecting is the person deciding not to pay, and stops the task.
        """
        await self._hand_over(
            task_id,
            step_number,
            state,
            prompt=(
                f"Your booking on {_site_of(state) or 'this site'} is ready for payment, "
                "and the agent never pays. Choose how to pay and finish the payment in "
                "the browser window, then continue here and the agent will read the "
                "confirmation. Stop here if you do not want to pay."
            ),
            target=HANDOFF_PAY,
            reasoning="The next step is a payment, which only the person makes.",
            wait_seconds=self._settings.AGENT_PAYMENT_WAIT_SECONDS,
        )

    async def _wait_for_details(
        self, task_id: uuid.UUID, step_number: int, state: PageState, wanted: str | None
    ) -> None:
        """Hand the browser to the person to fill in what the goal left out."""
        # Clipped: the planner wrote it, from a page somebody else wrote, and
        # it is shown to a person as the agent's own request.
        what = (wanted or "").strip()[:80] or "some details"
        await self._hand_over(
            task_id,
            step_number,
            state,
            prompt=(
                f"The form on {_site_of(state) or 'this site'} needs details the agent "
                f"was not given ({what}). Enter them in the browser window, then "
                "continue here and the agent will carry on."
            ),
            target=HANDOFF_DETAILS,
            reasoning="The form needs details the goal does not give.",
            wait_seconds=self._settings.AGENT_LOGIN_WAIT_SECONDS,
        )

    async def _hand_over(
        self,
        task_id: uuid.UUID,
        step_number: int,
        state: PageState,
        *,
        prompt: str,
        target: str,
        reasoning: str,
        wait_seconds: int,
    ) -> None:
        """Give the browser to the person for something only they can do, and wait.

        Rides the approval gate: the person is shown a prompt, does it in the
        visible browser window, and presses Approve to let the agent continue.
        Rejecting stops the task, and an unanswered prompt times it out, exactly
        as for any other approval. The wait is not charged to the task clock.
        """
        # A step that failed on the sign-in prompt leaves the task in RECOVERING
        # (or VERIFYING), and neither has an edge to AWAITING_APPROVAL. Only
        # PLANNING does.
        await self._return_to_planning(task_id)
        decision = Decision(
            action=ActionType.WAIT,
            target=target,
            reasoning=reasoning,
            expected_outcome=ExpectedOutcome(type=VerificationMethod.DOM_CHANGED),
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            confidence=1.0,
        )
        await self._indicate("waiting")
        try:
            await self._gate(
                task_id,
                step_number,
                decision,
                RiskLevel.HIGH,
                None,
                [prompt],
                state,
                wait_seconds=wait_seconds,
            )
        finally:
            await self._indicate("working")
        # Approve resumes into ACTING, and the machine only allows ACTING ->
        # VERIFYING -> PLANNING from there. Walk it, so the task does not sit
        # in AWAITING_APPROVAL while the planner thinks.
        await self._set_status(task_id, TaskStatus.ACTING)
        await self._set_status(task_id, TaskStatus.VERIFYING)
        await self._set_status(task_id, TaskStatus.PLANNING)

    async def _indicate(self, mode: str) -> None:
        """Tell the visible browser what the agent is doing, if it can show it."""
        hook = getattr(self._browser, "set_indicator", None)
        if hook is not None:
            await hook(mode)

    async def _gate(
        self,
        task_id: uuid.UUID,
        step_number: int,
        decision: Decision,
        risk: RiskLevel,
        element: ElementInfo | None,
        reasons: list[str],
        state: PageState,
        wait_seconds: int | None = None,
    ) -> None:
        """Park on a human.

        Three outcomes, and they are not the same. Approved carries on.
        Rejected is somebody saying no, which is REJECTED. Nobody answering is
        not a rejection: spec section 4.3 and decision A4 send that to
        TIMED_OUT and keep REJECTED for a human who actually looked.
        """
        await self._set_status(task_id, TaskStatus.AWAITING_APPROVAL)
        shot = await self._browser.screenshot()
        waited_from = time.monotonic()
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
            timeout_seconds=wait_seconds,
        )
        # The person's time is not the agent's: take it out of the task clock.
        if self._running_budget is not None:
            self._running_budget.exclude(time.monotonic() - waited_from)
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
        *,
        rescue: bool = True,
    ) -> tuple[Verdict, PageState, list[Attempt], list[str]]:
        """Check the step, and try to rescue it if the check failed.

        Returns the page as it ended up, which is not always the page that
        failed: a strategy that works has navigated, dismissed something or
        otherwise moved on, and the next step has to plan against that.

        ``rescue=False`` skips the ladder, for a step whose action is known to
        have landed and so has no locator to fix.
        """
        if not result.ok:
            why = _why_not_run(result)
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
                    "message": result.error_message,
                },
                detail=why,
            )
        else:
            verdict = await self._verifier.check(decision, before, after, result)

        if verdict.ok or not rescue:
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
        planned: PlannerResult | None,
        grounded: GroundingOutcome,
        element: ElementInfo | None,
        state: PageState,
        verdict: Verdict,
        attempts: list[Attempt],
        risk: RiskLevel,
        shot: Screenshot,
        after: PageState,
        replayed: bool = False,
        plan_step: int | None = None,
    ) -> None:
        """Write the step, then tell anyone watching.

        The image goes to storage first so the row can carry a reference that
        already resolves. A rolled-back transaction then leaves an orphaned
        file, which `delete_task` sweeps and nothing else notices; the reverse
        order would leave a committed row pointing at nothing.

        ``planned`` is ``None`` for a replayed step, which has no model call
        behind it and so no ``llm_decisions`` row. That absence is the
        evidence: counting steps without one is how the saving from a cache
        hit is measured.
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
                    plan_step=plan_step,
                    status=StepStatus.VERIFIED if verdict.ok else StepStatus.FAILED,
                )
            )
            if planned is not None:
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
            metrics.verification_total.labels(
                result="passed" if verdict.ok else "failed",
                method=verdict.method.value,
            ).inc()
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
                replayed=replayed,
                plan_step=plan_step,
            ),
        )

    async def _clarify(
        self, task_id: uuid.UUID, instruction: str, pages: _PageSurvey | None = None
    ) -> str:
        """Ask the person what the task leaves out, then fold the answers in.

        Rides the approval channel: the question is a WAIT decision whose
        options travel in ``value``, and the answer comes back as the reason on
        the approve call. Rejecting a question means "no preference", not "stop
        the task", so it is skipped rather than failing anything. Never fatal.
        """
        questions = await self._planner.questions(
            instruction, self._settings.AGENT_CLARIFY_MAX_QUESTIONS
        )
        answers: list[tuple[str, str]] = []
        for question in questions:
            answer = await self._ask(task_id, question.text, question.options)
            if answer:
                answers.append((question.text, answer))
            elif pages is not None:
                pages.skipped.append(question.text)
        return with_answers(instruction, answers)

    async def _ask_about_page(
        self,
        task_id: uuid.UUID,
        instruction: str,
        state: PageState,
        pages: _PageSurvey,
        tally: _Tally,
    ) -> tuple[bool, str]:
        """Ask about what this page's form needs and the task and answers do not say.

        Once per page (host and path), only on a page with something to fill
        in, and on at most MAX_PAGE_SURVEYS pages a task. Returns whether this
        page is where the task's form is, and the task with any answers added.
        """
        if not self._settings.AGENT_CLARIFY:
            return False, instruction
        key = _page_key(state)
        if key in pages.seen or len(pages.seen) >= MAX_PAGE_SURVEYS or not _has_fields(state):
            return False, instruction
        pages.seen.add(key)

        found = await self._planner.page_questions(
            instruction, state, self._settings.AGENT_CLARIFY_MAX_QUESTIONS, pages.skipped
        )
        if not found.questions:
            return found.for_task, instruction

        # The step before left the task in VERIFYING or later; a question rides
        # the approval gate, which is only reachable from PLANNING.
        await self._return_to_planning(task_id)
        answers: list[tuple[str, str]] = []
        for question in found.questions:
            answer = await self._ask(task_id, question.text, question.options, site=_site_of(state))
            if answer:
                answers.append((question.text, answer))
            else:
                pages.skipped.append(question.text)
        if answers:
            tally.asked_on_page = True
        logger.info("page_questions", asked=len(found.questions), answered=len(answers))
        return found.for_task, with_answers(instruction, answers)

    async def _ask(
        self,
        task_id: uuid.UUID,
        question: str,
        options: tuple[str, ...],
        *,
        site: str = "",
    ) -> str | None:
        """One question to the person. The answer, or None if they skipped it."""
        decision = Decision(
            action=ActionType.WAIT,
            target="question",
            value="|".join(options),
            reasoning="The task leaves this out and it changes the result.",
            expected_outcome=ExpectedOutcome(type=VerificationMethod.DOM_CHANGED),
            risk_level=RiskLevel.LOW,
            requires_approval=True,
            confidence=1.0,
        )
        await self._set_status(task_id, TaskStatus.AWAITING_APPROVAL)
        await self._indicate("waiting")
        waited_from = time.monotonic()
        try:
            answer = await self._approvals.request(
                task_id=task_id,
                step_number=0,
                decision=decision,
                risk=RiskLevel.LOW,
                screenshot=None,
                bbox=None,
                reasons=[question],
                site=site,
                timeout_seconds=self._settings.AGENT_CLARIFY_WAIT_SECONDS,
            )
        finally:
            await self._indicate("working")
        if self._running_budget is not None:
            self._running_budget.exclude(time.monotonic() - waited_from)
        if answer.expired:
            raise _Stop(TaskStatus.TIMED_OUT, failure_reason="approval timed out")
        # Walk back to planning the way a sign-in hand-off does: the machine only
        # leaves AWAITING_APPROVAL for ACTING or REJECTED.
        await self._set_status(task_id, TaskStatus.ACTING)
        await self._set_status(task_id, TaskStatus.VERIFYING)
        await self._set_status(task_id, TaskStatus.PLANNING)
        text = (answer.reason or "").strip() if answer.approved else ""
        return text[:200] or None

    async def _publish_outline(
        self, task_id: uuid.UUID, instruction: str, state: PageState | None = None
    ) -> list[str]:
        """Say how many steps to expect, planned from the page the agent has open.

        Stored on the task (so a reconnecting client still gets it) and then
        published, in that order. Never fatal: a task with no outline is still a
        task, and an outline is a forecast the agent is free to depart from.
        """
        steps = await self._planner.outline(instruction, state)
        if not steps:
            return []
        await self._save_plan(task_id, steps)
        return steps

    async def _follow_plan(
        self,
        task_id: uuid.UUID,
        outline: list[str],
        decision: Decision,
        plan_at: int | None,
    ) -> tuple[list[str], int | None]:
        """Which plan item this step works on, adding any the planner found missing.

        New items go in just before the item the planner is on: they are work
        that has to happen before it can carry on (a popup to close, a class to
        choose), and this step is the first of it. The plan is saved and sent
        again whenever it grows, so the list the person sees keeps up with what
        the agent is really doing. A number off either end is clamped rather
        than trusted, and there is a ceiling on how long the plan may grow.
        """
        if not outline:
            return outline, None
        current = decision.plan_step or plan_at or 1
        room = max(0, MAX_PLAN_STEPS - len(outline))
        added = [" ".join(item.split())[:MAX_STEP_CHARS] for item in decision.add_plan_steps]
        added = [item for item in added if item and item not in outline][
            : min(room, MAX_PLAN_ADDS_PER_STEP)
        ]
        if not added:
            return outline, min(max(current, 1), len(outline))

        at = min(max(current, 1), len(outline) + 1)
        grown = outline[: at - 1] + added + outline[at - 1 :]
        await self._save_plan(task_id, grown)
        logger.info("plan_grew", added=len(added), steps=len(grown))
        return grown, at

    async def _save_plan(self, task_id: uuid.UUID, steps: list[str]) -> None:
        """Store the plan on the task, then send it: a reconnecting client gets it too."""
        async with self._unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            task.plan = steps
        await self._events.publish(task_id, PlanEvent(task_id=task_id, steps=steps))

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
        usage: Usage | None = None,
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

                if usage is not None:
                    task.credits_used = credits_for_usage(
                        usage, CreditPrices.from_settings(self._settings)
                    )

                # Same transaction as the terminal status, which is the point of
                # the outbox: a task cannot be finished without its charge being
                # queued, and the charge cannot be queued for a task that did
                # not finish (spec section 7).
                await self._queue_settlement(uow, task, usage)
        except Exception:
            # The final event still has to go out, so this cannot be fatal.
            logger.exception("task_settle_failed", target=status.value)
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
                    replayed_steps=outcome.replayed_steps,
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


# What a hand-off is for, as the `target` of its `wait` decision. Clients tell
# the kinds apart by these, so a payment is not shown as a sign-in: change one
# and the frontend's hand-off card has to change with it.
HANDOFF_SIGN_IN: Final = "for you to sign in or sign up"
HANDOFF_PAY: Final = "for you to pay"
HANDOFF_DETAILS: Final = "for you to fill in details"

MAX_LOGIN_PAUSES: Final = 3
# Payment hand-offs before the task is failed for it. A page that still asks to
# be paid after the person was given it twice is not one they are going to pay.
MAX_PAYMENT_PAUSES: Final = 2
# Times the planner may ask the person for details the goal did not give.
MAX_DETAIL_PAUSES: Final = 2
# Pages read for questions in one task. Each costs a model call, and a task that
# has wandered through this many forms is not short of questions.
MAX_PAGE_SURVEYS: Final = 5
# Steps after which the outline is made even if no page has been the task's
# form, so a lookup that never meets one still gets a plan.
PLAN_BY_STEP: Final = 3
# How long the plan may grow mid-run, and by how much in one step. A planner
# adding work on every action is narrating, not planning.
MAX_PLAN_STEPS: Final = 20
MAX_PLAN_ADDS_PER_STEP: Final = 2
# Controls a person fills in or chooses between, as opposed to ones they press.
_FILL_ROLES: Final = frozenset(
    {"combobox", "textbox", "searchbox", "radio", "listbox", "spinbutton"}
)
# Scrolls in a row that may find the page already at its end before the task is
# failed for it. A planner that keeps scrolling a page with nothing left is stuck.
MAX_IDLE_SCROLLS: Final = 4
# Waits in a row that found the page already settled, for the same reason.
MAX_IDLE_WAITS: Final = 3
# Extracts that came back empty, for the same reason.
MAX_IDLE_EXTRACTS: Final = 4
# Fresh looks at the page after the planner could not decide, before the task
# is failed for it. Reset by every step that works.
MAX_PLANNER_RETRIES: Final = 1
# Sites the agent chose that would not open, before the task is failed for it.
MAX_REFUSED_SITES: Final = 3
# Steps in a row that landed but did not bring what the planner expected,
# before the task is failed for it. More than this and the planner is not
# misjudging one page, it is lost.
MAX_MISSED_FORECASTS: Final = 3
# Actions on an element, whose landing the page itself shows. A navigation has
# its own path above; a scroll, an extract or `done` has nothing to forecast.
_FORECAST_ACTIONS: Final = frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.SELECT})
# Actions with an element on the page to find, which is all grounding does.
_GROUNDED_ACTIONS: Final = frozenset({ActionType.CLICK, ActionType.TYPE, ActionType.SELECT})


def _history_note(
    verdict: Verdict, *, missed: bool, action: ActionType | None = None
) -> str | None:
    """Why a step failed, as the planner reads it back. Nothing for a step that worked."""
    if verdict.ok:
        return None
    if missed:
        # Said outright, or the planner reads "failed" and repeats the action
        # that in fact worked.
        return f"it ran and the page changed, but {(verdict.detail or 'not as expected')[:80]}"
    if action is ActionType.WAIT:
        # Or the planner waits again, for something a wait will never bring.
        return "nothing changed: the page has finished loading, so act on what it lists"
    if action is ActionType.SCROLL:
        # Or it scrolls again, hunting for a control that is not further down.
        return (
            "the page did not move: there is nothing more that way. What you need is "
            "on the page as listed, or under an open popup"
        )
    return (verdict.detail or "")[:100] or None


def _address_in_goal(url: str | None, instruction: str) -> bool:
    """Did the person name this site? Then there is nothing else to try."""
    host = (urlsplit(url or "").hostname or "").removeprefix("www.")
    return bool(host) and host in instruction.lower()


def _history_target(decision: Decision, result: ActionResult, verdict: Verdict) -> str | None:
    """The step's target as the planner reads it back, with why a site was refused."""
    if decision.action is ActionType.NAVIGATE and not verdict.ok:
        why = (result.error_message or "it would not open")[:80]
        return f"{decision.value} ({why}; choose a different site)"
    return decision.target


MAX_OVERLAY_CLEARS: Final = 2

_LOGIN_PATH: Final = re.compile(r"/(log-?in|sign-?in|sign-?up|register|accounts?/login)(/|$)", re.I)
# The page says outright that it wants a sign-in before it will go on. Phrases,
# not the bare word "login": nearly every site has a Login button in its header.
_LOGIN_WORDING: Final = re.compile(
    r"(log ?in|sign ?in) (is )?(required|to continue|to proceed|to book|to view|first)"
    r"|(please|you (must|need to|have to)) (log ?in|sign ?in)"
    r"|(session|login) (has )?(expired|timed out)"
    r"|(log ?in|sign ?in) to (your account|continue)",
    re.IGNORECASE,
)
# A goal that mentions credentials is a goal about signing in; do not second-guess it.
_GOAL_IS_LOGIN: Final = re.compile(r"password|credential|log ?in with|sign ?in with", re.I)


def _login_wall(state: PageState, instruction: str) -> bool:
    """Is the page asking for a sign-in or sign-up the agent must not do?

    Deterministic and cheap: a usable password field, or a login-looking path
    with an email field. Nothing here asks the model, because "should I invent
    an account" is not a question to put to one.
    """
    if _GOAL_IS_LOGIN.search(instruction):
        return False
    usable = [e for e in state.elements if not e.disabled and not e.covered]
    if any(e.input_type == "password" for e in usable):
        return True
    if _LOGIN_WORDING.search(state.text) and any(e.tag in {"input", "button", "a"} for e in usable):
        return True
    if _LOGIN_PATH.search(urlsplit(state.url).path):
        return any(e.input_type in {"email", "tel"} or "email" in e.name.lower() for e in usable)
    return False


def _on_a_site(state: PageState) -> bool:
    """Is a real page open, rather than the blank tab a task starts on?"""
    return urlsplit(state.url).scheme in {"http", "https"}


def _page_key(state: PageState) -> str:
    """A page, for asking about it once: host and path, without the query."""
    parts = urlsplit(state.url)
    return f"{parts.hostname or ''}{parts.path.rstrip('/')}"


def _has_fields(state: PageState) -> bool:
    """Does the page have anything to fill in or choose? Nothing to ask otherwise."""
    return any(
        not element.disabled
        and (element.tag in {"input", "select", "textarea"} or (element.role or "") in _FILL_ROLES)
        for element in state.elements
    )


# Second-level labels a country code sits under: irctc.co.in, bbc.co.uk.
_SECOND_LEVEL: Final = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})
# How titles join a site's name to the rest: a bar, or a hyphen or en or em
# dash with spaces round it ("IRCTC, en dash, Official ...").
_TITLE_PARTS: Final = re.compile(r"\s+[|\u2013\u2014-]\s+|\s*\|\s*")


def _site_of(state: PageState) -> str:
    """The site's own short name, as a person would say it: "IRCTC".

    Not the page title: "IRCTC - Official Indian Railway Catering & Tourism
    Corporation Portal" quoted in every prompt is noise. A title segment that
    is the site's name keeps its capitals ("MakeMyTrip"); otherwise the bare
    domain ("uber.com").
    """
    host = (urlsplit(state.url).hostname or "").removeprefix("www.")
    labels = host.split(".")
    owned = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    domain = ".".join(labels[-owned:]) if len(labels) >= owned else host
    name = _letters(labels[-owned]) if len(labels) >= owned else _letters(host)
    for part in _TITLE_PARTS.split(state.title):
        if name and _letters(part) == name:
            return part.strip()
    return domain


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _hint_text(hint: CachedHint | None) -> str | None:
    return hint.text if hint else None


def _cacheable(decision: Decision, element: ElementInfo | None) -> ReplayStep:
    """One verified step, in the portable form a later run can replay.

    A masked value is kept as a marker rather than a value. The real one never
    reaches this layer, so there is nothing to store even if storing it were
    allowed, and a step that silently typed an empty password would be worse
    than one that says the planner has to do it.
    """
    sensitive = _is_sensitive(element)
    return ReplayStep(
        action=decision.action,
        locator=Locator.of(element) if element is not None else None,
        value=None if sensitive else decision.value,
        target=decision.target,
        expected_type=decision.expected_outcome.type,
        expected_value=decision.expected_outcome.value,
        needs_planner=sensitive,
    )


def _publish_metrics(outcome: RunOutcome) -> None:
    """One terminal run, in Prometheus.

    Labelled by status only. Everything else about a run is in the rows, and a
    metric labelled by anything task-specific would grow a series per task.
    """
    status = outcome.status.value
    metrics.tasks_total.labels(status=status).inc()
    metrics.task_duration_seconds.labels(status=status).observe(outcome.duration_ms / 1000)
    metrics.steps_per_task.labels(status=status).observe(outcome.steps)


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
