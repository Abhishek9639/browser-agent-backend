"""Running the agent against SkyFare, for the chaos suite and the benchmark.

Shared so the two cannot drift. A benchmark that measured something slightly
different from what the suite asserts would report numbers nobody could act
on, and the suite is the definition of what a passing run looks like.

Everything is in one process: SkyFare on a thread, a real Chromium, the real
loop with its real verifier and recovery ladder. The HTTP API is deliberately
not in the path. It is covered by the integration suite, and putting it here
would mean a chaos failure could be a routing bug, which is exactly the
ambiguity a chaos suite should not have.
"""

import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import PlaywrightDriver
from app.agent.contracts import FlowCache
from app.agent.llm.base import LLMClient
from app.agent.llm.factory import build_llm
from app.agent.llm.mock import MockLLM
from app.agent.orchestrator import Orchestrator, RunOutcome
from app.agent.planner.planner import Planner
from app.agent.planner.registry import PromptRegistry
from app.agent.recovery.runner import RecoveryRunner
from app.agent.recovery.strategies import strategies_with_grounding
from app.agent.vision.grounding import VisionGrounder, VisualGroundingService
from app.core.config import Settings
from app.domain.enums import RecoveryOutcome, StepStatus, TaskStatus
from app.models.task import Task
from app.repositories.unit_of_work import UnitOfWork
from app.services.events import InMemoryEventBus
from app.services.storage import LocalFileStorage
from demo_sites.skyfare.app import ORACLE_HEADER, ORACLE_TOKEN
from demo_sites.skyfare.app import app as skyfare_app

# Spec section 12's primary recorded demo, with a date far enough out that it
# is never today and never the form's own default.
ORIGIN = "Delhi"
DESTINATION = "Bengaluru"

CHAOS_CONFIGS: tuple[str, ...] = (
    "",
    "delay",
    "cookie",
    "popup",
    "shuffle",
    "layoutb",
    "iframe",
    "newtab",
    "cookie,popup,shuffle",
    "inject",
)


def demo_date() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def instruction(on: str) -> str:
    return f"Find the cheapest flight from {ORIGIN} to {DESTINATION} on {on} on SkyFare"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def skyfare_server() -> Iterator[str]:
    """SkyFare on a background thread, for the whole session.

    A thread rather than a subprocess so a failure surfaces as an exception
    here instead of as a silent port that never opens.
    """
    port = free_port()
    config = uvicorn.Config(skyfare_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:  # pragma: no cover - only on a badly stuck machine
        raise RuntimeError("SkyFare did not start")

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def cheapest_fare(base: str, on: str) -> dict[str, Any]:
    """Ground truth, from the endpoint the agent cannot reach."""
    response = httpx.get(
        f"{base}/api/cheapest",
        params={"from": ORIGIN, "to": DESTINATION, "date": on},
        headers={ORACLE_HEADER: ORACLE_TOKEN},
        timeout=10,
    )
    response.raise_for_status()
    return dict(response.json())


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One step, detached from the session that produced it."""

    number: int
    action: str
    target: str | None
    value: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One recovery attempt, likewise."""

    number: int
    strategy_used: str
    outcome: RecoveryOutcome
    error: str | None


@dataclass
class RunReport:
    """One run, as both the suite and the benchmark read it.

    Plain values rather than ORM rows. The benchmark closes a session per run,
    and a report holding detached instances raises the moment anything reads
    them, which is a long way from where the mistake was made.
    """

    chaos: str
    outcome: RunOutcome
    task_id: uuid.UUID | None = None
    steps: list[StepRecord] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    answer: str | None = None
    approvals: list[str] = field(default_factory=list)
    expected_price: int = 0
    wall_ms: int = 0
    final_url: str = ""
    consent_choices: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.outcome.status is TaskStatus.COMPLETED

    @property
    def correct(self) -> bool:
        """Did it report the fare the oracle says is cheapest?

        Matched on the number rather than on the sentence, because the wording
        is the planner's and the number is the answer.
        """
        if not self.completed or not self.answer:
            return False
        return _digits(f"{self.expected_price}") in _digits(self.answer)

    @property
    def recovered(self) -> bool:
        return any(a.outcome is RecoveryOutcome.SUCCEEDED for a in self.attempts)

    @property
    def episodes(self) -> list[list[AttemptRecord]]:
        """Attempts grouped by the step that needed rescuing.

        Spec section 9 wants recovery success on induced failures, which is a
        rate per failure and not per attempt. The ladder tries several rungs
        to fix one failure, so counting attempts scores a step that was
        rescued on the third rung as two failures and one success.

        Attempt numbers restart at one per step, which is where a new episode
        begins.
        """
        grouped: list[list[AttemptRecord]] = []
        for attempt in self.attempts:
            if attempt.number == 1 or not grouped:
                grouped.append([])
            grouped[-1].append(attempt)
        return grouped

    @property
    def recovered_episodes(self) -> int:
        return sum(
            1
            for episode in self.episodes
            if any(a.outcome is RecoveryOutcome.SUCCEEDED for a in episode)
        )

    @property
    def clean_failure(self) -> bool:
        """A failure a person could act on, rather than a crash."""
        return self.outcome.status is not TaskStatus.COMPLETED and bool(self.outcome.failure_reason)

    @property
    def verified_steps(self) -> int:
        return sum(1 for step in self.steps if step.verified)


def _digits(text: str) -> str:
    return "".join(character for character in text if character.isdigit())


def session_unit_of_work(session: AsyncSession) -> Any:
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


def build_client(settings: Settings, *, live: bool) -> LLMClient:
    """The mock by default, the configured provider with --live.

    Defaulting to the mock is deliberate: a suite that spends money whenever
    somebody types `make chaos` is a suite people stop running.
    """
    return build_llm(settings) if live else MockLLM()


async def run_once(
    *,
    chaos: str,
    base_url: str,
    session: AsyncSession,
    settings: Settings,
    storage_dir: Path,
    llm: LLMClient,
    task: Task,
    cache: FlowCache | None = None,
) -> RunReport:
    """One task against SkyFare with one chaos configuration.

    The cache is off unless a caller asks for it. The suite measures what the
    planner and the recovery ladder do against a misbehaving page, and a run
    that replays a stored flow is not measuring either.
    """
    on = demo_date()
    query = f"?chaos={chaos}" if chaos else ""
    settings = settings.model_copy(
        update={
            "STORAGE_DIR": str(storage_dir),
            "AGENT_ALLOWED_DOMAINS": ["127.0.0.1", "localhost"],
            "AGENT_HEADLESS": True,
        }
    )

    driver = PlaywrightDriver(settings)
    await driver.launch()
    started = time.monotonic()

    try:
        # Observe real consent clicks across navigation; proactive dismissal is
        # successful handling even when no recovery ladder was needed.
        await driver.page.add_init_script("""document.addEventListener('click', event => {
            const button = event.target.closest('[data-consent]');
            if (!button) return;
            const choices = JSON.parse(sessionStorage.getItem('__test_consent_choices') || '[]');
            choices.push(button.dataset.consent);
            sessionStorage.setItem('__test_consent_choices', JSON.stringify(choices));
        }, true);""")
        await driver.page.goto(f"{base_url}/{query}")

        browser = DriverBrowser(driver, settings)
        grounder = VisionGrounder(
            VisualGroundingService(
                llm,
                version=settings.VISION_PROMPT_VERSION,
                max_width=settings.VISION_MAX_WIDTH,
                max_image_bytes=settings.VISION_MAX_IMAGE_BYTES,
            )
        )
        orchestrator = Orchestrator(
            browser=browser,
            planner=Planner(llm, PromptRegistry(settings.PLANNER_PROMPT_VERSION)),
            storage=LocalFileStorage(settings),
            events=InMemoryEventBus(),
            unit_of_work=session_unit_of_work(session),
            settings=settings,
            cache=cache,
            grounder=grounder,
            recovery=RecoveryRunner(
                max_attempts=settings.AGENT_MAX_RETRIES_PER_STEP,
                strategies=strategies_with_grounding(grounder),
            ),
        )

        outcome = await orchestrator.run(task.id)
        final_url = driver.page.url
        consent_choices = await driver.page.evaluate(
            "JSON.parse(sessionStorage.getItem('__test_consent_choices') || '[]')"
        )
    finally:
        await driver.close()

    wall_ms = int((time.monotonic() - started) * 1000)
    await session.refresh(task)

    uow = UnitOfWork(session)
    steps = [
        StepRecord(
            number=step.step_number,
            action=step.action.value,
            target=step.target,
            value=step.value_redacted,
            verified=step.status is StepStatus.VERIFIED,
        )
        for step in await uow.task_steps.list_for_task(task.id)
    ]
    attempts = [
        AttemptRecord(
            number=attempt.attempt_number,
            strategy_used=attempt.strategy_used,
            outcome=attempt.outcome,
            error=attempt.error,
        )
        for attempt in await uow.recovery_attempts.list_for_task(task.id)
    ]
    approvals = [
        approval.approval_status.value for approval in await uow.approvals.list_for_task(task.id)
    ]
    answer = (task.final_answer or {}).get("text") if task.final_answer else None

    return RunReport(
        chaos=chaos or "none",
        outcome=outcome,
        task_id=task.id,
        steps=steps,
        attempts=attempts,
        answer=answer,
        approvals=approvals,
        expected_price=int(cheapest_fare(base_url, on)["price"]),
        wall_ms=wall_ms,
        final_url=final_url,
        consent_choices=consent_choices,
    )


async def make_task(session: AsyncSession, *, email: str | None = None) -> Task:
    """A user and a task row for one run."""
    from app.models.user import User

    user = User(
        name="Chaos",
        email=email or f"chaos-{uuid.uuid4().hex[:10]}@example.test",
        password_hash="argon2-digest",
    )
    session.add(user)
    await session.flush()

    task = Task(instruction=instruction(demo_date()), created_by=user.id)
    session.add(task)
    await session.flush()
    await session.refresh(task)
    await session.commit()
    return task
