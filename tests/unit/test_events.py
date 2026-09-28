"""The live event stream: sequencing, fan-out, and what gives way under load."""

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from app.domain.enums import RiskLevel, TaskStatus
from app.services.events import (
    ApprovalRequiredEvent,
    ApprovalResolvedEvent,
    ErrorEvent,
    EventType,
    FinalEvent,
    HeartbeatEvent,
    InMemoryEventBus,
    RecoveryEvent,
    StatusEvent,
    StepEvent,
    Subscription,
    TaskStats,
)

TASK = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TASK = uuid.UUID("22222222-2222-2222-2222-222222222222")

# Stands in for a base64 JPEG. Long enough that the memory argument for
# shedding images before events is obvious from the fixture itself.
SHOT = "/9j/" + "A" * 2048


def step(number: int, *, screenshot: str | None = SHOT) -> StepEvent:
    return StepEvent(
        task_id=TASK,
        step_number=number,
        action="click",
        target="Search",
        screenshot=screenshot,
    )


def status(new: TaskStatus = TaskStatus.ACTING) -> StatusEvent:
    return StatusEvent(task_id=TASK, status=new)


async def drain(subscription: Subscription) -> list:
    """Everything queued right now, without waiting for more."""
    collected = []
    while subscription.pending:
        collected.append(await subscription.__anext__())
    return collected


async def test_publish_stamps_sequence_task_and_time() -> None:
    bus = InMemoryEventBus()

    published = await bus.publish(TASK, status())

    assert published.seq == 1
    assert published.task_id == TASK
    assert published.timestamp.tzinfo is not None


async def test_sequence_is_monotonic_per_task() -> None:
    bus = InMemoryEventBus()

    for expected in range(1, 6):
        published = await bus.publish(TASK, HeartbeatEvent(task_id=TASK))
        assert published.seq == expected


async def test_sequences_do_not_leak_between_tasks() -> None:
    bus = InMemoryEventBus()

    await bus.publish(TASK, status())
    await bus.publish(TASK, status())
    first_on_other = await bus.publish(OTHER_TASK, status())

    assert first_on_other.seq == 1
    assert bus.last_sequence(TASK) == 2
    assert bus.last_sequence(OTHER_TASK) == 1


async def test_concurrent_publishes_never_reuse_a_number() -> None:
    bus = InMemoryEventBus()

    published = await asyncio.gather(
        *(bus.publish(TASK, HeartbeatEvent(task_id=TASK)) for _ in range(50))
    )

    # A duplicate here would let a reconnecting client skip an event while
    # believing it had seen everything up to N.
    assert sorted(event.seq for event in published) == list(range(1, 51))


async def test_publishing_with_nobody_listening_still_advances() -> None:
    bus = InMemoryEventBus()

    await bus.publish(TASK, status())

    # A client attaching mid-run must not be handed sequence 1 again.
    subscription = bus.subscribe(TASK)
    second = await bus.publish(TASK, status(TaskStatus.VERIFYING))

    assert second.seq == 2
    assert [event.seq for event in await drain(subscription)] == [2]


async def test_events_arrive_in_order() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)

    await bus.publish(TASK, status(TaskStatus.PLANNING))
    await bus.publish(TASK, step(1))
    await bus.publish(TASK, FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED))

    received = await drain(subscription)

    assert [event.type for event in received] == [
        EventType.STATUS,
        EventType.STEP,
        EventType.FINAL,
    ]
    assert [event.seq for event in received] == [1, 2, 3]


async def test_every_subscriber_gets_every_event() -> None:
    bus = InMemoryEventBus()
    first = bus.subscribe(TASK)
    second = bus.subscribe(TASK)

    await bus.publish(TASK, status())
    await bus.publish(TASK, step(1))

    assert bus.subscriber_count(TASK) == 2
    assert [event.seq for event in await drain(first)] == [1, 2]
    assert [event.seq for event in await drain(second)] == [1, 2]


async def test_a_subscriber_only_sees_its_own_task() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)

    await bus.publish(OTHER_TASK, status())

    assert subscription.pending == 0


async def test_a_late_subscriber_misses_earlier_events() -> None:
    bus = InMemoryEventBus()

    await bus.publish(TASK, status())
    subscription = bus.subscribe(TASK)
    await bus.publish(TASK, step(1))

    assert [event.seq for event in await drain(subscription)] == [2]


async def test_iteration_waits_for_the_next_event() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)

    async def publish_shortly() -> None:
        await asyncio.sleep(0.01)
        await bus.publish(TASK, status())

    publisher = asyncio.create_task(publish_shortly())
    received = await asyncio.wait_for(subscription.__anext__(), timeout=1)
    await publisher

    assert received.type is EventType.STATUS


async def test_screenshots_are_shed_before_anything_else() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=16, screenshot_capacity=2)

    for number in range(1, 7):
        await bus.publish(TASK, step(number))

    received = await drain(subscription)

    # Every step survives, in order. Only the older images went.
    assert [event.step_number for event in received] == [1, 2, 3, 4, 5, 6]
    assert [event.screenshot is not None for event in received] == [
        False,
        False,
        False,
        False,
        True,
        True,
    ]
    assert subscription.screenshots_stripped == 4
    assert subscription.events_dropped == 0


async def test_an_approval_keeps_its_screenshot() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=16, screenshot_capacity=1)
    approval = ApprovalRequiredEvent(
        task_id=TASK,
        approval_id=uuid.uuid4(),
        step_number=3,
        proposed_action={"action": "click", "target": "Pay now"},
        risk_level=RiskLevel.HIGH,
        screenshot=SHOT,
    )

    await bus.publish(TASK, approval)
    for number in range(1, 5):
        await bus.publish(TASK, step(number))

    received = await drain(subscription)

    # A human deciding whether to spend money needs the picture.
    assert received[0].type is EventType.APPROVAL_REQUIRED
    assert received[0].screenshot == SHOT


async def test_droppable_events_go_once_the_queue_is_full() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=4, screenshot_capacity=1)

    await bus.publish(TASK, status(TaskStatus.PLANNING))
    for number in range(1, 7):
        await bus.publish(TASK, step(number, screenshot=None))

    received = await drain(subscription)

    assert subscription.pending == 0
    assert len(received) == 4
    # The status survived, the oldest steps did not, and the newest are intact.
    assert received[0].type is EventType.STATUS
    assert [event.step_number for event in received[1:]] == [4, 5, 6]
    assert subscription.events_dropped == 3


@pytest.mark.parametrize(
    "event",
    [
        StatusEvent(task_id=TASK, status=TaskStatus.ACTING),
        ApprovalRequiredEvent(
            task_id=TASK,
            approval_id=uuid.uuid4(),
            step_number=1,
            proposed_action={"action": "click"},
            risk_level=RiskLevel.HIGH,
        ),
        ApprovalResolvedEvent(task_id=TASK, approval_id=uuid.uuid4(), approved=True),
        FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED, stats=TaskStats(steps=3)),
        ErrorEvent(task_id=TASK, code="BROWSER_CRASHED", message="lost the page"),
    ],
)
async def test_load_shedding_never_discards_a_decisive_event(event) -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=2, screenshot_capacity=1)

    await bus.publish(TASK, event)
    for number in range(1, 21):
        await bus.publish(TASK, step(number, screenshot=None))

    received = await drain(subscription)

    assert received[0].type is event.type
    assert received[0].seq == 1


async def test_a_queue_of_only_decisive_events_overruns_rather_than_loses_one() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=2)

    for _ in range(6):
        await bus.publish(TASK, status())

    # The bound gives way before correctness does: too much memory is a worse
    # day, a client stuck on the wrong status is a wrong product.
    assert subscription.pending == 6
    assert subscription.events_dropped == 0


async def test_heartbeats_and_recoveries_are_droppable() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK, capacity=2)

    await bus.publish(TASK, HeartbeatEvent(task_id=TASK))
    await bus.publish(
        TASK,
        RecoveryEvent(
            task_id=TASK, step_number=1, attempt_number=1, strategy="retry", outcome="failed"
        ),
    )
    await bus.publish(TASK, status())
    await bus.publish(TASK, FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED))

    received = await drain(subscription)

    assert [event.type for event in received] == [EventType.STATUS, EventType.FINAL]
    assert subscription.events_dropped == 2


async def test_a_slow_subscriber_does_not_hold_up_a_fast_one() -> None:
    bus = InMemoryEventBus()
    slow = bus.subscribe(TASK, capacity=2, screenshot_capacity=1)
    fast = bus.subscribe(TASK, capacity=64, screenshot_capacity=64)

    for number in range(1, 11):
        await bus.publish(TASK, step(number))

    fast_received = await drain(fast)

    assert [event.step_number for event in fast_received] == list(range(1, 11))
    assert all(event.screenshot == SHOT for event in fast_received)
    assert slow.pending == 2


async def test_unsubscribe_deregisters_and_ends_the_iterator() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)

    subscription.unsubscribe()

    assert bus.subscriber_count(TASK) == 0
    assert subscription.closed
    with pytest.raises(StopAsyncIteration):
        await subscription.__anext__()


async def test_publishing_to_a_closed_subscription_is_harmless() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)
    subscription.unsubscribe()

    published = await bus.publish(TASK, status())

    assert published.seq == 1
    assert subscription.pending == 0


async def test_unsubscribing_one_leaves_the_other_running() -> None:
    bus = InMemoryEventBus()
    first = bus.subscribe(TASK)
    second = bus.subscribe(TASK)

    first.unsubscribe()
    await bus.publish(TASK, status())

    assert bus.subscriber_count(TASK) == 1
    assert first.pending == 0
    assert second.pending == 1


async def test_the_bus_forgets_a_task_once_nobody_is_watching() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe(TASK)

    subscription.unsubscribe()

    # Otherwise a long-lived process keeps an entry per task ever viewed.
    assert TASK not in bus._subscribers


async def test_the_context_manager_deregisters_on_the_way_out() -> None:
    bus = InMemoryEventBus()

    async with bus.subscribe(TASK) as subscription:
        await bus.publish(TASK, status())
        assert subscription.pending == 1

    assert bus.subscriber_count(TASK) == 0


async def test_the_context_manager_deregisters_after_a_failure() -> None:
    bus = InMemoryEventBus()

    with pytest.raises(RuntimeError):
        async with bus.subscribe(TASK) as subscription:
            await bus.publish(TASK, status())
            raise RuntimeError("client went away")

    # A dropped WebSocket raises out of the loop body. If that path leaked, a
    # long-lived server would accumulate buffers nobody reads.
    assert bus.subscriber_count(TASK) == 0
    assert subscription.closed


async def test_events_serialise_with_the_wire_names() -> None:
    bus = InMemoryEventBus()
    published = await bus.publish(TASK, step(1, screenshot=None))

    body = published.model_dump(mode="json", by_alias=True)

    assert body["type"] == "step"
    assert body["taskId"] == str(TASK)
    assert body["seq"] == 1
    assert body["stepNumber"] == 1
    assert "timestamp" in body


async def test_a_final_event_carries_its_stats() -> None:
    bus = InMemoryEventBus()
    published = await bus.publish(
        TASK,
        FinalEvent(
            task_id=TASK,
            status=TaskStatus.COMPLETED,
            final_answer="Rs 4,299 on SkyFare",
            stats=TaskStats(
                steps=7,
                duration_ms=41_000,
                credits_used=12,
                cache_hit=True,
                replayed_steps=5,
            ),
        ),
    )

    body = published.model_dump(mode="json", by_alias=True)

    assert body["finalAnswer"] == "Rs 4,299 on SkyFare"
    assert body["stats"] == {
        "steps": 7,
        "durationMs": 41_000,
        "creditsUsed": 12,
        "cacheHit": True,
        "replayedSteps": 5,
    }


async def test_events_are_immutable() -> None:
    event = status()

    # Frozen on purpose: the same object is handed to every subscriber, so a
    # mutation by one consumer would rewrite another's history.
    with pytest.raises(ValidationError):
        event.status = TaskStatus.COMPLETED  # type: ignore[misc]


async def test_stamping_does_not_mutate_the_caller_s_event() -> None:
    bus = InMemoryEventBus()
    original = status()

    published = await bus.publish(TASK, original)

    assert original.seq == 0
    assert published.seq == 1
