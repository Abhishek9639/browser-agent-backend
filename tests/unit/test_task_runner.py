"""The task runner: concurrency, cancellation and crash recovery.

No browser and no database. The runner's own logic is scheduling and
bookkeeping, and both are easier to pin down against fakes than against a real
Chromium that takes a second to start.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.agent.orchestrator import RunOutcome
from app.core.config import Settings
from app.domain.enums import TaskStatus
from app.services.task_runner import TaskRunner, interrupted_reason, shutdown_reason


@dataclass
class FakeTask:
    created_by: uuid.UUID = field(default_factory=uuid.uuid4)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    instruction: str = "find a flight"
    status: TaskStatus = TaskStatus.SUBMITTED
    start_url: str | None = None
    failure_reason: str | None = None
    finished_at: Any = None


@dataclass
class FakeStore:
    tasks: dict[uuid.UUID, FakeTask] = field(default_factory=dict)

    def add(self, **kwargs: Any) -> FakeTask:
        task = FakeTask(**kwargs)
        self.tasks[task.id] = task
        return task

    def active(self) -> list[FakeTask]:
        return [t for t in self.tasks.values() if not t.status.is_terminal]


class FakeTasks:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_or_404(self, task_id: uuid.UUID) -> FakeTask:
        return self._store.tasks[task_id]

    async def list_active(self) -> list[FakeTask]:
        return self._store.active()


class FakeApprovals:
    async def get_pending_for_task(self, task_id: uuid.UUID) -> None:
        return None


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.tasks = FakeTasks(store)
        self.approvals = FakeApprovals()


def unit_of_work_for(store: FakeStore) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield FakeUnitOfWork(store)

    return factory


class FakeDriver:
    """A driver that launches nothing and closes cleanly."""

    def __init__(self) -> None:
        self.launched = False
        self.closed = False

    async def launch(self) -> None:
        self.launched = True

    async def close(self) -> None:
        self.closed = True


class FakeOrchestrator:
    """Stands in for a run. Blocks until released, so slots can be observed."""

    def __init__(self, cancel_signal: Any, gate: asyncio.Event, log: list[str]) -> None:
        self._cancel = cancel_signal
        self._gate = gate
        self._log = log

    async def run(self, task_id: uuid.UUID) -> RunOutcome:
        self._log.append(f"start:{task_id}")
        waiter = asyncio.ensure_future(self._gate.wait())
        try:
            while not waiter.done():
                if self._cancel.is_cancelled():
                    self._log.append(f"cancelled:{task_id}")
                    return RunOutcome(status=TaskStatus.CANCELLED)
                await asyncio.wait({waiter}, timeout=0.01)
        finally:
            waiter.cancel()
        self._log.append(f"done:{task_id}")
        return RunOutcome(status=TaskStatus.COMPLETED)


@dataclass
class Rig:
    runner: TaskRunner
    store: FakeStore
    gate: asyncio.Event
    log: list[str]

    def release(self) -> None:
        self.gate.set()


@pytest.fixture
async def rig(settings: Settings) -> AsyncIterator[Rig]:
    store = FakeStore()
    gate = asyncio.Event()
    log: list[str] = []

    runner = TaskRunner(
        settings.model_copy(update={"AGENT_MAX_CONCURRENT_TASKS": 2}),
        database=None,  # type: ignore[arg-type]
        unit_of_work=unit_of_work_for(store),
        driver_factory=FakeDriver,  # type: ignore[arg-type]
        orchestrator_factory=lambda **kwargs: FakeOrchestrator(kwargs["cancel_signal"], gate, log),
    )
    # The browser is the one collaborator a fake cannot stand in for cheaply,
    # and nothing below needs a page, so the runner is marked started without
    # launching one.
    runner._started = True  # type: ignore[attr-defined]

    yield Rig(runner=runner, store=store, gate=gate, log=log)

    # No test may leave a task running. A fake orchestrator that nobody
    # released would otherwise hang the whole suite rather than fail one test.
    gate.set()
    for task_id in list(runner._tasks):
        runner._signal(task_id)
    pending = list(runner._tasks.values())
    if pending:
        with suppress(Exception):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)


async def settle() -> None:
    """Let scheduled tasks start and park on the gate."""
    for _ in range(10):
        await asyncio.sleep(0.005)


async def finish(rig: "Rig") -> None:
    """Release the gate and wait for every scheduled task to finish."""
    rig.release()
    pending = list(rig.runner._tasks.values())
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)


# ------------------------------------------------------------ concurrency


async def test_the_concurrency_limit_holds_extra_tasks_back(rig: Rig) -> None:
    ids = [rig.store.add().id for _ in range(5)]

    for task_id in ids:
        rig.runner.submit(task_id)
    await settle()

    started = [line for line in rig.log if line.startswith("start:")]
    assert len(started) == 2, "the semaphore should admit exactly two"
    assert rig.runner.running == 5, "the other three are scheduled, not dropped"


async def test_a_queued_task_stays_submitted(rig: Rig) -> None:
    ids = [rig.store.add().id for _ in range(5)]

    for task_id in ids:
        rig.runner.submit(task_id)
    await settle()

    # The orchestrator writes PLANNING as its first act, so a task that has not
    # started is still SUBMITTED. That is what the history screen shows.
    waiting = [i for i in ids if f"start:{i}" not in rig.log]
    assert len(waiting) == 3
    assert all(rig.store.tasks[i].status is TaskStatus.SUBMITTED for i in waiting)


async def test_a_freed_slot_admits_the_next_task(rig: Rig) -> None:
    ids = [rig.store.add().id for _ in range(4)]
    for task_id in ids:
        rig.runner.submit(task_id)
    await settle()
    assert len([line for line in rig.log if line.startswith("start:")]) == 2

    await finish(rig)

    assert len([line for line in rig.log if line.startswith("start:")]) == 4
    assert rig.runner.running == 0


async def test_submitting_the_same_task_twice_schedules_it_once(rig: Rig) -> None:
    task_id = rig.store.add().id

    rig.runner.submit(task_id)
    rig.runner.submit(task_id)
    await settle()

    assert [line for line in rig.log if line == f"start:{task_id}"] == [f"start:{task_id}"]
    assert rig.runner.running == 1


async def test_submitting_before_start_is_refused(settings: Settings) -> None:
    runner = TaskRunner(settings, database=None)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        runner.submit(uuid.uuid4())


# ----------------------------------------------------------- cancellation


async def test_cancel_reaches_a_running_task(rig: Rig) -> None:
    task_id = rig.store.add().id
    rig.runner.submit(task_id)
    await settle()

    assert await rig.runner.cancel(task_id) is True
    await asyncio.wait_for(
        asyncio.gather(*rig.runner._tasks.values(), return_exceptions=True), timeout=10
    )

    assert f"cancelled:{task_id}" in rig.log
    assert f"done:{task_id}" not in rig.log


async def test_cancelling_a_task_this_process_is_not_running_is_not_an_error(
    rig: Rig,
) -> None:
    # Another worker may hold it, or it may have finished a moment ago. The row
    # is already CANCELLED either way; the signal is only an optimisation.
    assert await rig.runner.cancel(uuid.uuid4()) is False


async def test_a_task_cancelled_while_queued_never_starts(rig: Rig) -> None:
    ids = [rig.store.add().id for _ in range(4)]
    for task_id in ids:
        rig.runner.submit(task_id)
    await settle()

    queued = next(i for i in ids if f"start:{i}" not in rig.log)
    await rig.runner.cancel(queued)
    await finish(rig)

    assert f"start:{queued}" not in rig.log


async def test_a_finished_task_is_forgotten(rig: Rig) -> None:
    task_id = rig.store.add().id
    rig.runner.submit(task_id)
    await settle()
    await finish(rig)

    assert rig.runner.is_running(task_id) is False
    assert rig.runner.running == 0


# -------------------------------------------------------------- lifecycle


async def test_shutdown_marks_running_tasks_cancelled(rig: Rig) -> None:
    ids = [rig.store.add().id for _ in range(3)]
    for task_id in ids:
        rig.runner.submit(task_id)
    await settle()

    await asyncio.wait_for(rig.runner.stop(), timeout=10)

    for task_id in ids:
        task = rig.store.tasks[task_id]
        assert task.status is TaskStatus.CANCELLED
        assert task.failure_reason == shutdown_reason
        assert task.finished_at is not None
    assert rig.runner.running == 0


async def test_shutdown_leaves_an_already_finished_task_alone(rig: Rig) -> None:
    task_id = rig.store.add().id
    rig.runner.submit(task_id)
    await settle()
    # The loop settled itself before shutdown reached it.
    rig.store.tasks[task_id].status = TaskStatus.COMPLETED
    rig.store.tasks[task_id].failure_reason = None

    await asyncio.wait_for(rig.runner.stop(), timeout=10)

    assert rig.store.tasks[task_id].status is TaskStatus.COMPLETED
    assert rig.store.tasks[task_id].failure_reason is None


async def test_stopping_twice_is_harmless(rig: Rig) -> None:
    await rig.runner.stop()
    await rig.runner.stop()

    assert rig.runner.running == 0


async def test_startup_fails_tasks_abandoned_by_a_crash(rig: Rig) -> None:
    left_acting = rig.store.add(status=TaskStatus.ACTING)
    left_planning = rig.store.add(status=TaskStatus.PLANNING)
    finished = rig.store.add(status=TaskStatus.COMPLETED)

    recovered = await rig.runner._recover_interrupted()

    # Nothing will ever move these again: the asyncio task that owned them died
    # with the process, so a user would watch them run forever.
    assert recovered == 2
    assert left_acting.status is TaskStatus.EXECUTION_FAILED
    assert left_acting.failure_reason == interrupted_reason
    assert left_planning.status is TaskStatus.EXECUTION_FAILED
    assert finished.status is TaskStatus.COMPLETED


async def test_recovery_walks_a_submitted_task_through_the_state_machine(
    rig: Rig,
) -> None:
    submitted = rig.store.add(status=TaskStatus.SUBMITTED)

    await rig.runner._recover_interrupted()

    # SUBMITTED cannot reach EXECUTION_FAILED directly, so recovery has to go
    # through PLANNING rather than writing the status by hand.
    assert submitted.status is TaskStatus.EXECUTION_FAILED
    assert submitted.failure_reason == interrupted_reason


async def test_a_task_parked_on_a_human_times_out_rather_than_fails(rig: Rig) -> None:
    waiting = rig.store.add(status=TaskStatus.AWAITING_APPROVAL)

    await rig.runner._recover_interrupted()

    # Nobody answered because the process that would have asked them died.
    # Decision A4 calls an unanswered approval a timeout, and it is also the
    # only terminal state the lifecycle allows from here.
    assert waiting.status is TaskStatus.TIMED_OUT
    assert waiting.failure_reason == interrupted_reason


async def test_a_driver_that_will_not_launch_fails_the_task(rig: Rig) -> None:
    class BrokenDriver(FakeDriver):
        async def launch(self) -> None:
            raise RuntimeError("chromium is not installed")

    rig.runner._driver_factory = BrokenDriver  # type: ignore[attr-defined]
    task_id = rig.store.add().id

    rig.runner.submit(task_id)
    await finish(rig)

    # The task never planned, so this only works because SUBMITTED can reach
    # EXECUTION_FAILED. Without that edge it would sit live forever.
    assert rig.store.tasks[task_id].status is TaskStatus.EXECUTION_FAILED
    assert rig.store.tasks[task_id].failure_reason == "The task could not be started."


async def test_user_limit_leaves_capacity_for_other_users(rig: Rig) -> None:
    rig.runner._settings = rig.runner._settings.model_copy(
        update={"AGENT_MAX_CONCURRENT_TASKS_PER_USER": 1}
    )
    owner = uuid.uuid4()
    first = rig.store.add(created_by=owner)
    second = rig.store.add(created_by=owner)
    other = rig.store.add()
    for task in (first, second, other):
        rig.runner.submit(task.id)
    await settle()
    assert f"start:{first.id}" in rig.log
    assert f"start:{other.id}" in rig.log
    assert f"start:{second.id}" not in rig.log
    await finish(rig)
    assert f"start:{second.id}" in rig.log
    assert rig.runner._user_slots == {}
