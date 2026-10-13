"""The loop end to end: real Postgres, real Chromium, scripted model.

Everything is real except the decisions. The unit tests prove the loop's logic
against fakes; this proves the parts actually fit together, which is where the
interesting failures live: a column that will not take the value, a selector
that resolves in a test double but not in a browser, a screenshot that never
reaches the disk.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import PlaywrightDriver
from app.agent.budget import TaskBudget
from app.agent.contracts import ApprovalOutcome, GroundingOutcome
from app.agent.llm.mock import MockLLM
from app.agent.orchestrator import Orchestrator
from app.agent.perception.dom_extractor import MASK
from app.agent.planner.planner import Planner
from app.core.config import Settings
from app.domain.enums import (
    ActionType,
    RecoveryOutcome,
    StepStatus,
    TaskStatus,
    VerificationMethod,
)
from app.models.task import LLMDecision, RecoveryAttempt, TaskStep, Verification
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import EventType, InMemoryEventBus
from app.services.storage import LocalFileStorage
from tests.integration.conftest_helpers import make_task, make_user

pytestmark = pytest.mark.browser


class Approve:
    """An approver that always says yes, for tests about something else."""

    async def request(self, **kwargs: Any) -> ApprovalOutcome:
        return ApprovalOutcome(approved=True, approval_id=uuid.uuid4())


def session_unit_of_work(session: AsyncSession) -> Any:
    """A factory bound to the test's transaction.

    The session commits into a savepoint, so the orchestrator behaves exactly
    as it does in production, one transaction per step, while the test still
    rolls everything back at the end.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[UnitOfWork]:
        try:
            yield UnitOfWork(session)
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()

    return factory


def index_of(state: Any, **match: str) -> int:
    """Find an element by what it looks like, not by position.

    Hardcoding an index would tie the test to the fixture's DOM order, so a
    line added to the page would break the test for no reason.
    """
    for element in state.elements:
        if all(getattr(element, key, None) == value for key, value in match.items()):
            return element.index
    raise AssertionError(f"no element matching {match} in {state.summary_text}")


@pytest.fixture
async def rig(
    db_session: AsyncSession,
    driver: Any,
    html_server: str,
    browser_settings: Settings,
    tmp_path: Path,
) -> AsyncIterator[dict[str, Any]]:
    settings = browser_settings.model_copy(update={"STORAGE_DIR": str(tmp_path)})
    await driver.page.goto(f"{html_server}/simple_form.html")

    browser = DriverBrowser(driver, settings)
    bus = InMemoryEventBus()
    user = await make_user(db_session, f"owner-{uuid.uuid4().hex[:8]}@example.test")
    task = await make_task(
        db_session,
        user,
        instruction="Fill the form, including the password, and search for a flight from Delhi",
    )
    await db_session.commit()

    yield {
        "settings": settings,
        "browser": browser,
        "bus": bus,
        "task": task,
        "session": db_session,
        "storage": LocalFileStorage(settings),
        "state": await browser.perceive(),
    }


def build(rig: dict[str, Any], script: list[dict[str, Any]], **kwargs: Any) -> Orchestrator:
    return Orchestrator(
        browser=rig["browser"],
        planner=Planner(MockLLM(script=script)),
        storage=rig["storage"],
        events=rig["bus"],
        unit_of_work=session_unit_of_work(rig["session"]),
        settings=rig["settings"],
        **kwargs,
    )


async def fetch_steps(session: AsyncSession, task_id: uuid.UUID) -> list[TaskStep]:
    rows = await session.execute(
        select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_number)
    )
    return list(rows.scalars())


async def test_a_scripted_run_fills_the_form_and_completes(rig: dict[str, Any]) -> None:
    session, task = rig["session"], rig["task"]
    origin = index_of(rig["state"], placeholder="From")
    submit = index_of(rig["state"], name="Search flights")

    orchestrator = build(
        rig,
        [
            {
                "action": "type",
                "targetIndex": origin,
                "target": "From",
                "value": "Delhi",
                "reasoning": "Fill in the origin.",
                "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
                "confidence": 0.9,
            },
            {
                "action": "click",
                "targetIndex": submit,
                "target": "Search flights",
                "reasoning": "Run the search.",
                "expectedOutcome": {"type": "text_present", "value": "Searched"},
                "confidence": 0.9,
            },
            {
                "action": "done",
                "reasoning": "The search ran.",
                "finalAnswer": "Searched: Delhi",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.95,
            },
        ],
    )

    outcome = await orchestrator.run(task.id)

    assert outcome.status is TaskStatus.COMPLETED
    assert outcome.final_answer == "Searched: Delhi"
    assert outcome.steps == 3

    await session.refresh(task)
    assert task.status is TaskStatus.COMPLETED
    assert task.final_answer == {"text": "Searched: Delhi"}
    assert task.finished_at is not None

    # The form really was filled in and really was submitted.
    assert await rig["browser"]._driver.page.input_value("#from") == "Delhi"
    assert "Searched: Delhi" in await rig["browser"]._driver.page.inner_text("#result")


async def test_the_database_holds_a_full_record_of_each_step(rig: dict[str, Any]) -> None:
    session, task = rig["session"], rig["task"]
    origin = index_of(rig["state"], placeholder="From")

    orchestrator = build(
        rig,
        [
            {
                "action": "type",
                "targetIndex": origin,
                "target": "From",
                "value": "Delhi",
                "reasoning": "Fill in the origin.",
                "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
                "confidence": 0.8,
            },
            {
                "action": "done",
                "reasoning": "Done.",
                "finalAnswer": "ok",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
            },
        ],
    )

    await orchestrator.run(task.id)
    steps = await fetch_steps(session, task.id)

    assert [step.step_number for step in steps] == [1, 2]
    first = steps[0]
    assert first.action is ActionType.TYPE
    assert first.target == "From"
    assert first.target_index == origin
    assert first.value_redacted == "Delhi"
    assert first.status is StepStatus.VERIFIED
    assert first.selector_used == f'[data-agent-idx="{origin}"]'
    assert first.bbox is not None and first.bbox["width"] > 0
    assert first.tab_id

    decision = (
        await session.execute(select(LLMDecision).where(LLMDecision.step_id == first.id))
    ).scalar_one()
    assert decision.provider == "mock"
    assert decision.prompt_version
    assert decision.confidence == pytest.approx(0.8)
    assert decision.used_vision is False

    verification = (
        await session.execute(select(Verification).where(Verification.step_id == first.id))
    ).scalar_one()
    assert verification.verified is True
    assert verification.actual_outcome is not None
    # The real verifier re-read the field rather than taking the model's word.
    assert verification.verification_method is VerificationMethod.VALUE_EQUALS
    assert verification.actual_outcome["check"] == "value_equals"
    assert verification.actual_outcome["value"] == "Delhi"
    assert verification.expected_outcome["value"] == "Delhi"


async def test_the_screenshot_on_disk_matches_the_reference_in_the_row(
    rig: dict[str, Any],
) -> None:
    session, task = rig["session"], rig["task"]

    orchestrator = build(
        rig,
        [
            {
                "action": "done",
                "reasoning": "Nothing to do.",
                "finalAnswer": "ok",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
            }
        ],
    )

    await orchestrator.run(task.id)
    step = (await fetch_steps(session, task.id))[0]

    assert step.screenshot_ref is not None
    # A row pointing at a missing file is what the write order exists to avoid.
    assert await rig["storage"].exists(step.screenshot_ref)
    image = await rig["storage"].load(step.screenshot_ref)
    assert image.startswith(b"\xff\xd8\xff")


async def test_the_events_a_client_would_see(rig: dict[str, Any]) -> None:
    task, bus = rig["task"], rig["bus"]
    subscription = bus.subscribe(task.id)

    orchestrator = build(
        rig,
        [
            {
                "action": "done",
                "reasoning": "Nothing to do.",
                "finalAnswer": "all done",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
            }
        ],
    )
    await orchestrator.run(task.id)

    received = []
    while subscription.pending:
        received.append(await subscription.__anext__())

    assert [event.type.value for event in received] == [
        "status",
        "status",
        "status",
        "step",
        "status",
        "final",
    ]
    assert [event.seq for event in received] == [1, 2, 3, 4, 5, 6]

    step = next(event for event in received if event.type is EventType.STEP)
    assert step.screenshot is not None and len(step.screenshot) > 100
    assert step.viewport is not None and step.viewport.width > 0
    assert step.url.endswith("/simple_form.html")

    final = received[-1]
    assert final.status is TaskStatus.COMPLETED
    assert final.final_answer == "all done"
    assert final.stats.steps == 1


async def test_a_password_typed_into_a_real_page_is_never_stored(
    rig: dict[str, Any],
) -> None:
    session, task = rig["session"], rig["task"]
    secret = "correct-horse-battery-staple"
    password = index_of(rig["state"], input_type="password")

    orchestrator = build(
        rig,
        [
            {
                "action": "type",
                "targetIndex": password,
                "target": "Password",
                "value": secret,
                "reasoning": "Fill in the password.",
                "expectedOutcome": {"type": "value_equals", "value": "set"},
                "confidence": 0.9,
            },
            {
                "action": "done",
                "reasoning": "Done.",
                "finalAnswer": "ok",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
            },
        ],
        # Typing into a password field is high risk, so this needs a human to
        # say yes before it can get as far as writing a row.
        approvals=Approve(),
    )

    await orchestrator.run(task.id)
    step = (await fetch_steps(session, task.id))[0]
    decision = (
        await session.execute(select(LLMDecision).where(LLMDecision.step_id == step.id))
    ).scalar_one()

    # It was really typed into the real field, and it is nowhere in the row.
    assert await rig["browser"]._driver.page.input_value("#password") == secret
    assert step.value_redacted == MASK
    assert decision.raw_output is not None
    assert decision.raw_output["value"] == MASK
    assert secret not in str(step.__dict__) + str(decision.raw_output)


async def test_running_out_of_steps_leaves_a_consistent_record(
    rig: dict[str, Any],
) -> None:
    session, task = rig["session"], rig["task"]
    origin = index_of(rig["state"], placeholder="From")
    typing = {
        "action": "type",
        "targetIndex": origin,
        "target": "From",
        "value": "Delhi",
        "reasoning": "Fill in the origin.",
        "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
        "confidence": 0.7,
    }

    orchestrator = build(
        rig,
        [typing, typing, typing],
        budget=TaskBudget(max_steps=2, max_seconds=120),
    )

    outcome = await orchestrator.run(task.id)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    await session.refresh(task)
    assert task.status is TaskStatus.EXECUTION_FAILED
    assert task.failure_reason == "The task ran out of steps before it finished."
    # Two steps ran, two steps recorded. The budget stops the loop, it does not
    # discard work already done.
    assert len(await fetch_steps(session, task.id)) == 2


async def test_a_high_risk_action_never_touches_the_page(rig: dict[str, Any]) -> None:
    session, task = rig["session"], rig["task"]
    origin = index_of(rig["state"], placeholder="From")

    orchestrator = build(
        rig,
        [
            {
                "action": "type",
                "targetIndex": origin,
                "target": "From",
                "value": "Delhi",
                "reasoning": "Fill in the origin.",
                "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
                "riskLevel": "HIGH",
                "confidence": 0.9,
            }
        ],
    )

    outcome = await orchestrator.run(task.id)

    assert outcome.status is TaskStatus.REJECTED
    # The default gate refuses, and refusing has to mean the browser was never
    # asked: an approval that arrives after the click is not an approval.
    assert await rig["browser"]._driver.page.input_value("#from") == ""
    assert await fetch_steps(session, task.id) == []


async def test_an_approved_high_risk_action_goes_through(rig: dict[str, Any]) -> None:
    task = rig["task"]
    origin = index_of(rig["state"], placeholder="From")

    orchestrator = build(
        rig,
        [
            {
                "action": "type",
                "targetIndex": origin,
                "target": "From",
                "value": "Delhi",
                "reasoning": "Fill in the origin.",
                "expectedOutcome": {"type": "value_equals", "value": "Delhi"},
                "riskLevel": "HIGH",
                "confidence": 0.9,
            },
            {
                "action": "done",
                "reasoning": "Done.",
                "finalAnswer": "ok",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
            },
        ],
        approvals=Approve(),
    )

    outcome = await orchestrator.run(task.id)

    assert outcome.status is TaskStatus.COMPLETED
    assert await rig["browser"]._driver.page.input_value("#from") == "Delhi"


async def test_a_page_that_changes_under_the_agent_fails_the_step(
    rig: dict[str, Any], driver: PlaywrightDriver
) -> None:
    """TEST-003 and TEST-005, in the form they actually happen.

    The element was there when the page was read and is gone by the time the
    click lands. Emptying the page before the run would only prove the planner
    rejects an empty page; the gap worth testing is the one between perceiving
    and acting.

    TEST-005 is the second half of the same run: the verification fails, the
    ladder is tried, and every attempt is written to `recovery_attempts`.
    """
    session, task = rig["session"], rig["task"]
    origin = index_of(rig["state"], placeholder="From")

    class ClearsThePage:
        """Grounding runs after perception and before the action, which makes
        it the one seam where the page can be pulled out from under the loop."""

        async def resolve(
            self, decision: Any, state: Any, screenshot: bytes, reason: str
        ) -> GroundingOutcome:
            await driver.page.evaluate("() => document.body.replaceChildren()")
            return GroundingOutcome(decision=decision, used_vision=False, reason=reason)

    orchestrator = build(
        rig,
        [
            {
                "action": "click",
                "targetIndex": origin,
                "target": "From",
                "reasoning": "Click the origin field.",
                "expectedOutcome": {"type": "dom_changed", "value": ""},
                "confidence": 0.9,
                # Grounding only runs when a trigger fires, and this test needs
                # it to, because that is the seam where the page is pulled out
                # from under the loop.
                "needsVision": True,
            }
        ],
        grounder=ClearsThePage(),
    )

    outcome = await orchestrator.run(task.id)

    assert outcome.status is TaskStatus.EXECUTION_FAILED
    # TEST-003: the ladder ran, and the reason says what was tried rather
    # than only that the step failed.
    assert outcome.failure_reason.startswith("Step 1: Could not click 'From': tried ")
    assert "re-reading the page" in outcome.failure_reason

    steps = await fetch_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].status is StepStatus.FAILED

    attempts = (
        (
            await session.execute(
                select(RecoveryAttempt)
                .where(RecoveryAttempt.step_id == steps[0].id)
                .order_by(RecoveryAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )
    assert attempts, "recovery was not recorded"
    assert [a.attempt_number for a in attempts] == list(range(1, len(attempts) + 1))
    assert all(a.outcome is not RecoveryOutcome.SUCCEEDED for a in attempts)

    verification = (
        await session.execute(select(Verification).where(Verification.step_id == steps[0].id))
    ).scalar_one()
    # Nothing ran, so nothing was checked. A stub verifier that passes
    # everything must not be able to call this a success.
    assert verification.verified is False
    assert verification.actual_outcome["reason"] == "The action did not run."
    assert verification.actual_outcome["errorCode"] is not None
