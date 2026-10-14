"""The agent loop, driven entirely by fakes.

No browser, no database, no network. The point of every collaborator being an
interface is that the loop can be exercised on its own, which is also the only
way to test the paths that matter most: running out of budget, an unexpected
exception, and a slow client still being told the task finished.
"""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.agent.browser.driver import ActionResult, BrowserSnapshot
from app.agent.browser.errors import ActionErrorCode
from app.agent.browser.screenshots import Screenshot
from app.agent.budget import TaskBudget
from app.agent.cache.flow import Locator, ReplayStep
from app.agent.contracts import (
    ApprovalOutcome,
    Attempt,
    GroundingOutcome,
    RecoveryContext,
    ReplayPlan,
    Verdict,
)
from app.agent.llm.mock import MockLLM
from app.agent.perception.dom_extractor import MASK, BoundingBox, ElementInfo
from app.agent.perception.state import PageState
from app.agent.planner.planner import Planner
from app.agent.planner.schema import Decision
from app.core.config import Settings
from app.domain.enums import (
    ActionType,
    RecoveryOutcome,
    RiskLevel,
    StepStatus,
    TaskStatus,
    VerificationMethod,
)
from app.services.events import EventType, InMemoryEventBus

from app.agent.orchestrator import Orchestrator, RunOutcome  # isort: skip

TASK_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
GOAL = "Search for flights from Delhi"


# ------------------------------------------------------------------ fakes


def element(
    index: int, *, name: str = "From", input_type: str = "text", value: str = ""
) -> ElementInfo:
    return ElementInfo(
        index=index,
        tag="input",
        name=name,
        input_type=input_type,
        value=value,
        bbox=BoundingBox(x=10 * index, y=20, width=100, height=30),
        in_viewport=True,
    )


def page(*elements: ElementInfo, url: str = "https://example.test/form") -> PageState:
    return PageState(
        url=url,
        title="Form",
        tabs=["tab-1"],
        active_tab_id="tab-1",
        elements=list(elements),
        summary_text="[0] input From",
        dom_hash="abc123",
    )


def snapshot(url: str = "https://example.test/form") -> BrowserSnapshot:
    return BrowserSnapshot(
        url=url,
        title="Form",
        tab_ids=["tab-1"],
        active_tab_id="tab-1",
        scroll_y=0,
        viewport_width=1280,
        viewport_height=720,
    )


def shot() -> Screenshot:
    return Screenshot(
        jpeg_bytes=b"\xff\xd8\xff-not-really-a-jpeg",
        width=1280,
        height=720,
        viewport_width=1280,
        viewport_height=720,
    )


class FakeBrowser:
    """A browser that always agrees."""

    def __init__(self, state: PageState | None = None, *, ok: bool = True) -> None:
        self._state = state or page(element(0))
        self._ok = ok
        self.executed: list[Any] = []
        self.perceive_calls = 0
        self.settle_calls = 0
        self.scrolled: list[str] = []
        self.pressed: list[str] = []

    async def perceive(self) -> PageState:
        self.perceive_calls += 1
        return self._state

    async def settle(self) -> None:
        self.settle_calls += 1

    async def scroll_into_view(self, selector: str) -> bool:
        self.scrolled.append(selector)
        return False

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def screenshot(self) -> Screenshot:
        return shot()

    async def capture_state(self) -> BrowserSnapshot:
        return snapshot()

    async def execute(self, action: Any) -> ActionResult:
        self.executed.append(action)
        return ActionResult(
            ok=self._ok,
            duration_ms=12,
            url_before="https://example.test/form",
            url_after="https://example.test/form",
            error_code=None if self._ok else ActionErrorCode.ELEMENT_NOT_FOUND,
        )


class ApprovingGate:
    """Says yes. For tests about something other than the approval itself."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> ApprovalOutcome:
        self.requests.append(kwargs)
        return ApprovalOutcome(approved=True, approval_id=uuid.uuid4())


class PassingVerifier:
    """Says yes. These tests are about the loop, not about verification.

    The real verifier compares two page states, and a fake browser that hands
    back the same state twice would fail every check for reasons that have
    nothing to do with what is being tested here. Verification itself has its
    own suite.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def check(self, decision: Decision, *args: Any) -> Verdict:
        self.calls += 1
        return Verdict(
            ok=True,
            method=decision.expected_outcome.type,
            expected={"type": decision.expected_outcome.type.value, "value": "x"},
            actual={"check": "test"},
            detail="Passed by the test double.",
        )


class FakeStorage:
    def __init__(self) -> None:
        self.saved: dict[int, bytes] = {}

    async def save(self, task_id: uuid.UUID, step_number: int, data: bytes) -> str:
        self.saved[step_number] = data
        return f"{task_id}/{step_number:04d}.jpg"

    async def load(self, ref: str) -> bytes:  # pragma: no cover - unused here
        raise NotImplementedError

    async def exists(self, ref: str) -> bool:  # pragma: no cover - unused here
        return True

    async def delete_task(self, task_id: uuid.UUID) -> int:  # pragma: no cover - unused here
        return 0


@dataclass
class FakeTask:
    id: uuid.UUID = TASK_ID
    instruction: str = GOAL
    status: TaskStatus = TaskStatus.SUBMITTED
    final_answer: dict[str, Any] | None = None
    failure_reason: str | None = None
    plan: list[str] | None = None
    finished_at: Any = None


@dataclass
class FakeStore:
    """Stands in for the database, and records when writes became visible."""

    task: FakeTask = field(default_factory=FakeTask)
    steps: list[Any] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    verifications: list[Any] = field(default_factory=list)
    recovery_attempts: list[Any] = field(default_factory=list)
    # Staged rows only become visible on commit, exactly like a transaction.
    pending: list[tuple[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    fail_on_commit: bool = False

    def stage(self, kind: str, row: Any) -> Any:
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.pending.append((kind, row))
        return row

    def commit(self) -> None:
        if self.fail_on_commit:
            self.pending.clear()
            raise RuntimeError("the database went away")
        for kind, row in self.pending:
            getattr(self, kind).append(row)
        if self.pending:
            self.log.append("commit")
        self.pending.clear()

    def rollback(self) -> None:
        self.pending.clear()

    def step_numbers(self) -> list[int]:
        return [row.step_number for row in self.steps]


class FakeRepo:
    def __init__(self, store: FakeStore, kind: str) -> None:
        self._store = store
        self._kind = kind

    async def add(self, entity: Any) -> Any:
        return self._store.stage(self._kind, entity)


class FakeTasks:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_or_404(self, task_id: uuid.UUID) -> FakeTask:
        return self._store.task


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.tasks = FakeTasks(store)
        self.task_steps = FakeRepo(store, "steps")
        self.llm_decisions = FakeRepo(store, "decisions")
        self.verifications = FakeRepo(store, "verifications")
        self.recovery_attempts = FakeRepo(store, "recovery_attempts")


def unit_of_work_for(store: FakeStore) -> Any:
    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        try:
            yield FakeUnitOfWork(store)
        except Exception:
            store.rollback()
            raise
        else:
            store.commit()

    return factory


class WatchingBus(InMemoryEventBus):
    """An event bus that checks the database is already ahead of it.

    Publishing a step the database has not committed would mean a reconnecting
    client could be told about something it can never fetch again, which is the
    one thing spec section 6.1 promises cannot happen.
    """

    def __init__(self, store: FakeStore) -> None:
        super().__init__()
        self._store = store
        self.published: list[Any] = []

    async def publish(self, task_id: uuid.UUID, event: Any) -> Any:
        if event.type is EventType.STEP:
            assert event.step_number in self._store.step_numbers(), (
                f"step {event.step_number} was published before it was committed"
            )
            self._store.log.append(f"publish:step:{event.step_number}")
        stamped = await super().publish(task_id, event)
        self.published.append(stamped)
        return stamped

    def types(self) -> list[str]:
        return [event.type.value for event in self.published]


def decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "type",
        "targetIndex": 0,
        "target": "From",
        "value": "Delhi",
        "reasoning": "Fill the origin field.",
        "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
        "riskLevel": "LOW",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def done_payload(answer: str = "Found it") -> dict[str, Any]:
    return {
        "action": "done",
        "reasoning": "The goal is met.",
        "finalAnswer": answer,
        "expectedOutcome": {"type": "dom_changed", "value": ""},
        "confidence": 0.95,
    }


@dataclass
class Rig:
    """Everything a test needs to drive and inspect one run."""

    orchestrator: Orchestrator
    store: FakeStore
    bus: "WatchingBus"
    browser: FakeBrowser
    storage: FakeStorage


def build(
    *,
    script: list[dict[str, Any]],
    settings: Settings,
    store: FakeStore | None = None,
    browser: FakeBrowser | None = None,
    **kwargs: Any,
) -> Rig:
    store = store or FakeStore()
    store.task.status = TaskStatus.SUBMITTED
    browser = browser or FakeBrowser()
    bus = WatchingBus(store)
    storage = FakeStorage()

    kwargs.setdefault("verifier", PassingVerifier())
    orchestrator = Orchestrator(
        browser=browser,
        planner=Planner(MockLLM(script=script)),
        storage=storage,  # type: ignore[arg-type]
        events=bus,
        unit_of_work=unit_of_work_for(store),
        settings=settings,
        **kwargs,
    )
    return Rig(orchestrator=orchestrator, store=store, bus=bus, browser=browser, storage=storage)


# ------------------------------------------------------------- happy path


async def test_a_two_step_task_reaches_completed(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload("Delhi to Bengaluru, Rs 4299")],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.final_answer == "Delhi to Bengaluru, Rs 4299"
    assert outcome.failure_reason is None
    assert outcome.steps == 2
    assert rig.store.task.status is TaskStatus.COMPLETED
    assert rig.store.task.final_answer == {"text": "Delhi to Bengaluru, Rs 4299"}
    assert rig.store.task.finished_at is not None


async def test_every_step_writes_three_rows(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    assert rig.store.step_numbers() == [1, 2]
    assert len(rig.store.decisions) == 2
    assert len(rig.store.verifications) == 2
    # One screenshot per step, and the row points at the one just written.
    assert sorted(rig.storage.saved) == [1, 2]
    assert rig.store.steps[0].screenshot_ref == f"{TASK_ID}/0001.jpg"


async def test_the_status_events_follow_the_lifecycle(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    statuses = [event.status.value for event in rig.bus.published if event.type is EventType.STATUS]
    assert statuses == [
        "PLANNING",
        "ACTING",
        "VERIFYING",
        "PLANNING",
        "ACTING",
        "VERIFYING",
        "COMPLETED",
    ]


async def test_events_arrive_in_the_right_order(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    assert rig.bus.types() == [
        "status",  # PLANNING
        "status",  # ACTING
        "status",  # VERIFYING
        "step",  # step 1
        "status",  # PLANNING again
        "status",  # ACTING
        "status",  # VERIFYING
        "step",  # step 2, the done action
        "status",  # COMPLETED
        "final",
    ]
    assert [event.seq for event in rig.bus.published] == list(range(1, 11))


async def test_the_step_is_committed_before_it_is_published(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    # WatchingBus already asserts this per event; this pins the whole sequence
    # so a reordering cannot pass by publishing nothing at all.
    assert rig.store.log == ["commit", "publish:step:1", "commit", "publish:step:2"]


async def test_the_step_event_carries_what_the_live_view_needs(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)
    step = next(event for event in rig.bus.published if event.type is EventType.STEP)

    assert step.step_number == 1
    assert step.action == "type"
    assert step.target == "From"
    assert step.value == "Delhi"
    assert step.verified is True
    assert step.screenshot is not None
    assert step.bbox is not None and step.bbox.width == 100
    assert step.viewport is not None and step.viewport.width == 1280
    assert step.url == "https://example.test/form"
    assert step.tab_id == "tab-1"
    assert step.reasoning == "Fill the origin field."
    assert step.confidence == pytest.approx(0.9)
    assert step.used_vision is False


async def test_the_action_targets_the_collector_stamp(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    # Not a CSS path: the stamp survives a re-render that renames classes.
    assert rig.browser.executed[0].selector == '[data-agent-idx="0"]'
    assert rig.store.steps[0].selector_used == '[data-agent-idx="0"]'
    assert rig.store.steps[0].bbox == {"x": 0, "y": 20, "width": 100, "height": 30}


async def test_the_final_event_reports_the_run(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload("all done")],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)
    final = rig.bus.published[-1]

    assert final.type is EventType.FINAL
    assert final.status is TaskStatus.COMPLETED
    assert final.final_answer == "all done"
    assert final.stats.steps == 2
    assert final.stats.credits_used == 2
    assert final.stats.cache_hit is False


# ------------------------------------------------------------- redaction


async def test_a_typed_password_never_reaches_the_database_or_the_wire(
    settings: Settings,
) -> None:
    secret = "hunter2-do-not-rig.store"
    state = page(element(0, name="Password", input_type="password"))
    rig = build(
        script=[decision_payload(value=secret, target="Password"), done_payload()],
        # A goal about signing in is left to the agent; otherwise it would pause.
        store=FakeStore(task=FakeTask(instruction="Log in with my password")),
        browser=FakeBrowser(state),
        settings=settings,
        # Typing into a password field is high risk now, so without a gate
        # that answers, this stops before it ever writes a row.
        approvals=ApprovingGate(),
    )

    await rig.orchestrator.run(TASK_ID)

    step_row = rig.store.steps[0]
    step_event = next(event for event in rig.bus.published if event.type is EventType.STEP)

    assert step_row.value_redacted == MASK
    assert step_event.value == MASK
    # The model's own output is stored for debugging and carries the value it
    # chose, so it needs the same treatment as the column next to it.
    assert rig.store.decisions[0].raw_output["value"] == MASK
    assert secret not in repr(rig.store.steps) + repr(rig.store.decisions) + repr(rig.bus.published)


async def test_an_ordinary_value_is_kept(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(value="Delhi"), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    # Masking everything would make the activity log useless; only secrets go.
    assert rig.store.steps[0].value_redacted == "Delhi"


async def test_a_masked_field_value_is_treated_as_sensitive(settings: Settings) -> None:
    state = page(element(0, name="Card", input_type="text", value=MASK))
    rig = build(
        script=[decision_payload(value="4111111111111111"), done_payload()],
        browser=FakeBrowser(state),
        settings=settings,
        approvals=ApprovingGate(),
    )

    await rig.orchestrator.run(TASK_ID)

    assert rig.store.steps[0].value_redacted == MASK


# --------------------------------------------------------------- budgets


async def test_running_out_of_time_times_the_task_out(settings: Settings) -> None:
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    budget = TaskBudget(max_steps=25, max_seconds=5, clock=lambda: next(clock))
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        budget=budget,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.TIMED_OUT
    assert outcome.failure_reason == "The task ran out of time."
    assert rig.store.task.status is TaskStatus.TIMED_OUT
    assert rig.bus.published[-1].type is EventType.FINAL
    assert rig.bus.published[-1].status is TaskStatus.TIMED_OUT


async def test_running_out_of_steps_fails_the_task(settings: Settings) -> None:
    budget = TaskBudget(max_steps=2, max_seconds=300)
    # The planner would happily keep going; the budget is what stops it.
    rig = build(
        script=[decision_payload(), decision_payload(), decision_payload()],
        settings=settings,
        budget=budget,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason == "The task ran out of steps before it finished."
    assert outcome.steps == 2
    assert rig.store.step_numbers() == [1, 2]
    assert rig.bus.published[-1].type is EventType.FINAL


async def test_the_two_budget_failures_are_told_apart(settings: Settings) -> None:
    # Out of time may succeed on a retry; out of steps is stuck. Reporting
    # both as the same state would hide that from whoever retries it.
    assert TaskStatus.TIMED_OUT is not TaskStatus.EXECUTION_FAILED


# ------------------------------------------------------------- failures


async def test_an_unexpected_exception_still_publishes_a_final_event(
    settings: Settings,
) -> None:
    class ExplodingBrowser(FakeBrowser):
        async def perceive(self) -> PageState:
            raise RuntimeError("chromium fell over at https://secret.internal/?token=abc")

    rig = build(
        script=[decision_payload()],
        browser=ExplodingBrowser(),
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert rig.bus.published[-1].type is EventType.FINAL
    assert rig.store.task.status is TaskStatus.EXECUTION_FAILED


async def test_the_failure_reason_never_quotes_the_exception(settings: Settings) -> None:
    leak = "https://secret.internal/?token=abc123"

    class ExplodingBrowser(FakeBrowser):
        async def perceive(self) -> PageState:
            raise RuntimeError(f"chromium fell over at {leak}")

    rig = build(
        script=[decision_payload()],
        browser=ExplodingBrowser(),
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    # Spec section 10: a URL with a token in it is exactly what must not be
    # stored or shown, and exception messages are full of them.
    assert leak not in (outcome.failure_reason or "")
    assert leak not in (rig.store.task.failure_reason or "")
    assert leak not in repr(rig.bus.published)
    assert outcome.failure_reason == "The task stopped after an internal error (RuntimeError)."


async def test_a_final_event_goes_out_even_when_the_database_is_gone(
    settings: Settings,
) -> None:
    rig = build(script=[decision_payload(), done_payload()], settings=settings)
    rig.store.fail_on_commit = True

    outcome = await rig.orchestrator.run(TASK_ID)

    # Every write fails, including the one that records the failure. A client
    # that never hears anything is left on a spinner forever, which is worse
    # than the outage that caused it.
    assert rig.bus.published[-1].type is EventType.FINAL
    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert rig.store.steps == []


async def test_a_planner_that_cannot_decide_fails_safely(settings: Settings) -> None:
    rig = build(
        script=[{"action": "not-an-action"}, {"action": "still-not"}],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason == (
        "The planner could not produce a valid decision for this page."
    )
    assert rig.bus.published[-1].type is EventType.FINAL


async def test_an_action_the_browser_rejects_fails_the_task(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        browser=FakeBrowser(ok=False),
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    # The ladder ran and could not rescue it, so the reason says what was
    # tried rather than only that something went wrong.
    assert outcome.failure_reason == (
        "Step 1: Could not type 'From': tried re-reading the page, "
        "re-locating by name and matching its text."
    )
    # The step is still recorded: a failure nobody can see is worse than one
    # that shows up in the log.
    assert rig.store.step_numbers() == [1]
    assert rig.store.steps[0].status is StepStatus.FAILED
    # Nothing ran, so the verifier was never asked: a stub that passes
    # everything must not be able to call a step that never happened a success.
    verification = rig.store.verifications[0]
    assert verification.verified is False
    assert verification.actual_outcome["errorCode"] == "ELEMENT_NOT_FOUND"


async def test_a_cancel_between_iterations_stops_the_loop(settings: Settings) -> None:
    rig = build(script=[decision_payload()], settings=settings)
    rig.store.task.status = TaskStatus.CANCELLED

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.CANCELLED
    assert rig.browser.executed == []
    assert rig.bus.published[-1].type is EventType.FINAL
    assert rig.bus.published[-1].status is TaskStatus.CANCELLED


async def test_a_cancel_during_a_step_lets_the_cancel_win(settings: Settings) -> None:
    store = FakeStore()

    class CancellingBus(WatchingBus):
        async def publish(self, task_id: uuid.UUID, event: Any) -> Any:
            published = await super().publish(task_id, event)
            if event.type is EventType.STEP:
                # The API cancels while the loop is mid-step, which is the
                # race the state machine would otherwise turn into an error.
                store.task.status = TaskStatus.CANCELLED
            return published

    rig = build(script=[decision_payload(), done_payload()], store=store, settings=settings)
    rig.orchestrator._events = CancellingBus(store)  # type: ignore[attr-defined]
    rig.bus = rig.orchestrator._events  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.CANCELLED
    assert rig.store.step_numbers() == [1]
    assert rig.bus.published[-1].type is EventType.FINAL
    assert rig.bus.published[-1].status is TaskStatus.CANCELLED


# ------------------------------------------------- extension point defaults


async def test_a_high_risk_action_is_refused_rather_than_waved_through(
    settings: Settings,
) -> None:
    rig = build(
        script=[decision_payload(riskLevel="HIGH"), done_payload()],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    # Until the approval flow exists, failing closed is the only safe default:
    # an auto-approving stub looks like working software right up to the day
    # an agent buys something.
    assert outcome.status is TaskStatus.REJECTED
    assert rig.browser.executed == []
    approval_statuses = [
        event.status.value for event in rig.bus.published if event.type is EventType.STATUS
    ]
    assert "AWAITING_APPROVAL" in approval_statuses


async def test_the_planner_asking_for_approval_is_enough(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(requiresApproval=True), done_payload()],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.REJECTED
    assert rig.browser.executed == []


async def test_a_deterministic_policy_can_escalate_the_planner_s_risk(
    settings: Settings,
) -> None:
    class AlwaysHigh:
        def assess(self, decision: Decision, state: PageState) -> RiskLevel:
            return RiskLevel.HIGH

    rig = build(
        script=[decision_payload(riskLevel="LOW"), done_payload()],
        settings=settings,
        risk_policy=AlwaysHigh(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    # The whole point of max(deterministic, LLM): a model calling a purchase
    # low risk cannot talk its way past a rule that says otherwise.
    assert outcome.status is TaskStatus.REJECTED
    assert rig.browser.executed == []


async def test_an_approving_gate_lets_the_step_run(settings: Settings) -> None:
    class Approve:
        async def request(self, **kwargs: Any) -> ApprovalOutcome:
            return ApprovalOutcome(approved=True, approval_id=uuid.uuid4())

    rig = build(
        script=[decision_payload(riskLevel="HIGH"), done_payload()],
        settings=settings,
        approvals=Approve(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert len(rig.browser.executed) == 2
    assert rig.store.decisions[0].risk_level is RiskLevel.HIGH


async def test_the_verdict_is_persisted_in_full(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)
    verification = rig.store.verifications[0]

    # Both JSONB columns come from the verdict, so a step can be argued about
    # later from the row alone rather than by rerunning the agent.
    assert verification.verified is True
    assert verification.verification_method is VerificationMethod.VALUE_EQUALS
    assert verification.expected_outcome == {"type": "value_equals", "value": "x"}
    assert verification.actual_outcome == {"check": "test"}


async def test_the_page_is_allowed_to_settle_before_it_is_checked(
    settings: Settings,
) -> None:
    rig = build(script=[decision_payload(), done_payload()], settings=settings)

    await rig.orchestrator.run(TASK_ID)

    # Once per step. Checking a click against a page that is still assembling
    # itself fails steps that worked.
    assert rig.browser.settle_calls == 2


async def test_the_state_read_after_an_action_is_reused_for_the_next_plan(
    settings: Settings,
) -> None:
    rig = build(script=[decision_payload(), done_payload()], settings=settings)

    await rig.orchestrator.run(TASK_ID)

    # One perception to plan the first step, then one after each action. The
    # after-state of one step is the before-state of the next, so a two step
    # run perceives three times rather than four.
    assert rig.browser.perceive_calls == 3


async def test_a_failed_verification_ends_the_task(settings: Settings) -> None:
    class AlwaysFails:
        async def check(self, decision: Decision, *args: Any) -> Verdict:
            return Verdict(
                ok=False,
                method=decision.expected_outcome.type,
                actual={"reason": "nothing changed"},
                detail="The page is unchanged.",
            )

    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        verifier=AlwaysFails(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    # The reason carries the verifier's own account, so a user is told what
    # was checked rather than only that something went wrong.
    # The ladder ran first, so the reason lists the rungs rather than only
    # the verifier's account.
    assert outcome.failure_reason.startswith("Step 1: Could not type 'From': tried ")
    assert rig.store.steps[0].status is StepStatus.FAILED


async def test_recovery_can_rescue_a_failed_step(settings: Settings) -> None:
    class AlwaysFails:
        async def check(self, decision: Decision, *args: Any) -> Verdict:
            return Verdict(ok=False, method=decision.expected_outcome.type)

    class Rescues:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, ctx: RecoveryContext) -> Verdict:
            self.calls += 1
            ctx.attempts.append(
                Attempt(
                    number=1,
                    strategy="retry",
                    label="retrying",
                    outcome=RecoveryOutcome.SUCCEEDED,
                )
            )
            return Verdict(ok=True, method=ctx.verdict.method, actual={"strategy": "retry"})

    recovery = Rescues()
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        verifier=AlwaysFails(),
        recovery=recovery,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert recovery.calls == 2
    assert rig.store.steps[0].status is StepStatus.VERIFIED


async def test_recovery_is_not_consulted_when_the_step_worked(settings: Settings) -> None:
    class Counting:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, ctx: RecoveryContext) -> None:
            self.calls += 1
            return

    recovery = Counting()
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        recovery=recovery,
    )

    await rig.orchestrator.run(TASK_ID)

    assert recovery.calls == 0


class RecordingCache:
    """A cache with one flow in it, and a note of what it was told."""

    def __init__(self, *steps: ReplayStep, forced: bool = False) -> None:
        self.flow_id = uuid.uuid4()
        self._plan = ReplayPlan(
            flow_id=self.flow_id,
            steps=tuple(steps),
            run_count=4,
            success_rate=1.0,
            forced=forced,
        )
        self.saved: tuple[ReplayStep, ...] | None = None
        self.settled: list[bool] = []

    async def plan(self, task_id: uuid.UUID, instruction: str) -> ReplayPlan | None:
        return self._plan if self._plan.steps else None

    async def save(
        self, task_id: uuid.UUID, instruction: str, steps: tuple[ReplayStep, ...]
    ) -> None:
        self.saved = steps

    async def settle(self, plan: ReplayPlan, *, replayed_all: bool) -> None:
        self.settled.append(replayed_all)


def cached_type(value: str = "Delhi", *, name: str = "From") -> ReplayStep:
    return ReplayStep(
        action=ActionType.TYPE,
        locator=Locator(role="input", name=name),
        value=value,
        target=name,
        expected_type=VerificationMethod.VALUE_EQUALS,
        expected_value=value,
    )


async def test_a_cached_flow_is_replayed_without_calling_the_planner(
    settings: Settings,
) -> None:
    """The saving, stated as a number.

    Two steps, one of them replayed: the planner is asked once, for the answer.
    A cache that still costs a model call per step has saved nothing worth
    having.
    """
    llm = MockLLM(script=[done_payload()])
    cache = RecordingCache(cached_type())
    rig = build(script=[], settings=settings, cache=cache)
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.cache_hit is True
    assert outcome.replayed_steps == 1
    assert llm.call_count == 1
    assert rig.bus.published[-1].stats.replayed_steps == 1
    steps = [event for event in rig.bus.published if event.type is EventType.STEP]
    assert [event.replayed for event in steps] == [True, False]


async def test_a_replayed_step_writes_no_llm_decision_row(settings: Settings) -> None:
    """Which is what makes the saving auditable after the fact."""
    cache = RecordingCache(cached_type())
    rig = build(script=[done_payload()], settings=settings, cache=cache)

    await rig.orchestrator.run(TASK_ID)

    assert len(rig.store.steps) == 2
    assert len(rig.store.decisions) == 1


async def test_a_target_that_no_longer_resolves_hands_over_to_the_planner(
    settings: Settings,
) -> None:
    """A stale flow costs the lookup and nothing else.

    The remaining steps go to the planner as a hint, and the task still
    finishes: a flow that has gone out of date is not a reason to fail a task
    the planner can do perfectly well.
    """
    llm = MockLLM(script=[decision_payload(), done_payload()])
    cache = RecordingCache(cached_type(name="Nowhere"), cached_type(name="Gone"))
    rig = build(script=[], settings=settings, cache=cache)
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.cache_hit is True
    assert outcome.replayed_steps == 0
    assert "RETRIEVED HINT" in llm.calls[0].last_user_text
    assert "Nowhere" in llm.calls[0].last_user_text


async def test_a_replay_that_stops_early_is_recorded_as_a_failed_run(
    settings: Settings,
) -> None:
    """Success for a flow is stricter than success for the task.

    A flow that needs rescuing every time is not worth replaying, even when
    the task around it completes.
    """
    cache = RecordingCache(cached_type(name="Nowhere"))
    rig = build(script=[decision_payload(), done_payload()], settings=settings, cache=cache)

    await rig.orchestrator.run(TASK_ID)

    assert cache.settled == [False]


async def test_a_completed_task_stores_only_its_verified_steps(settings: Settings) -> None:
    cache = RecordingCache()
    rig = build(script=[decision_payload(), done_payload()], settings=settings, cache=cache)

    await rig.orchestrator.run(TASK_ID)

    assert cache.saved is not None
    # The `done` step is never stored: replaying it would answer today's
    # question with yesterday's price.
    assert [step.action for step in cache.saved] == [ActionType.TYPE]
    assert cache.saved[0].locator is not None
    assert cache.saved[0].locator.name == "From"


async def test_a_masked_value_is_never_stored(settings: Settings) -> None:
    """Spec section 10, at the cache boundary.

    The collector masks the field at source, so the real value is not here to
    store. What matters is that the step is kept as a marker rather than
    dropped or stored with the mask as its value.
    """
    secret = element(0, name="Password", input_type="password", value=MASK)
    cache = RecordingCache()
    rig = build(
        script=[
            decision_payload(
                value="hunter2", expectedOutcome={"type": "value_equals", "value": "hunter2"}
            ),
            done_payload(),
        ],
        settings=settings,
        store=FakeStore(task=FakeTask(instruction="Log in with my password")),
        browser=FakeBrowser(page(secret)),
        cache=cache,
        approvals=ApprovingGate(),
    )

    await rig.orchestrator.run(TASK_ID)

    assert cache.saved is not None
    stored = cache.saved[0]
    assert stored.value is None
    assert stored.needs_planner is True


async def test_a_step_marked_for_the_planner_ends_the_replay(settings: Settings) -> None:
    """A cached password field is where replay stops, by construction."""
    llm = MockLLM(script=[decision_payload(), done_payload()])
    cache = RecordingCache(
        ReplayStep(action=ActionType.TYPE, locator=Locator(name="Password"), needs_planner=True),
        cached_type(),
    )
    rig = build(script=[], settings=settings, cache=cache)
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.replayed_steps == 0
    assert outcome.status is TaskStatus.COMPLETED


async def test_a_vision_grounded_step_is_counted_and_costs_more(
    settings: Settings,
) -> None:
    class Grounds:
        async def resolve(
            self, decision: Decision, state: PageState, screenshot: bytes, reason: str
        ) -> GroundingOutcome:
            used = decision.action is not ActionType.DONE
            return GroundingOutcome(
                decision=decision, used_vision=used, reason=reason, latency_ms=40, tokens=120
            )

    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        grounder=Grounds(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert rig.store.decisions[0].used_vision is True
    assert next(e for e in rig.bus.published if e.type is EventType.STEP).used_vision is True
    # Spec section 7: a vision-grounded step costs two credits, not one.
    assert outcome.credits_used == 3


async def test_grounding_choosing_a_missing_element_is_refused(settings: Settings) -> None:
    class GroundsWrong:
        async def resolve(
            self, decision: Decision, state: PageState, screenshot: bytes, reason: str
        ) -> GroundingOutcome:
            return GroundingOutcome(
                decision=decision.model_copy(update={"target_index": 99}),
                used_vision=True,
                reason=reason,
            )

    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        grounder=GroundsWrong(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    # The planner's own existence check ran before grounding touched the index,
    # so the loop has to check again or the guard has a hole in it.
    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason == "The planner chose an element that is not on the page."
    assert rig.browser.executed == []


async def test_a_custom_session_manager_names_the_tab(settings: Settings) -> None:
    class Stable:
        def tab_id(self, state: PageState) -> str:
            return "stable-tab-7"

    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        sessions=Stable(),
    )

    await rig.orchestrator.run(TASK_ID)

    assert rig.store.steps[0].tab_id == "stable-tab-7"
    assert next(e for e in rig.bus.published if e.type is EventType.STEP).tab_id == "stable-tab-7"


# ------------------------------------------------------------ run outcome


async def test_run_outcome_is_returned_for_the_caller(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert isinstance(outcome, RunOutcome)
    assert outcome.duration_ms >= 0
    assert outcome.steps == 2


# ------------------------------------------------------------- sign-in hand-off


class LoginThenFormBrowser(FakeBrowser):
    """Shows a sign-in page first, then the real page once the person has signed in."""

    def __init__(self) -> None:
        super().__init__(page(element(0)))
        self.indicators: list[str] = []

    async def perceive(self) -> PageState:
        self.perceive_calls += 1
        if self.perceive_calls == 1:
            return page(
                element(0, name="Email", input_type="email"),
                element(1, name="Password", input_type="password"),
                url="https://example.test/login",
            )
        return self._state

    async def set_indicator(self, mode: str) -> None:
        self.indicators.append(mode)


async def test_a_sign_in_page_hands_the_browser_to_the_person(settings: Settings) -> None:
    gate = ApprovingGate()
    browser = LoginThenFormBrowser()
    rig = build(
        script=[done_payload("Signed in and done")],
        settings=settings,
        browser=browser,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert len(gate.requests) == 1
    prompt = gate.requests[0]["reasons"][0]
    assert "sign in or sign up" in prompt
    # Nothing was typed into the sign-in form, and the page was read again afterwards.
    assert browser.executed == [] or all(a.action.value == "done" for a in browser.executed)
    assert browser.perceive_calls >= 2
    assert browser.indicators == ["waiting", "working"]


async def test_declining_to_sign_in_stops_the_task(settings: Settings) -> None:
    class Decline:
        async def request(self, **kwargs: Any) -> ApprovalOutcome:
            return ApprovalOutcome(approved=False, approval_id=uuid.uuid4(), reason="No thanks")

    rig = build(
        script=[done_payload()],
        settings=settings,
        browser=LoginThenFormBrowser(),
        approvals=Decline(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.REJECTED


def test_login_wall_detection_is_deterministic() -> None:
    from app.agent.orchestrator import _login_wall

    password = page(element(0, name="Password", input_type="password"))
    plain = page(element(0, name="Search"))
    login_path = page(
        element(0, name="Email address", input_type="email"),
        url="https://example.test/accounts/login",
    )

    assert _login_wall(password, "find internships")
    assert not _login_wall(plain, "find internships")
    assert _login_wall(login_path, "find internships")
    # A goal that is itself about credentials is left alone.
    assert not _login_wall(password, "log in with my password")


async def test_grounding_cannot_move_a_type_onto_an_unnamed_input(settings: Settings) -> None:
    """The planner chose the named box; grounding must not swap in the unnamed one."""
    named = element(0, name="Search here")
    unnamed = element(1, name="")

    class MoveToUnnamed:
        async def ground(self, decision: Decision, state: PageState, **kwargs: Any) -> Any:
            return None

    rig = build(
        script=[
            decision_payload(
                targetIndex=0,
                value="Full Stack",
                expectedOutcome={"type": "value_equals", "value": "Full Stack"},
            ),
            done_payload(),
        ],
        settings=settings,
        browser=FakeBrowser(page(named, unnamed)),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    first = rig.browser.executed[0]
    assert "0" in first.selector or first.selector


# ------------------------------------------------------------- plan preview


async def test_the_outline_is_stored_and_published_before_the_first_step(
    settings: Settings,
) -> None:
    from app.services.events import EventType

    rig = build(
        script=[
            {"steps": ["Open the form", "Fill the origin", "Read the fare"]},
            decision_payload(),
            done_payload(),
        ],
        settings=settings.model_copy(update={"AGENT_PLAN_PREVIEW": True}),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert rig.store.task.plan == ["Open the form", "Fill the origin", "Read the fare"]
    kinds = [event.type for event in rig.bus.published]
    assert kinds.index(EventType.PLAN) < kinds.index(EventType.STEP)


async def test_no_outline_call_is_made_when_the_preview_is_off(settings: Settings) -> None:
    from app.services.events import EventType

    rig = build(script=[decision_payload(), done_payload()], settings=settings)

    await rig.orchestrator.run(TASK_ID)

    assert EventType.PLAN not in [event.type for event in rig.bus.published]
    assert rig.store.task.plan is None


async def test_a_replayed_step_that_fails_verification_falls_back_to_the_planner(
    settings: Settings,
) -> None:
    """The task is in RECOVERING when the replay gives up, and RECOVERING cannot
    move straight to PLANNING. It must walk back through VERIFYING."""

    class FailsOnce(PassingVerifier):
        async def check(self, decision: Decision, *args: Any) -> Verdict:
            self.calls += 1
            ok = self.calls > 1
            return Verdict(
                ok=ok,
                method=decision.expected_outcome.type,
                expected={"type": decision.expected_outcome.type.value, "value": "x"},
                actual={"check": "test"},
                detail="Passed." if ok else "Failed once.",
            )

    llm = MockLLM(script=[done_payload("Answered by the planner")])
    rig = build(
        script=[],
        settings=settings,
        cache=RecordingCache(cached_type()),
        verifier=FailsOnce(),
    )
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.final_answer == "Answered by the planner"


def test_the_page_saying_it_needs_a_login_is_a_login_wall() -> None:
    from app.agent.orchestrator import _login_wall

    button = element(0, name="Login").model_copy(update={"tag": "button"})
    needs = page(button).model_copy(update={"text": "Please login to continue booking."})
    plain = page(button).model_copy(update={"text": "Welcome. Login | Register"})

    assert _login_wall(needs, "book a ticket")
    assert not _login_wall(plain, "book a ticket")


async def test_the_login_wait_is_long_and_does_not_count_against_the_task_clock(
    settings: Settings,
) -> None:
    gate = ApprovingGate()
    rig = build(
        script=[done_payload("Signed in and done")],
        settings=settings.model_copy(update={"AGENT_LOGIN_WAIT_SECONDS": 1234}),
        browser=LoginThenFormBrowser(),
        approvals=gate,
    )

    await rig.orchestrator.run(TASK_ID)

    assert gate.requests[0]["timeout_seconds"] == 1234
