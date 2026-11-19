"""Live task events and the bus that delivers them (spec section 6.1).

Every event carries ``type``, ``taskId``, ``seq`` and ``timestamp``. ``seq`` is
per task and strictly increasing, which is what lets a reconnecting client say
"I saw up to N" and lets the server prove nothing was silently lost.

The bus is in-memory and per-process, which is right for one worker and wrong
for several: a subscriber connected to worker A never sees events published on
worker B. The interface exists so a Redis pub/sub implementation can replace it
without touching the orchestrator or the WebSocket route.
"""

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.logging import get_logger
from app.domain.enums import RiskLevel, TaskStatus

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "ApprovalRequiredEvent",
    "ApprovalResolvedEvent",
    "BoundingBox",
    "ErrorEvent",
    "Event",
    "EventBus",
    "EventType",
    "FinalEvent",
    "HeartbeatEvent",
    "InMemoryEventBus",
    "RecoveryEvent",
    "StatusEvent",
    "StepEvent",
    "Subscription",
    "TaskStats",
    "Viewport",
]

logger = get_logger(__name__)

# One page of steps plus headroom. Large enough that a browser tab pausing for
# a moment loses nothing, small enough that a client which has gone away does
# not hold a run's worth of screenshots in memory.
DEFAULT_QUEUE_SIZE: Final = 64


class EventType(StrEnum):
    """The section 6.1 event names, as they appear on the wire."""

    STATUS = "status"
    PLAN = "plan"
    STEP = "step"
    RECOVERY = "recovery"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    FINAL = "final"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


# Losing one of these leaves the client's view permanently wrong: a missed
# status means the wrong badge forever, a missed final means a spinner that
# never stops, a missed approval means a human is never asked. Steps and
# heartbeats are recoverable, because a step can be refetched by number and
# another heartbeat is fifteen seconds away.
UNDROPPABLE: Final[frozenset[EventType]] = frozenset(
    {
        EventType.STATUS,
        EventType.PLAN,
        EventType.APPROVAL_REQUIRED,
        EventType.APPROVAL_RESOLVED,
        EventType.FINAL,
        EventType.ERROR,
    }
)


class _Wire(BaseModel):
    """camelCase on the wire, matching the section 6.1 bodies."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
    )


class BoundingBox(_Wire):
    x: int
    y: int
    width: int
    height: int


class Viewport(_Wire):
    width: int
    height: int


class TaskStats(_Wire):
    steps: int = 0
    duration_ms: int = 0
    credits_used: int = 0
    cache_hit: bool = False
    # How many steps came from the cache. With `cache_hit` this says whether a
    # hit was worth anything: a flow that matched and then failed on its first
    # step is a hit that cost a lookup and saved nothing.
    replayed_steps: int = 0


class BaseEvent(_Wire):
    """Fields every event carries.

    ``seq`` and ``timestamp`` are placeholders until the bus stamps them: the
    publisher does not know its own sequence number, because ordering is a
    property of the task's stream rather than of any one producer.
    """

    task_id: uuid.UUID
    seq: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StatusEvent(BaseEvent):
    type: Literal[EventType.STATUS] = EventType.STATUS
    status: TaskStatus
    previous_status: TaskStatus | None = None


class PlanEvent(BaseEvent):
    """The plan as it now stands, in full.

    Sent when the plan is made, once the site is open and its questions are
    answered, and again whenever the agent adds an item it found the plan
    missing. Each one replaces the last.

    ``current_step`` is the 1-based index of the plan item the agent is
    actively working on when this event is sent. Items before it are
    completed; items at or after it are pending. ``None`` means the agent
    has not started executing yet (the plan was just made).
    """

    type: Literal[EventType.PLAN] = EventType.PLAN
    steps: list[str]
    current_step: int | None = None


class StepEvent(BaseEvent):
    type: Literal[EventType.STEP] = EventType.STEP
    step_number: int
    action: str
    target: str | None = None
    # Already masked by the perception layer. Spec section 10 forbids the real
    # value reaching a client, a log, or the database.
    value: str | None = None
    verified: bool | None = None
    # base64 JPEG, no wider than 1280, quality about 60.
    screenshot: str | None = None
    bbox: BoundingBox | None = None
    viewport: Viewport | None = None
    url: str | None = None
    tab_id: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    used_vision: bool = False
    # Replayed from a cached flow rather than planned. The activity log says so
    # because a step nobody chose is a different thing to watch: there is no
    # reasoning behind it and no model call to account for.
    replayed: bool = False
    # The plan item this step worked on, numbered from 1, so the client ticks
    # off the item the agent is really on instead of guessing from a count.
    plan_step: int | None = None

    def without_screenshot(self) -> "StepEvent":
        """The same step, minus the expensive part.

        The activity log still renders; only the thumbnail is missing, and the
        client can fetch it from the REST endpoint by step number.
        """
        return self.model_copy(update={"screenshot": None})


class RecoveryEvent(BaseEvent):
    type: Literal[EventType.RECOVERY] = EventType.RECOVERY
    step_number: int
    attempt_number: int
    strategy: str
    outcome: str


class ApprovalRequiredEvent(BaseEvent):
    type: Literal[EventType.APPROVAL_REQUIRED] = EventType.APPROVAL_REQUIRED
    approval_id: uuid.UUID
    step_number: int
    proposed_action: dict[str, Any]
    risk_level: RiskLevel
    reason: str | None = None
    screenshot: str | None = None
    bbox: BoundingBox | None = None


class ApprovalResolvedEvent(BaseEvent):
    type: Literal[EventType.APPROVAL_RESOLVED] = EventType.APPROVAL_RESOLVED
    approval_id: uuid.UUID
    approved: bool
    by: uuid.UUID | None = None
    reason: str | None = None


class FinalEvent(BaseEvent):
    type: Literal[EventType.FINAL] = EventType.FINAL
    status: TaskStatus
    final_answer: str | None = None
    failure_reason: str | None = None
    stats: TaskStats = Field(default_factory=TaskStats)


class ErrorEvent(BaseEvent):
    type: Literal[EventType.ERROR] = EventType.ERROR
    code: str
    message: str


class HeartbeatEvent(BaseEvent):
    type: Literal[EventType.HEARTBEAT] = EventType.HEARTBEAT


Event = Annotated[
    StatusEvent
    | PlanEvent
    | StepEvent
    | RecoveryEvent
    | ApprovalRequiredEvent
    | ApprovalResolvedEvent
    | FinalEvent
    | ErrorEvent
    | HeartbeatEvent,
    Field(discriminator="type"),
]

AnyEvent = (
    StatusEvent
    | PlanEvent
    | StepEvent
    | RecoveryEvent
    | ApprovalRequiredEvent
    | ApprovalResolvedEvent
    | FinalEvent
    | ErrorEvent
    | HeartbeatEvent
)


def _carries_strippable_screenshot(event: AnyEvent) -> bool:
    """A step whose image can be shed.

    Only steps. An approval prompt keeps its screenshot: a human being asked to
    approve a purchase is entitled to see what they are approving.
    """
    return isinstance(event, StepEvent) and event.screenshot is not None


class Subscription:
    """One client's view of one task's stream.

    A bounded buffer with a shedding policy rather than a plain queue, because
    the alternatives are both wrong: an unbounded queue lets one stalled client
    hold a whole run's screenshots in memory, and a blocking queue lets that
    client stall the agent loop itself.
    """

    def __init__(
        self,
        task_id: uuid.UUID,
        capacity: int = DEFAULT_QUEUE_SIZE,
        screenshot_capacity: int | None = None,
    ) -> None:
        self.task_id = task_id
        self.capacity = capacity
        # Two budgets, not one, because the two costs are orders of magnitude
        # apart: a queued step is around a kilobyte, its base64 JPEG is a
        # hundred times that. Bounding only the item count would let a stalled
        # client hold a run's worth of images; bounding only bytes would make
        # "how far behind is this client" unanswerable. The screenshot budget
        # is the smaller one, which is what makes images the first thing shed.
        self.screenshot_capacity = (
            max(1, capacity // 8) if screenshot_capacity is None else screenshot_capacity
        )
        self._items: deque[AnyEvent] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False
        # Counters rather than silence: shedding is invisible to the client, so
        # it has to be visible somewhere.
        self.screenshots_stripped = 0
        self.events_dropped = 0

    @property
    def pending(self) -> int:
        return len(self._items)

    @property
    def pending_screenshots(self) -> int:
        return sum(1 for event in self._items if _carries_strippable_screenshot(event))

    @property
    def closed(self) -> bool:
        return self._closed

    def offer(self, event: AnyEvent) -> None:
        """Queue an event, shedding load if the subscriber is behind.

        Never blocks and never raises. A subscriber's problems are its own: the
        agent loop publishes and moves on.
        """
        if self._closed:
            return
        self._items.append(event)
        self._shed_screenshots()
        self._enforce_capacity()
        self._wakeup.set()

    def _shed_screenshots(self) -> None:
        """Strip images from the oldest steps until back inside the budget.

        The step itself survives, so the activity log still renders in order
        and the image is refetchable by step number. The newest screenshot is
        always the one kept, because that is the one a viewer is looking at.
        """
        while self.pending_screenshots > self.screenshot_capacity:
            for index, event in enumerate(self._items):
                if _carries_strippable_screenshot(event):
                    self._items[index] = event.without_screenshot()  # type: ignore[union-attr]
                    self.screenshots_stripped += 1
                    break
            else:  # pragma: no cover - the count above guarantees a match
                return

    def _enforce_capacity(self) -> None:
        """Drop whole events, oldest droppable first, once the queue is full.

        Losing a status, approval, final or error leaves the client's view
        permanently wrong: a badge that never updates, a human who is never
        asked, a spinner that never stops. Those are kept even at the cost of
        overrunning the bound, which is a memory problem rather than a
        correctness one.
        """
        while len(self._items) > self.capacity:
            for index, event in enumerate(self._items):
                if event.type not in UNDROPPABLE:
                    del self._items[index]
                    self.events_dropped += 1
                    break
            else:
                logger.warning(
                    "event_buffer_over_capacity",
                    task_id=str(self.task_id),
                    pending=len(self._items),
                    capacity=self.capacity,
                )
                return

    def close(self) -> None:
        self._closed = True
        self._items.clear()
        self._wakeup.set()

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> AnyEvent:
        while True:
            if self._items:
                return self._items.popleft()
            if self._closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.unsubscribe()

    def unsubscribe(self) -> None:
        """Set by the bus, so closing a subscription deregisters it."""
        self.close()


class EventBus(ABC):
    """Publish task events, subscribe to one task's stream."""

    @abstractmethod
    async def publish(self, task_id: uuid.UUID, event: AnyEvent) -> AnyEvent:
        """Stamp the event with its sequence number and fan it out."""

    @abstractmethod
    def subscribe(
        self,
        task_id: uuid.UUID,
        capacity: int = DEFAULT_QUEUE_SIZE,
        screenshot_capacity: int | None = None,
    ) -> Subscription:
        """A new subscription. Close it, or use it as a context manager."""

    @abstractmethod
    def subscriber_count(self, task_id: uuid.UUID) -> int: ...


class InMemoryEventBus(EventBus):
    """Per-process fan-out.

    Sequence numbers are assigned under a lock. As written the critical
    section has no await in it, so on one event loop it is already atomic and
    the lock costs nothing; it is there so that the invariant survives the
    first await somebody adds, which a Redis implementation taking its number
    from INCR certainly will. Two publishers landing on the same number would
    let a client asking for "everything after 7" skip an event while believing
    it had seen them all, which is the one failure the sequence exists to
    prevent.
    """

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, list[Subscription]] = {}
        self._sequences: dict[uuid.UUID, int] = {}
        self._lock = asyncio.Lock()

    async def publish(self, task_id: uuid.UUID, event: AnyEvent) -> AnyEvent:
        async with self._lock:
            sequence = self._sequences.get(task_id, 0) + 1
            self._sequences[task_id] = sequence
            stamped = event.model_copy(
                update={
                    "task_id": task_id,
                    "seq": sequence,
                    "timestamp": datetime.now(UTC),
                }
            )
            subscribers = list(self._subscribers.get(task_id, ()))

        for subscription in subscribers:
            subscription.offer(stamped)

        return stamped

    def subscribe(
        self,
        task_id: uuid.UUID,
        capacity: int = DEFAULT_QUEUE_SIZE,
        screenshot_capacity: int | None = None,
    ) -> Subscription:
        subscription = Subscription(task_id, capacity, screenshot_capacity)
        bus = self

        def _unsubscribe() -> None:
            subscription.close()
            bus._remove(task_id, subscription)

        # Bound here rather than giving Subscription a reference to the bus:
        # the subscription stays a plain buffer that knows nothing about
        # routing, which is what makes it easy to test on its own.
        subscription.unsubscribe = _unsubscribe  # type: ignore[method-assign]

        self._subscribers.setdefault(task_id, []).append(subscription)
        return subscription

    def _remove(self, task_id: uuid.UUID, subscription: Subscription) -> None:
        subscribers = self._subscribers.get(task_id)
        if not subscribers:
            return
        if subscription in subscribers:
            subscribers.remove(subscription)
        if not subscribers:
            # Otherwise a long-running process accumulates an entry per task
            # that ever had a viewer.
            del self._subscribers[task_id]

    def subscriber_count(self, task_id: uuid.UUID) -> int:
        return len(self._subscribers.get(task_id, ()))

    def last_sequence(self, task_id: uuid.UUID) -> int:
        """The highest sequence issued for a task, or zero."""
        return self._sequences.get(task_id, 0)
