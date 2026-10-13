"""Runs submitted tasks in the background (spec section 4.2).

One runner per process, started and stopped by the application lifespan. It
owns three things: the Playwright process and the Chromium it launches, the
concurrency limit, and the cancellation flags.

The browser is shared and the context is not. Launching Chromium costs a second
or more, so paying that per task would dominate a short run; a context is cheap
and is what actually isolates one task's cookies and storage from another's.

Nothing here is the queue. A task waiting for a slot is an asyncio task parked
on a semaphore, which is fine for one process and is not a work queue: it does
not survive a restart, and another worker cannot pick it up. Those are the
properties a real queue buys, and the point at which this should become one.
"""

import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Final

from playwright.async_api import Browser, Playwright, async_playwright

from app.agent.browser.agent_browser import DriverBrowser
from app.agent.browser.driver import PlaywrightDriver
from app.agent.cache.store import DatabaseFlowCache
from app.agent.llm.factory import build_llm
from app.agent.orchestrator import Orchestrator, UnitOfWorkFactory, database_unit_of_work
from app.agent.planner.planner import Planner
from app.agent.planner.registry import PromptRegistry
from app.agent.recovery.runner import RecoveryRunner
from app.agent.recovery.strategies import strategies_with_grounding
from app.agent.safety.gate import DatabaseApprovalGate
from app.agent.vision.grounding import VisionGrounder, VisualGroundingService
from app.core import metrics
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.domain.enums import TaskStatus
from app.domain.state_machine import transition
from app.services.approvals import ApprovalRegistry, expire_pending
from app.services.events import EventBus, InMemoryEventBus
from app.services.storage import LocalFileStorage, ScreenshotStorage

__all__ = ["TaskRunner", "interrupted_reason", "shutdown_reason"]

logger = get_logger(__name__)

# Written to `tasks.failure_reason`, and read by a person, so they say what
# happened rather than which code path produced them.
interrupted_reason: Final = "interrupted by restart"
shutdown_reason: Final = "server shutdown"


class _Cancellation:
    """The flag one run watches.

    A plain object rather than an `asyncio.Event` because the orchestrator only
    ever reads it, and giving it something awaitable would invite a future
    version to block the loop on it.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


def _recovery_status(current: TaskStatus) -> TaskStatus:
    """What an abandoned task should become.

    Almost always EXECUTION_FAILED. A task parked on a human is the exception:
    nobody answered, because the process that would have asked them died, and
    decision A4 already says an unanswered approval lands in TIMED_OUT. It is
    also the only terminal state the lifecycle allows from there, which is the
    table agreeing rather than a coincidence.
    """
    if current is TaskStatus.AWAITING_APPROVAL:
        return TaskStatus.TIMED_OUT
    return TaskStatus.EXECUTION_FAILED


class TaskRunner:
    """Schedules `Orchestrator.run` for submitted tasks."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        events: EventBus | None = None,
        approvals: ApprovalRegistry | None = None,
        storage: ScreenshotStorage | None = None,
        unit_of_work: UnitOfWorkFactory | None = None,
        driver_factory: Callable[[], PlaywrightDriver] | None = None,
        orchestrator_factory: Callable[..., Orchestrator] | None = None,
    ) -> None:
        self._settings = settings
        self._events = events or InMemoryEventBus()
        self._approvals = approvals or ApprovalRegistry()
        self._storage = storage
        self._driver_factory = driver_factory
        self._orchestrator_factory = orchestrator_factory
        self._open_unit_of_work: UnitOfWorkFactory = unit_of_work or database_unit_of_work(database)

        # One shared user browser means one task at a time, or tasks would
        # fight over focus and see each other's tabs.
        slots = 1 if settings.AGENT_CDP_URL else settings.AGENT_MAX_CONCURRENT_TASKS
        self._semaphore = asyncio.Semaphore(slots)
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._cancellations: dict[uuid.UUID, _Cancellation] = {}
        self._drivers: dict[uuid.UUID, PlaywrightDriver] = {}
        self._closing: set[asyncio.Task[None]] = set()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._started = False

    # ---------------------------------------------------------- lifecycle

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def running(self) -> int:
        """Tasks currently holding a slot or waiting for one."""
        return len(self._tasks)

    async def start(self) -> None:
        """Bring up the browser and clean up after the last process.

        Recovery runs before anything new is accepted. A task left `ACTING` by
        a crash has no worker and never will: the asyncio task that owned it
        died with the process, so nothing will ever move it again. Leaving it
        alone would show a user a task that appears to be running forever.
        """
        if self._started:
            return

        self._playwright = await async_playwright().start()
        if self._settings.AGENT_CDP_URL:
            # Opt-in: drive the user's own Chrome. Disconnects on stop; the
            # browser itself keeps running.
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.AGENT_CDP_URL, slow_mo=self._settings.AGENT_SLOW_MO_MS
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.AGENT_HEADLESS,
                slow_mo=self._settings.AGENT_SLOW_MO_MS,
                channel=self._settings.AGENT_BROWSER_CHANNEL or None,
            )
        self._started = True
        logger.info("task_runner_started", max_concurrent=self._settings.AGENT_MAX_CONCURRENT_TASKS)

        await self._recover_interrupted()

    async def stop(self) -> None:
        """Cancel everything in flight, then tear the browser down.

        Running tasks are cancelled cooperatively first and only then awaited,
        so each one gets the chance to write its own terminal status and
        publish a final event. Anything that does not stop in time is marked
        here instead, because a row left `ACTING` is the exact state the next
        startup has to guess about.
        """
        if not self._started:
            return
        self._started = False

        in_flight = list(self._tasks.items())
        for task_id, _ in in_flight:
            self._signal(task_id)
        await asyncio.gather(*(self._close_driver(task_id) for task_id, _ in in_flight))

        for _, runner_task in in_flight:
            runner_task.cancel()
        for _, runner_task in in_flight:
            with suppress(asyncio.CancelledError, Exception):
                await runner_task

        for task_id, _ in in_flight:
            await self._force_terminal(task_id, TaskStatus.CANCELLED, shutdown_reason)

        self._tasks.clear()
        self._cancellations.clear()
        self._drivers.clear()

        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

        logger.info("task_runner_stopped", cancelled=len(in_flight))

    async def __aenter__(self) -> "TaskRunner":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------- submit

    def submit(self, task_id: uuid.UUID) -> None:
        """Schedule a run. Returns immediately.

        Called after the creating request has committed. The task stays
        `SUBMITTED` until a slot frees, because the orchestrator writes
        `PLANNING` as its first act and it does not start until then.
        """
        if not self._started:
            raise RuntimeError("The task runner is not running.")
        if task_id in self._tasks:
            return

        self._cancellations[task_id] = _Cancellation()
        runner_task = asyncio.create_task(self._run(task_id), name=f"task-{task_id}")
        self._tasks[task_id] = runner_task
        runner_task.add_done_callback(lambda _: self._forget(task_id))

    async def cancel(self, task_id: uuid.UUID) -> bool:
        """Ask a running task to stop. Returns whether it was running here.

        Two things happen, and the second is what makes it prompt. The flag is
        read between phases, so a loop between steps stops within one phase.
        Closing the context makes an action that is already in flight fail
        immediately instead of waiting out its timeout.

        Cancelling a task this process is not running is not an error: the row
        has already been written `CANCELLED` by the caller, and the loop, here
        or elsewhere, refuses to move a task that is already terminal.
        """
        if task_id not in self._tasks:
            return False

        self._signal(task_id)
        await self._close_driver(task_id)
        logger.info("task_cancel_signalled", task_id=str(task_id))
        return True

    def cancel_soon(self, task_id: uuid.UUID) -> bool:
        """Signal a cancel without awaiting the driver teardown.

        The flag is what stops the loop and it is set synchronously here, so a
        caller that cannot await, such as an after-commit hook, still gets the
        prompt behaviour. Closing the context is the slow half and is left to
        run on its own.
        """
        if task_id not in self._tasks:
            return False

        self._signal(task_id)
        closing = asyncio.create_task(self._close_driver(task_id))
        # Held so the task is not garbage collected mid-flight.
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)
        return True

    def browser_for(self, task_id: uuid.UUID) -> DriverBrowser | None:
        """The live browser for a task this process is running.

        None when the task is queued, finished, or running on another worker.
        Used by the grounding endpoint, which asks a question about the page
        the agent is looking at right now and cannot do that from a row.
        """
        driver = self._drivers.get(task_id)
        return DriverBrowser(driver, self._settings) if driver is not None else None

    def is_running(self, task_id: uuid.UUID) -> bool:
        return task_id in self._tasks

    # --------------------------------------------------------------- work

    async def _run(self, task_id: uuid.UUID) -> None:
        """One task, start to finish, holding a slot for the whole run.

        The two capacity gauges are moved here rather than counted from
        ``self._tasks``, which cannot tell a task waiting for a slot from one
        that is running: both are entries in the same dictionary, and the
        difference is the whole point of the pair.
        """
        metrics.queued_tasks.inc()
        acquired = False
        try:
            async with self._semaphore:
                metrics.queued_tasks.dec()
                acquired = True
                metrics.active_tasks.inc()
                try:
                    await self._run_with_slot(task_id)
                finally:
                    metrics.active_tasks.dec()
        finally:
            # A task cancelled while still queued never reaches the slot, so the
            # decrement above never runs and the gauge would drift upward for
            # the life of the process.
            if not acquired:
                metrics.queued_tasks.dec()

    async def _run_with_slot(self, task_id: uuid.UUID) -> None:
        """The run itself, once a concurrency slot is held."""
        cancellation = self._cancellations.get(task_id) or _Cancellation()
        if cancellation.is_cancelled():
            # Cancelled while it was still queued. It never started, so
            # there is nothing to stop and nothing to publish.
            return

        driver = self._new_driver()
        self._drivers[task_id] = driver

        try:
            await driver.launch()
            await self._goto_start_url(task_id, driver)
            outcome = await self._build(task_id, driver, cancellation).run(task_id)
            logger.info(
                "task_finished",
                task_id=str(task_id),
                status=outcome.status.value,
                steps=outcome.steps,
            )
        except asyncio.CancelledError:
            # Shutdown. `stop` writes the terminal status, because doing it
            # here would need an await that the cancellation will interrupt.
            raise
        except Exception:
            logger.exception("task_runner_failed", task_id=str(task_id))
            await self._force_terminal(
                task_id,
                TaskStatus.EXECUTION_FAILED,
                "The task could not be started.",
            )
        finally:
            await self._close_driver(task_id)

    def _new_driver(self) -> PlaywrightDriver:
        """A context on the shared browser, or whatever a test injected.

        Injectable because the alternative is that every test of the runner's
        scheduling launches a real Chromium per task, which is slow enough to
        stop anyone running them.
        """
        if self._driver_factory is not None:
            return self._driver_factory()
        return PlaywrightDriver(self._settings, browser=self._browser)

    async def _goto_start_url(self, task_id: uuid.UUID, driver: PlaywrightDriver) -> None:
        """Open the task's start URL, if it has one.

        The allow-list was checked when the task was created and is checked
        again by the driver on the way out, because the row has been sitting in
        a database in between and configuration may have changed under it.
        """
        async with self._open_unit_of_work() as uow:
            task = await uow.tasks.get_or_404(task_id)
            start_url = task.start_url

        if start_url:
            await driver.page.goto(start_url)

    def _build(
        self,
        task_id: uuid.UUID,
        driver: PlaywrightDriver,
        cancellation: _Cancellation,
    ) -> Orchestrator:
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory(
                task_id=task_id,
                driver=driver,
                cancel_signal=cancellation,
            )

        llm = build_llm(self._settings)
        grounder = VisionGrounder(
            VisualGroundingService(
                llm,
                version=self._settings.VISION_PROMPT_VERSION,
                max_width=self._settings.VISION_MAX_WIDTH,
                max_image_bytes=self._settings.VISION_MAX_IMAGE_BYTES,
            )
        )

        return Orchestrator(
            browser=DriverBrowser(driver, self._settings),
            cache=DatabaseFlowCache(
                self._open_unit_of_work,
                min_success_rate=self._settings.CACHE_MIN_SUCCESS_RATE,
                enabled=self._settings.CACHE_ENABLED,
            ),
            planner=Planner(llm, PromptRegistry(self._settings.PLANNER_PROMPT_VERSION)),
            grounder=grounder,
            approvals=DatabaseApprovalGate(
                self._open_unit_of_work,
                self._approvals,
                self._events,
                timeout_seconds=self._settings.AGENT_APPROVAL_TIMEOUT_SECONDS,
            ),
            recovery=RecoveryRunner(
                max_attempts=self._settings.AGENT_MAX_RETRIES_PER_STEP,
                strategies=strategies_with_grounding(grounder),
            ),
            storage=self._storage or LocalFileStorage(self._settings),
            events=self._events,
            unit_of_work=self._open_unit_of_work,
            settings=self._settings,
            cancel_signal=cancellation,
            # The driver's own registry, not the id baked into the last page
            # state: these ids survive a reload, and a step event naming a tab
            # the client cannot find is worse than no tab id at all.
            sessions=driver.sessions,
        )

    # ------------------------------------------------------------ plumbing

    def _signal(self, task_id: uuid.UUID) -> None:
        cancellation = self._cancellations.get(task_id)
        if cancellation is not None:
            cancellation.cancel()

    async def _close_driver(self, task_id: uuid.UUID) -> None:
        driver = self._drivers.pop(task_id, None)
        if driver is not None:
            with suppress(Exception):
                await driver.close()

    def _forget(self, task_id: uuid.UUID) -> None:
        self._tasks.pop(task_id, None)
        self._cancellations.pop(task_id, None)

    async def _recover_interrupted(self) -> int:
        """Finish off tasks abandoned by the last process."""
        async with self._open_unit_of_work() as uow:
            abandoned = await uow.tasks.list_active()
            for task in abandoned:
                if TaskStatus(task.status) is TaskStatus.AWAITING_APPROVAL:
                    # The run that would have received the answer died with the
                    # process, so nobody is listening. The approval is expired
                    # rather than left PENDING, or the task's own endpoint would
                    # keep offering a decision that can never reach anything.
                    await expire_pending(uow, task.id, interrupted_reason)
                transition(task, _recovery_status(TaskStatus(task.status)))
                task.failure_reason = interrupted_reason
                task.finished_at = datetime.now(UTC)

        if abandoned:
            logger.warning("tasks_recovered_after_restart", count=len(abandoned))
        return len(abandoned)

    async def _force_terminal(
        self,
        task_id: uuid.UUID,
        status: TaskStatus,
        reason: str,
    ) -> None:
        """Write a terminal status for a run that could not write its own.

        Tolerant of the task already being terminal, which is the common case:
        a loop that noticed the cancel in time has settled itself, and this is
        only here for the one that did not.
        """
        try:
            async with self._open_unit_of_work() as uow:
                task = await uow.tasks.get_or_404(task_id)
                if TaskStatus(task.status).is_terminal:
                    return
                transition(task, status)
                task.failure_reason = reason
                task.finished_at = datetime.now(UTC)
        except Exception:
            logger.exception("task_force_terminal_failed", task_id=str(task_id))
