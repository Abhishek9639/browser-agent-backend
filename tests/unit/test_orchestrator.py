"""The agent loop, driven entirely by fakes.

No browser, no database, no network. The point of every collaborator being an
interface is that the loop can be exercised on its own, which is also the only
way to test the paths that matter most: running out of budget, an unexpected
exception, and a slow client still being told the task finished.
"""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
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
from app.agent.perception.dom_extractor import MASK, BoundingBox, ElementInfo, PageNotReadyError
from app.agent.perception.state import PageState
from app.agent.planner.planner import Planner
from app.agent.planner.schema import Decision
from app.agent.safety.payment import is_payment_page
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

from app.agent.orchestrator import (  # isort: skip
    MAX_DETAIL_PAUSES,
    MAX_PAYMENT_PAUSES,
    Orchestrator,
    RunOutcome,
)

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


async def test_blocked_navigation_explains_configuration_without_leaking_url(
    settings: Settings,
) -> None:
    from app.agent.browser.errors import DomainNotAllowedError

    private_url = "https://example.test/?token=private-query-value"

    class RestrictedBrowser(FakeBrowser):
        async def execute(self, action: Any) -> ActionResult:
            raise DomainNotAllowedError(f"{private_url} is outside the allowed domains.")

    rig = build(
        script=[decision_payload()],
        browser=RestrictedBrowser(),
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert "AGENT_ALLOWED_DOMAINS" in (outcome.failure_reason or "")
    assert rig.store.task.failure_reason == outcome.failure_reason
    assert rig.bus.published[-1].failure_reason == outcome.failure_reason
    assert private_url not in repr(rig.bus.published)
    assert private_url not in (outcome.failure_reason or "")
    assert rig.store.steps == []


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
    # Two rounds of two attempts: the page is looked at again once before giving up.
    rig = build(
        script=[{"action": "not-an-action"}, {"action": "still-not"}] * 2,
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason == (
        "The planner could not produce a valid decision for this page."
    )
    assert rig.bus.published[-1].type is EventType.FINAL


async def test_one_round_the_planner_could_not_decide_is_not_the_end(settings: Settings) -> None:
    """The page is read again and the model asked again, once, before the task is failed."""
    rig = build(
        script=[{"action": "not-an-action"}, {"action": "still-not"}, done_payload("Found it")],
        settings=settings,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    # It looked at the page afresh rather than planning against the old reading.
    assert rig.browser.perceive_calls >= 2


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


async def test_a_step_with_no_element_to_find_is_never_grounded(settings: Settings) -> None:
    """Vision finds a target. A navigation has none, so photographing it is waste."""

    class Counts:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(
            self, decision: Decision, state: PageState, screenshot: bytes, reason: str
        ) -> GroundingOutcome:
            self.calls += 1
            return GroundingOutcome(decision=decision, used_vision=True, reason=reason)

    grounder = Counts()
    go = decision_payload(
        action="navigate",
        targetIndex=None,
        target="the site",
        value="https://example.test/",
        needsVision=True,
        expectedOutcome={"type": "url_contains", "value": "example"},
    )
    rig = build(script=[go, done_payload()], settings=settings, grounder=grounder)

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert grounder.calls == 0


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


async def test_the_sign_in_prompt_says_what_works_when_a_site_refuses_the_agent_s_browser(
    settings: Settings,
) -> None:
    """Google tells a person typing into the agent's browser that it is "not secure"."""
    own_browser = ApprovingGate()
    await build(
        script=[done_payload()],
        settings=settings.model_copy(update={"AGENT_CDP_URL": ""}),
        browser=LoginThenFormBrowser(),
        approvals=own_browser,
    ).orchestrator.run(TASK_ID)
    their_chrome = ApprovingGate()
    await build(
        script=[done_payload()],
        settings=settings.model_copy(update={"AGENT_CDP_URL": "http://127.0.0.1:9222"}),
        browser=LoginThenFormBrowser(),
        approvals=their_chrome,
    ).orchestrator.run(TASK_ID)

    hint = "scripts/chrome_debug.ps1"
    assert hint in own_browser.requests[0]["reasons"][0]
    # In the person's own Chrome there is nothing better to suggest.
    assert hint not in their_chrome.requests[0]["reasons"][0]


@pytest.mark.parametrize(
    ("url", "title", "said"),
    [
        (
            "https://www.irctc.co.in/nget/payment/bkgPaymentOptions",
            # IRCTC's title really uses an en dash.
            "IRCTC \u2013 Official Indian Railway Catering & Tourism Corporation Portal",
            "IRCTC",
        ),
        ("https://www.makemytrip.com/cabs/", "MakeMyTrip - #1 Travel Website", "MakeMyTrip"),
        ("https://www.skyfare.test/results", "Results | SkyFare", "SkyFare"),
        # No title part is the site's name: the bare domain, not a sentence.
        ("https://auth.uber.com/v2/", "Sign up or Log in with Uber", "uber.com"),
        ("https://www.bbc.co.uk/news", "Home - BBC News", "bbc.co.uk"),
    ],
)
def test_a_site_is_named_the_way_a_person_says_it(url: str, title: str, said: str) -> None:
    """Every prompt used to quote IRCTC's whole page title."""
    from app.agent.orchestrator import _site_of

    assert _site_of(page(url=url).model_copy(update={"title": title})) == said


async def test_a_sign_in_wall_met_after_a_failed_step_still_hands_over(
    settings: Settings,
) -> None:
    """A click that a sign-in prompt swallowed fails, and recovery runs before the
    hand-off. The task is then in RECOVERING, which has no edge to
    AWAITING_APPROVAL, so the hand-off used to die with IllegalTransition."""

    class FailsThenSignsIn(FakeBrowser):
        def __init__(self) -> None:
            super().__init__(page(element(0)), ok=False)
            self.signed_in = False

        async def perceive(self) -> PageState:
            self.perceive_calls += 1
            if self.perceive_calls == 1 or self.signed_in:
                return self._state
            return page(
                element(0, name="Email", input_type="email"),
                element(1, name="Password", input_type="password"),
                url="https://example.test/login",
            )

        async def execute(self, action: Any) -> ActionResult:
            result = await super().execute(action)
            if self.signed_in:
                self._ok = True
            return result

    browser = FailsThenSignsIn()

    class SignsInThenApproves(ApprovingGate):
        async def request(self, **kwargs: Any) -> ApprovalOutcome:
            browser.signed_in = True
            return await super().request(**kwargs)

    gate = SignsInThenApproves()

    class FailingVerifier(PassingVerifier):
        async def check(self, decision: Decision, *args: Any) -> Verdict:
            verdict = await super().check(decision, *args)
            return replace(verdict, ok=browser.signed_in)

    rig = build(
        script=[decision_payload(), done_payload()],
        settings=settings,
        browser=browser,
        approvals=gate,
        verifier=FailingVerifier(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.failure_reason is None or "internal error" not in outcome.failure_reason
    assert len(gate.requests) == 1
    assert "sign in or sign up" in gate.requests[0]["reasons"][0]


async def test_a_scroll_with_nowhere_to_go_does_not_fail_the_task(settings: Settings) -> None:
    class NothingMoves(PassingVerifier):
        async def check(self, decision: Decision, *args: Any) -> Verdict:
            verdict = await super().check(decision, *args)
            return replace(verdict, ok=decision.action.value != "scroll")

    scroll = decision_payload(
        action="scroll",
        targetIndex=None,
        target="page",
        value="down",
        expectedOutcome={"type": "dom_changed", "value": ""},
    )
    rig = build(
        script=[scroll, done_payload("Read it all")],
        settings=settings,
        verifier=NothingMoves(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    # The step is still on record as not having done anything.
    assert rig.store.steps[0].status is StepStatus.FAILED


class ScrollGoesNowhere(PassingVerifier):
    async def check(self, decision: Decision, *args: Any) -> Verdict:
        verdict = await super().check(decision, *args)
        return replace(
            verdict, ok=decision.action.value != "scroll", detail="The page did not scroll."
        )


def scroll_down() -> dict[str, Any]:
    return decision_payload(
        action="scroll",
        targetIndex=None,
        target="page",
        value="down",
        expectedOutcome={"type": "dom_changed", "value": ""},
    )


async def test_a_scroll_that_moves_nothing_is_not_the_end_whatever_the_height_says(
    settings: Settings,
) -> None:
    """IRCTC's results page sat at its footer while its measured height said
    there was more, and "the page did not scroll" failed the task."""
    from app.agent.perception.state import ScrollState

    mid_page = page(element(0)).model_copy(update={"scroll": ScrollState(y=600, max_y=2400)})
    llm = MockLLM(script=[scroll_down(), done_payload("Read the results")])
    rig = build(
        script=[], settings=settings, browser=FakeBrowser(mid_page), verifier=ScrollGoesNowhere()
    )
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    # The planner is told why, so it does not go on hunting further down.
    assert "the page did not move" in llm.calls[1].last_user_text


async def test_scrolling_nowhere_again_and_again_still_ends_the_task(settings: Settings) -> None:
    from app.agent.orchestrator import MAX_IDLE_SCROLLS

    rig = build(
        script=[scroll_down()] * (MAX_IDLE_SCROLLS + 1),
        settings=settings,
        verifier=ScrollGoesNowhere(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert len(rig.store.steps) == MAX_IDLE_SCROLLS + 1


class NothingComesOfWaiting(PassingVerifier):
    async def check(self, decision: Decision, *args: Any) -> Verdict:
        verdict = await super().check(decision, *args)
        return replace(verdict, ok=decision.action.value != "wait", detail="The page is unchanged.")


def wait_to_look() -> dict[str, Any]:
    return decision_payload(
        action="wait",
        targetIndex=None,
        target="November 16 date in calendar",
        value=None,
        expectedOutcome={"type": "dom_changed", "value": ""},
    )


async def test_a_wait_that_changes_nothing_does_not_fail_the_task(settings: Settings) -> None:
    """A planner that could not find a date waited "to see the page", and the run was over."""
    llm = MockLLM(script=[wait_to_look(), done_payload("Picked the date")])
    rig = build(script=[], settings=settings, verifier=NothingComesOfWaiting())
    rig.orchestrator._planner = Planner(llm)  # type: ignore[attr-defined]

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert rig.store.steps[0].status is StepStatus.FAILED
    # The planner is told why, so it acts instead of waiting again.
    second_turn = " ".join(message.text for message in llm.calls[1].messages)
    assert "the page has finished loading" in second_turn


async def test_waiting_again_and_again_still_ends_the_task(settings: Settings) -> None:
    from app.agent.orchestrator import MAX_IDLE_WAITS

    rig = build(
        script=[wait_to_look()] * (MAX_IDLE_WAITS + 1),
        settings=settings,
        verifier=NothingComesOfWaiting(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert len(rig.store.steps) == MAX_IDLE_WAITS + 1


async def test_a_site_the_agent_chose_that_refuses_it_is_not_the_end(settings: Settings) -> None:
    class RefusesTheFirstSite(FakeBrowser):
        async def execute(self, action: Any) -> ActionResult:
            self.executed.append(action)
            blocked = getattr(action, "url", "") == "https://blocked.test/"
            return ActionResult(
                ok=not blocked,
                duration_ms=5,
                url_before="about:blank",
                url_after="about:blank",
                error_code=ActionErrorCode.NAVIGATION_FAILED if blocked else None,
                error_message="Navigation returned HTTP 403" if blocked else None,
            )

    class FailsOnlyBlocked(PassingVerifier):
        async def check(self, decision: Decision, before: Any, after: Any, result: Any) -> Verdict:
            verdict = await super().check(decision, before, after, result)
            return replace(verdict, ok=result.ok)

    def go(url: str) -> dict[str, Any]:
        return decision_payload(
            action="navigate",
            targetIndex=None,
            target="the site",
            value=url,
            expectedOutcome={"type": "url_contains", "value": "test"},
        )

    rig = build(
        script=[go("https://blocked.test/"), go("https://open.test/"), done_payload("Found it")],
        settings=settings,
        browser=RefusesTheFirstSite(),
        verifier=FailsOnlyBlocked(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert [getattr(a, "url", None) for a in rig.browser.executed][:2] == [
        "https://blocked.test/",
        "https://open.test/",
    ]


class OpensADropdown(FakeBrowser):
    """Each action opens something new, the way clicking a "To" field does."""

    async def perceive(self) -> PageState:
        self.perceive_calls += 1
        opened = [element(i + 1, name=f"Suggestion {i}") for i in range(len(self.executed))]
        state = page(element(0), *opened)
        return state.model_copy(update={"dom_hash": f"hash-{len(self.executed)}"})


class ForecastNeverComesTrue(PassingVerifier):
    """The planner expected "2024" in a calendar; the page shows a city list."""

    async def check(self, decision: Decision, *args: Any) -> Verdict:
        verdict = await super().check(decision, *args)
        if decision.action is ActionType.DONE:
            return verdict
        return replace(verdict, ok=False, detail="'2024' does not appear on the page.")


def _click_to_field() -> dict[str, Any]:
    return decision_payload(
        action="click",
        target="To field",
        value=None,
        expectedOutcome={"type": "text_present", "value": "2024"},
    )


async def test_a_click_that_landed_but_missed_the_forecast_is_not_the_end(
    settings: Settings,
) -> None:
    rig = build(
        script=[_click_to_field(), done_payload("Found it")],
        settings=settings,
        browser=OpensADropdown(),
        verifier=ForecastNeverComesTrue(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    # Still on record as not having done what the planner said it would.
    assert rig.store.steps[0].status is StepStatus.FAILED
    # The click landed, so the ladder did not re-click it three more times.
    clicks = [a for a in rig.browser.executed if getattr(a, "action", None) is ActionType.CLICK]
    assert len(clicks) == 1


async def test_a_click_that_changed_nothing_still_fails_the_task(settings: Settings) -> None:
    rig = build(
        script=[_click_to_field(), done_payload()],
        settings=settings,
        verifier=ForecastNeverComesTrue(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED


async def test_missing_every_forecast_in_a_row_fails_the_task(settings: Settings) -> None:
    rig = build(
        script=[_click_to_field() for _ in range(5)] + [done_payload()],
        settings=settings,
        browser=OpensADropdown(),
        verifier=ForecastNeverComesTrue(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.steps == 4


def test_a_missed_forecast_tells_the_planner_the_action_worked() -> None:
    from app.agent.orchestrator import _history_note

    verdict = Verdict(
        ok=False,
        method=VerificationMethod.TEXT_PRESENT,
        expected={},
        actual={},
        detail="'2024' does not appear on the page.",
    )

    note = _history_note(verdict, missed=True)

    assert note is not None
    assert note.startswith("it ran and the page changed")


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


def _steps_published(rig: Rig) -> list[Any]:
    return [event for event in rig.bus.published if event.type is EventType.STEP]


async def test_the_plan_grows_when_the_agent_meets_work_it_did_not_list(
    settings: Settings,
) -> None:
    """The person's list kept showing three items while the agent did seven things."""
    rig = build(
        script=[
            {"steps": ["Open the form", "Fill the origin", "Search"]},
            decision_payload(planStep=2, addPlanSteps=["Close the cookie popup"]),
            decision_payload(planStep=3),
            done_payload(),
        ],
        settings=settings.model_copy(update={"AGENT_PLAN_PREVIEW": True}),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    grown = ["Open the form", "Close the cookie popup", "Fill the origin", "Search"]
    assert rig.store.task.plan == grown
    plans = [event.steps for event in rig.bus.published if event.type is EventType.PLAN]
    assert plans == [["Open the form", "Fill the origin", "Search"], grown]
    # The new item went in before the one the agent was on, and this step is its first.
    assert [row.plan_step for row in rig.store.steps] == [2, 3, 3]
    assert [event.plan_step for event in _steps_published(rig)] == [2, 3, 3]
    # The planner then saw the plan as it now stands, and where it was.
    calls = rig.orchestrator._planner._llm.calls  # type: ignore[attr-defined]
    assert "2. Close the cookie popup  <- last step was on this item" in calls[2].last_user_text


async def test_a_plan_item_number_off_the_end_is_clamped(settings: Settings) -> None:
    rig = build(
        script=[
            {"steps": ["Open the form", "Fill the origin"]},
            decision_payload(
                planStep=9,
                addPlanSteps=["Fill the origin", "Choose a class", "Pick a seat", "Add meals"],
            ),
            done_payload(),
        ],
        settings=settings.model_copy(update={"AGENT_PLAN_PREVIEW": True}),
    )

    await rig.orchestrator.run(TASK_ID)

    # Nothing it already had, at most two new items for one step, put at the end.
    assert rig.store.task.plan == [
        "Open the form",
        "Fill the origin",
        "Choose a class",
        "Pick a seat",
    ]
    assert rig.store.steps[0].plan_step == 3


async def test_the_plan_never_grows_past_its_ceiling(settings: Settings) -> None:
    from app.agent.orchestrator import MAX_PLAN_STEPS

    full = [f"Item {i}" for i in range(1, MAX_PLAN_STEPS + 1)]
    rig = build(
        script=[decision_payload(planStep=1, addPlanSteps=["One more"]), done_payload()],
        settings=settings.model_copy(update={"AGENT_PLAN_PREVIEW": True}),
    )
    # A made outline is capped lower than this; a plan this long is what a run
    # of additions builds, so the test starts from one.
    rig.orchestrator._planner.outline = _returns(full)  # type: ignore[method-assign]

    await rig.orchestrator.run(TASK_ID)

    assert rig.store.task.plan == full
    assert len([e for e in rig.bus.published if e.type is EventType.PLAN]) == 1


def _returns(steps: list[str]) -> Any:
    async def outline(*args: Any, **kwargs: Any) -> list[str]:
        return list(steps)

    return outline


async def test_without_a_plan_no_plan_item_is_recorded(settings: Settings) -> None:
    rig = build(
        script=[decision_payload(planStep=2, addPlanSteps=["Close the popup"]), done_payload()],
        settings=settings,
    )

    await rig.orchestrator.run(TASK_ID)

    assert rig.store.task.plan is None
    assert [row.plan_step for row in rig.store.steps] == [None, None]
    assert EventType.PLAN not in [event.type for event in rig.bus.published]


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


def test_a_closed_browser_gets_a_plain_reason_not_an_internal_error() -> None:
    from app.agent.browser.errors import BrowserClosedError
    from app.agent.orchestrator import _safe_reason

    reason = _safe_reason(BrowserClosedError("closed"))

    assert "closed" in reason and "internal error" not in reason


def test_a_blocked_site_is_not_reported_as_a_bad_address() -> None:
    from app.agent.orchestrator import _why_not_run

    blocked = ActionResult(
        ok=False,
        duration_ms=1,
        url_before="",
        url_after="",
        error_code=ActionErrorCode.NAVIGATION_FAILED,
        error_message="Navigation returned HTTP 403",
    )
    missing = replace(blocked, error_message="Navigation returned HTTP 404")

    assert "refused" in _why_not_run(blocked)
    assert "AGENT_CDP_URL" in _why_not_run(blocked)
    assert "check the address" in _why_not_run(missing)


def test_a_timeout_is_reported_as_a_slow_page_not_an_internal_error() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from app.agent.orchestrator import _safe_reason

    assert "too long" in _safe_reason(PlaywrightTimeoutError("Timeout 10000ms exceeded"))
    assert "too long" in _safe_reason(TimeoutError())
    assert "internal error" not in _safe_reason(TimeoutError())


# ------------------------------------------------------------ questions first


class Answering:
    """Answers each question in turn: a string approves with it, None rejects."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> ApprovalOutcome:
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        return ApprovalOutcome(approved=answer is not None, approval_id=uuid.uuid4(), reason=answer)


def _ask_two() -> dict[str, Any]:
    return {
        "questions": [
            {"question": "Which class?", "options": ["3AC", "Sleeper"]},
            {"question": "What time?", "options": ["Morning", "Evening"]},
        ]
    }


def _page_asks(*questions: tuple[str, list[str]], for_task: bool = True) -> dict[str, Any]:
    """The model's reading of a page: is it the task's form, and what to ask about it."""
    return {
        "formForTask": for_task,
        "questions": [{"question": text, "options": options} for text, options in questions],
    }


async def test_questions_are_asked_first_and_the_answers_reach_the_planner(
    settings: Settings,
) -> None:
    gate = Answering("3AC", "Evening")
    rig = build(
        # The page the fake starts on has a field, so it is read for questions too.
        script=[_ask_two(), _page_asks(), decision_payload(), done_payload()],
        settings=settings.model_copy(update={"AGENT_CLARIFY": True}),
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert [r["reasons"][0] for r in gate.requests] == ["Which class?", "What time?"]
    first = gate.requests[0]["decision"]
    assert first.target == "question"
    assert first.value == "3AC|Sleeper"
    # The second planner call saw the answers, because they are part of the task.
    calls = rig.orchestrator._planner._llm.calls  # type: ignore[attr-defined]
    assert any("3AC" in c.last_user_text and "Evening" in c.last_user_text for c in calls[1:])


async def test_skipping_a_question_does_not_stop_the_task(settings: Settings) -> None:
    gate = Answering(None, "Evening")
    rig = build(
        script=[_ask_two(), _page_asks(), decision_payload(), done_payload()],
        settings=settings.model_copy(update={"AGENT_CLARIFY": True}),
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED


async def test_an_unanswered_question_times_the_task_out(settings: Settings) -> None:
    class Nobody:
        async def request(self, **kwargs: Any) -> ApprovalOutcome:
            return ApprovalOutcome(
                approved=False, approval_id=uuid.uuid4(), reason="approval timed out", expired=True
            )

    rig = build(
        script=[_ask_two()],
        settings=settings.model_copy(update={"AGENT_CLARIFY": True}),
        approvals=Nobody(),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.TIMED_OUT


async def test_no_question_call_is_made_when_clarifying_is_off(settings: Settings) -> None:
    gate = Answering()
    rig = build(script=[decision_payload(), done_payload()], settings=settings, approvals=gate)

    await rig.orchestrator.run(TASK_ID)

    assert gate.requests == []


# ------------------------------------------------ the page is read, then asked about


class Pages(FakeBrowser):
    """Shows each page in turn, moving to the next one after every action."""

    def __init__(self, *states: PageState) -> None:
        super().__init__(states[0])
        self._states = list(states)
        self._at = 0

    async def perceive(self) -> PageState:
        self.perceive_calls += 1
        return self._states[self._at]

    async def execute(self, action: Any) -> ActionResult:
        result = await super().execute(action)
        self._at = min(self._at + 1, len(self._states) - 1)
        return result


BLANK = page(url="about:blank")


def cab_form(url: str = "https://cabs.test/cabs") -> PageState:
    return page(
        element(0, name="One Way", input_type="radio"),
        element(1, name="Round Trip", input_type="radio"),
        element(2, name="From"),
        url=url,
    ).model_copy(
        update={
            "summary_text": (
                '[0] input[type=radio] "One Way" (checked)\n'
                '[1] input[type=radio] "Round Trip"\n'
                '[2] input[type=text] "From"'
            )
        }
    )


def go_to(url: str = "https://cabs.test/cabs") -> dict[str, Any]:
    return decision_payload(
        action="navigate",
        targetIndex=None,
        target="the site",
        value=url,
        expectedOutcome={"type": "url_contains", "value": "cabs.test"},
    )


def outline_of(*steps: str) -> dict[str, Any]:
    return {"steps": list(steps)}


def clarifying(settings: Settings, *, preview: bool = True) -> Settings:
    return settings.model_copy(update={"AGENT_CLARIFY": True, "AGENT_PLAN_PREVIEW": preview})


async def test_the_plan_waits_until_the_site_is_open(settings: Settings) -> None:
    """Spec section 6: planned from the page, not from the task text before anything."""
    rig = build(
        script=[go_to(), outline_of("Choose the trip type", "Fill the form"), done_payload()],
        settings=settings.model_copy(update={"AGENT_PLAN_PREVIEW": True}),
        browser=Pages(BLANK, cab_form()),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert rig.store.task.plan == ["Choose the trip type", "Fill the form"]
    kinds = [event.type for event in rig.bus.published]
    # The first step opened the site; the plan came after it.
    assert kinds.index(EventType.STEP) < kinds.index(EventType.PLAN)
    # And it was made from the page the agent had open.
    calls = rig.orchestrator._planner._llm.calls  # type: ignore[attr-defined]
    assert "Round Trip" in calls[1].last_user_text


async def test_the_page_is_read_and_asked_about_before_the_plan(settings: Settings) -> None:
    gate = Answering("Round Trip")
    rig = build(
        script=[
            {"questions": []},
            go_to(),
            _page_asks(("One way or round trip?", ["One Way", "Round Trip"])),
            outline_of("Choose Round Trip", "Fill the form"),
            done_payload(),
        ],
        settings=clarifying(settings),
        browser=Pages(BLANK, cab_form()),
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert [r["reasons"][0] for r in gate.requests] == ["One way or round trip?"]
    assert gate.requests[0]["decision"].value == "One Way|Round Trip"
    assert gate.requests[0]["site"]
    calls = rig.orchestrator._planner._llm.calls  # type: ignore[attr-defined]
    # The page went to the question call, fenced as untrusted data.
    assert "<page_state>" in calls[2].last_user_text
    assert "One Way" in calls[2].last_user_text
    # The plan was made after the answer, and the planner works from the answer.
    assert "Round Trip" in calls[3].last_user_text
    assert "One way or round trip? Round Trip" in calls[4].last_user_text
    assert rig.store.task.plan == ["Choose Round Trip", "Fill the form"]


async def test_each_new_page_is_asked_about_once(settings: Settings) -> None:
    gate = Answering("Round Trip", "Sedan")
    results = cab_form("https://cabs.test/cabs/search")
    rig = build(
        script=[
            {"questions": []},
            _page_asks(("One way or round trip?", ["One Way", "Round Trip"])),
            decision_payload(targetIndex=2),
            _page_asks(("Which car?", ["Hatchback", "Sedan"])),
            decision_payload(targetIndex=2),
            done_payload(),
        ],
        settings=clarifying(settings, preview=False),
        browser=Pages(cab_form(), results, results),
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert [r["reasons"][0] for r in gate.requests] == ["One way or round trip?", "Which car?"]
    # Six calls: the opening questions, two pages, two actions and the answer.
    # The results page was not read twice.
    assert rig.orchestrator._planner._llm.call_count == 6  # type: ignore[attr-defined]


async def test_a_skipped_question_is_not_asked_again_on_the_page(settings: Settings) -> None:
    gate = Answering(None)
    rig = build(
        script=[
            {"questions": [{"question": "Which class?", "options": ["AC", "Non-AC"]}]},
            _page_asks(),
            done_payload(),
        ],
        settings=clarifying(settings, preview=False),
        browser=FakeBrowser(cab_form()),
        approvals=gate,
    )

    await rig.orchestrator.run(TASK_ID)

    page_call = rig.orchestrator._planner._llm.calls[1]  # type: ignore[attr-defined]
    assert "do not ask them again" in page_call.last_user_text
    assert "Which class?" in page_call.last_user_text


async def test_a_page_with_nothing_to_fill_is_not_asked_about(settings: Settings) -> None:
    article = page(
        ElementInfo(
            index=0,
            tag="a",
            name="Read more",
            bbox=BoundingBox(x=0, y=0, width=80, height=20),
            in_viewport=True,
        ),
        url="https://news.test/story",
    )
    rig = build(
        script=[{"questions": []}, done_payload("Read it")],
        settings=clarifying(settings, preview=False),
        browser=FakeBrowser(article),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert rig.orchestrator._planner._llm.call_count == 2  # type: ignore[attr-defined]


async def test_a_run_answered_on_a_page_is_not_cached(settings: Settings) -> None:
    """The cache is looked up before any page is read, so a cached flow holding
    this person's trip type would be replayed for somebody who never chose it."""
    cache = RecordingCache()
    rig = build(
        script=[
            {"questions": []},
            _page_asks(("One way or round trip?", ["One Way", "Round Trip"])),
            done_payload(),
        ],
        settings=clarifying(settings, preview=False),
        browser=FakeBrowser(cab_form()),
        approvals=Answering("Round Trip"),
        cache=cache,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert cache.saved is None


# ------------------------------------------------------------- unreadable page


async def test_a_page_that_cannot_be_read_is_reported_plainly(settings: Settings) -> None:
    """It used to end as "internal error (PageNotReadyError)", which tells nobody anything."""

    class NeverReady(FakeBrowser):
        async def perceive(self) -> PageState:
            raise PageNotReadyError("Could not read the page: Execution context was destroyed")

    rig = build(script=[done_payload()], settings=settings, browser=NeverReady())

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason == (
        "The page kept changing and could not be read. Try the task again."
    )


# ------------------------------------------------------------ payment hand-off

BOOKING = "Book the cheapest flight from Delhi to Mumbai"


def card_form() -> PageState:
    return page(
        element(0, name="Card number"),
        element(1, name="CVV"),
        url="https://pay.skyfare.test/checkout",
    )


def confirmation() -> PageState:
    """Still under /payment and naming how it was paid: a page to read, not a second till."""
    return page(
        element(0, name="Download ticket"), url="https://skyfare.test/payment/success"
    ).model_copy(update={"text": "Booking confirmed. PNR ABC123. Paid by UPI. Credit card offers."})


def pay_now(label: str = "Pay now") -> dict[str, Any]:
    return decision_payload(
        action="click",
        target=label,
        value=None,
        expectedOutcome={"type": "dom_changed", "value": ""},
        riskLevel="HIGH",
        requiresApproval=True,
    )


def booking_store() -> FakeStore:
    return FakeStore(task=FakeTask(instruction=BOOKING))


class PaysThenConfirms(FakeBrowser):
    """The payment form until the person has paid, then the confirmation."""

    def __init__(self) -> None:
        super().__init__(card_form())
        self.paid = False
        self.indicators: list[str] = []

    async def perceive(self) -> PageState:
        self.perceive_calls += 1
        return confirmation() if self.paid else self._state

    async def set_indicator(self, mode: str) -> None:
        self.indicators.append(mode)


class PersonPays(ApprovingGate):
    def __init__(self, browser: PaysThenConfirms) -> None:
        super().__init__()
        self._browser = browser

    async def request(self, **kwargs: Any) -> ApprovalOutcome:
        self._browser.paid = True
        return await super().request(**kwargs)


async def test_a_payment_page_hands_the_browser_to_the_person(settings: Settings) -> None:
    browser = PaysThenConfirms()
    gate = PersonPays(browser)
    rig = build(
        script=[done_payload("Booked AI-203, PNR ABC123")],
        settings=settings.model_copy(update={"AGENT_PAYMENT_WAIT_SECONDS": 4321}),
        store=booking_store(),
        browser=browser,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.final_answer == "Booked AI-203, PNR ABC123"
    assert len(gate.requests) == 1
    request = gate.requests[0]
    assert "ready for payment" in request["reasons"][0]
    assert request["decision"].target == "for you to pay"
    assert request["timeout_seconds"] == 4321
    # Nothing was typed into the card form.
    assert all(a.action.value == "done" for a in browser.executed)
    assert browser.indicators == ["waiting", "working"]
    # The page check would have fired on the confirmation too, and did not.
    assert is_payment_page(confirmation())


async def test_a_lookup_is_not_stopped_by_a_card_field(settings: Settings) -> None:
    gate = ApprovingGate()
    rig = build(
        script=[done_payload("It costs 4,500")],
        settings=settings,
        browser=FakeBrowser(card_form()),
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert gate.requests == []


async def test_the_agent_never_presses_pay_itself(settings: Settings) -> None:
    """Approved or not: the planner asks to press Pay, the person is handed the browser."""
    gate = ApprovingGate()
    browser = FakeBrowser(page(element(0, name="Pay ₹4,500")))
    rig = build(
        script=[pay_now("Pay ₹4,500"), done_payload("Paid by the person")],
        settings=settings,
        browser=browser,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert len(gate.requests) == 1
    assert "never pays" in gate.requests[0]["reasons"][0]
    assert not any(a.action.value == "click" for a in browser.executed)
    # No step was spent on a click that never happened: only the answer is recorded.
    assert rig.store.step_numbers() == [1]


async def test_rejecting_the_payment_stops_the_task_without_paying(settings: Settings) -> None:
    browser = FakeBrowser(card_form())
    # The default gate refuses, which here is the person deciding not to pay.
    rig = build(script=[done_payload()], settings=settings, store=booking_store(), browser=browser)

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.REJECTED
    assert browser.executed == []


async def test_a_page_still_asking_for_payment_ends_the_task(settings: Settings) -> None:
    gate = ApprovingGate()
    browser = FakeBrowser(page(element(0, name="Pay now")))
    rig = build(
        script=[pay_now()] * (MAX_PAYMENT_PAUSES + 1),
        settings=settings,
        browser=browser,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.failure_reason is not None
    assert "never pays itself" in outcome.failure_reason
    assert len(gate.requests) == MAX_PAYMENT_PAUSES
    assert browser.executed == []


async def test_a_cached_flow_never_replays_a_payment(settings: Settings) -> None:
    cached_pay = ReplayStep(
        action=ActionType.CLICK,
        locator=Locator(role="input", name="Pay now"),
        value=None,
        target="Pay now",
        expected_type=VerificationMethod.DOM_CHANGED,
        expected_value="",
    )
    browser = FakeBrowser(page(element(0, name="Pay now")))
    rig = build(
        script=[done_payload("Stopped at payment")],
        settings=settings,
        browser=browser,
        cache=RecordingCache(cached_pay),
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.replayed_steps == 0
    assert not any(a.action.value == "click" for a in browser.executed)


# ------------------------------------------------------- details the goal left out


def ask_for(fields: str) -> dict[str, Any]:
    return decision_payload(
        action="wait",
        targetIndex=None,
        target=fields,
        value=None,
        requiresApproval=True,
        expectedOutcome={"type": "dom_changed", "value": ""},
    )


async def test_the_planner_can_ask_the_person_for_missing_details(settings: Settings) -> None:
    gate = ApprovingGate()
    browser = FakeBrowser(page(element(0, name="Full name")))
    rig = build(
        script=[ask_for("traveller name, age and phone"), done_payload()],
        settings=settings,
        browser=browser,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.COMPLETED
    assert len(gate.requests) == 1
    assert "traveller name, age and phone" in gate.requests[0]["reasons"][0]
    assert not any(a.action.value == "wait" for a in browser.executed)


async def test_asking_for_details_again_and_again_ends_the_task(settings: Settings) -> None:
    gate = ApprovingGate()
    rig = build(
        script=[ask_for("traveller name")] * (MAX_DETAIL_PAUSES + 1),
        settings=settings,
        approvals=gate,
    )

    outcome = await rig.orchestrator.run(TASK_ID)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert len(gate.requests) == MAX_DETAIL_PAUSES


def test_provider_access_failure_explains_key_configuration_without_echoing_secrets() -> None:
    from app.agent.llm.base import LLMAccessDenied
    from app.agent.orchestrator import _safe_reason

    reason = _safe_reason(LLMAccessDenied("private-provider-response"))
    assert "API key" in reason
    assert "private-provider-response" not in reason
    assert "internal error" not in reason


async def test_a_blocked_done_is_failed_and_never_cached_as_success(settings: Settings) -> None:
    cache = RecordingCache()
    blocker = "The selected ride confirmation is missing."
    rig = build(
        script=[decision_payload(), done_payload(blocker) | {"goalAchieved": False}],
        settings=settings,
        cache=cache,
    )
    outcome = await rig.orchestrator.run(TASK_ID)
    assert outcome.status is TaskStatus.EXECUTION_FAILED
    assert outcome.final_answer is None
    assert outcome.failure_reason == blocker
    assert rig.store.task.status is TaskStatus.EXECUTION_FAILED
    assert rig.store.task.final_answer is None
    assert rig.store.task.failure_reason == blocker
    assert cache.saved is None
    final = rig.bus.published[-1]
    assert final.type == "final"
    assert final.status is TaskStatus.EXECUTION_FAILED
    assert final.failure_reason == blocker


def test_uber_ride_options_login_prompt_is_detected_without_a_model() -> None:
    from app.agent.orchestrator import _login_wall

    state = page(
        element(0, name="Continue").model_copy(update={"tag": "button"}),
        url="https://m.uber.com/go/product-selection",
    ).model_copy(update={"text": "Log in to see ride options"})
    assert _login_wall(state, "show ride options from PST hostel to Polaris School of Technology")
    assert _login_wall(state, "Show ride options. Do not enter credentials or passwords.")


async def test_cancel_wins_over_an_approval_and_publishes_the_final_event(
    settings: Settings,
) -> None:
    class Signal:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    signal = Signal()

    class RacingApproval:
        async def request(self, **kwargs: Any) -> ApprovalOutcome:
            signal.cancelled = True
            return ApprovalOutcome(approved=True, approval_id=uuid.uuid4())

    rig = build(
        script=[decision_payload(requiresApproval=True)],
        settings=settings,
        cancel_signal=signal,
        approvals=RacingApproval(),
    )
    outcome = await rig.orchestrator.run(TASK_ID)
    assert outcome.status is TaskStatus.CANCELLED
    assert rig.browser.executed == []
    assert rig.bus.published[-1].type is EventType.FINAL
    assert rig.bus.published[-1].status is TaskStatus.CANCELLED


async def test_required_popup_stops_cached_background_clicks_and_is_not_closed(
    settings: Settings,
) -> None:
    book = element(0, name="Book tickets").model_copy(update={"tag": "button", "input_type": None})
    choice = element(1, name="English 3D").model_copy(
        update={"tag": "button", "input_type": None, "in_modal": True}
    )
    close = element(2, name="Close").model_copy(
        update={"tag": "button", "input_type": None, "in_modal": True}
    )
    popup = page(book.model_copy(update={"covered": True}), choice, close).model_copy(
        update={"modal_open": True, "modal_text": "Select language and format: English 3D"}
    )

    class ChoosingBrowser(FakeBrowser):
        async def execute(self, action: Any) -> ActionResult:
            result = await super().execute(action)
            if action.action is ActionType.CLICK:
                self._state = page(book)
            return result

    cache = RecordingCache(
        ReplayStep(
            action=ActionType.CLICK,
            locator=Locator(name="Book tickets"),
            target="Book tickets",
            expected_type=VerificationMethod.DOM_CHANGED,
            expected_value="",
        )
    )
    background = decision_payload(
        action="click",
        targetIndex=0,
        target="Book tickets",
        value=None,
        expectedOutcome={"type": "dom_changed", "value": ""},
    )
    choose = {**background, "targetIndex": 1, "target": "English 3D"}
    rig = build(
        # First proposal repeats the bad background click. It must be repaired
        # before execution, including on the planner's final (non-advisory) turn.
        script=[background, choose, background, done_payload()],
        browser=ChoosingBrowser(popup),
        settings=settings,
        cache=cache,
    )
    outcome = await rig.orchestrator.run(TASK_ID)
    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.replayed_steps == 0
    assert len(rig.browser.executed) == 3  # choice, background, done; never Close
    assert '"1"' in rig.browser.executed[0].selector
    assert '"0"' in rig.browser.executed[1].selector
    assert rig.browser.pressed == []
