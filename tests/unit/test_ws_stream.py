"""The live stream endpoint: close codes, replay, dedupe, heartbeat, ping.

Driven through Starlette's test client, which does a real WebSocket handshake,
against stubbed collaborators. Nothing here touches Postgres: what is being
tested is what the socket does, and the replay itself is exercised against real
rows in the integration suite.
"""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_event_bus, get_stream_service, get_ticket_store
from app.api.v1 import stream as stream_api
from app.core.config import Settings
from app.domain.enums import TaskStatus
from app.services.events import (
    AnyEvent,
    FinalEvent,
    StatusEvent,
    StepEvent,
    Subscription,
)
from app.services.stream import Replay
from app.services.ws_tickets import InMemoryTicketStore

TASK = uuid.UUID("55555555-5555-4555-8555-555555555555")
USER = uuid.UUID("66666666-6666-4666-8666-666666666666")


class StubStreamService:
    """Stands in for the database side of the stream."""

    def __init__(
        self,
        replay: Replay | None,
        access: str = "ok",
    ) -> None:
        self._replay = replay
        self._access = access
        self.built_after: list[int] = []

    async def access(self, task_id: uuid.UUID, user_id: uuid.UUID) -> str:
        return self._access

    async def build(self, task_id: uuid.UUID, after_step: int = 0) -> Replay | None:
        self.built_after.append(after_step)
        return self._replay


class StubBus:
    """Hands out one subscription that the test has already filled."""

    def __init__(self, subscription: Subscription) -> None:
        self.subscription = subscription

    def subscribe(self, task_id: uuid.UUID, *args: Any, **kwargs: Any) -> Subscription:
        return self.subscription

    async def publish(self, task_id: uuid.UUID, event: AnyEvent) -> AnyEvent:  # pragma: no cover
        return event

    def subscriber_count(self, task_id: uuid.UUID) -> int:  # pragma: no cover
        return 1


def step(number: int, *, screenshot: str | None = None) -> StepEvent:
    return StepEvent(
        task_id=TASK,
        seq=number,
        step_number=number,
        action="click",
        target=f"Select {number}",
        screenshot=screenshot,
    )


def replay_of(*events: AnyEvent, status: TaskStatus = TaskStatus.ACTING, last: int = 0) -> Replay:
    return Replay(list(events), status=status, last_step=last)


def build_app(
    *,
    service: StubStreamService,
    subscription: Subscription | None = None,
    tickets: InMemoryTicketStore | None = None,
    heartbeat_seconds: int = 15,
) -> tuple[FastAPI, InMemoryTicketStore, Subscription]:
    settings = Settings(WS_HEARTBEAT_SECONDS=heartbeat_seconds)
    store = tickets or InMemoryTicketStore()
    feed = subscription if subscription is not None else Subscription(TASK, capacity=64)

    application = FastAPI()
    application.state.settings = settings
    application.include_router(stream_api.router, prefix="/api/v1")
    application.dependency_overrides[get_stream_service] = lambda: service
    application.dependency_overrides[get_event_bus] = lambda: StubBus(feed)
    application.dependency_overrides[get_ticket_store] = lambda: store
    return application, store, feed


def url(ticket: str, after_step: int = 0) -> str:
    return f"/api/v1/tasks/{TASK}/stream?ticket={ticket}&after_step={after_step}"


# ------------------------------------------------------------ close codes


def test_an_unknown_ticket_is_refused() -> None:
    app, _, _ = build_app(service=StubStreamService(replay_of()))

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(url("nonsense")) as socket,
    ):
        # The handshake succeeds and the close arrives on the first read, so
        # the code only shows up when something is asked of the socket.
        socket.receive_json()

    assert caught.value.code == stream_api.WS_INVALID_TICKET


def test_a_ticket_works_once_over_the_socket() -> None:
    service = StubStreamService(
        replay_of(
            StatusEvent(task_id=TASK, status=TaskStatus.COMPLETED), status=TaskStatus.COMPLETED
        )
    )
    app, store, _ = build_app(service=service)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client:
        with client.websocket_connect(url(ticket)) as socket:
            socket.receive_json()

        with (
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect(url(ticket)) as second,
        ):
            second.receive_json()

    # Reconnecting needs a new ticket, which needs the access token again.
    assert caught.value.code == stream_api.WS_INVALID_TICKET


def test_someone_elses_task_is_refused() -> None:
    app, store, _ = build_app(service=StubStreamService(replay_of(), access="forbidden"))
    ticket = store.issue(USER, TASK).value

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(url(ticket)) as socket,
    ):
        # The handshake succeeds and the close arrives on the first read, so
        # the code only shows up when something is asked of the socket.
        socket.receive_json()

    assert caught.value.code == stream_api.WS_NOT_OWNER


def test_a_missing_task_is_refused() -> None:
    app, store, _ = build_app(service=StubStreamService(replay_of(), access="missing"))
    ticket = store.issue(USER, TASK).value

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(url(ticket)) as socket,
    ):
        # The handshake succeeds and the close arrives on the first read, so
        # the code only shows up when something is asked of the socket.
        socket.receive_json()

    assert caught.value.code == stream_api.WS_NO_TASK


def test_a_task_that_vanished_between_the_ticket_and_the_socket() -> None:
    app, store, _ = build_app(service=StubStreamService(None))
    ticket = store.issue(USER, TASK).value

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect(url(ticket)) as socket,
    ):
        # The handshake succeeds and the close arrives on the first read, so
        # the code only shows up when something is asked of the socket.
        socket.receive_json()

    assert caught.value.code == stream_api.WS_NO_TASK


# ---------------------------------------------------------------- replay


def test_the_status_comes_first_then_the_missed_steps() -> None:
    service = StubStreamService(
        replay_of(
            StatusEvent(task_id=TASK, status=TaskStatus.ACTING),
            step(3),
            step(4),
        )
    )
    app, store, _ = build_app(service=service)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket, after_step=2)) as socket:
        first = socket.receive_json()
        second = socket.receive_json()
        third = socket.receive_json()

    assert first["type"] == "status"
    assert [second["stepNumber"], third["stepNumber"]] == [3, 4]
    assert service.built_after == [2]


def test_a_finished_task_is_replayed_and_the_socket_closes() -> None:
    service = StubStreamService(
        replay_of(
            StatusEvent(task_id=TASK, status=TaskStatus.COMPLETED),
            step(1),
            FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED, final_answer="₹4,210"),
            status=TaskStatus.COMPLETED,
        )
    )
    app, store, _ = build_app(service=service)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        kinds = [socket.receive_json()["type"] for _ in range(3)]
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_json()

    assert kinds == ["status", "step", "final"]
    # Holding the socket open would leave a client waiting on a feed that is
    # already over.
    assert caught.value.code == 1000


# ------------------------------------------------------------------ live


def test_live_events_follow_the_replay() -> None:
    feed = Subscription(TASK, capacity=32)
    feed.offer(step(3))
    feed.offer(FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED))

    service = StubStreamService(
        replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING), step(1), step(2))
    )
    app, store, _ = build_app(service=service, subscription=feed)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        received = [socket.receive_json() for _ in range(5)]

    assert [item["type"] for item in received] == ["status", "step", "step", "step", "final"]
    assert [item["stepNumber"] for item in received[1:4]] == [1, 2, 3]


def test_a_step_in_both_the_replay_and_the_live_feed_is_sent_once() -> None:
    feed = Subscription(TASK, capacity=32)
    # Published while the replay was being built, so the client has it already.
    feed.offer(step(2))
    feed.offer(step(3))
    feed.offer(FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED))

    service = StubStreamService(
        replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING), step(1), step(2))
    )
    app, store, _ = build_app(service=service, subscription=feed)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        received = [socket.receive_json() for _ in range(4)]

    numbers = [item["stepNumber"] for item in received if item["type"] == "step"]
    assert numbers == [1, 2, 3], "step 2 was sent twice"


def test_reconnecting_receives_only_what_was_missed() -> None:
    feed = Subscription(TASK, capacity=32)
    feed.offer(FinalEvent(task_id=TASK, status=TaskStatus.COMPLETED))

    service = StubStreamService(
        replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING), step(4), step(5))
    )
    app, store, _ = build_app(service=service, subscription=feed)
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket, after_step=3)) as socket:
        received = [socket.receive_json() for _ in range(4)]

    assert service.built_after == [3]
    assert [item["stepNumber"] for item in received if item["type"] == "step"] == [4, 5]


# --------------------------------------------------------- ping and pulse


def test_a_ping_is_answered_with_a_pong() -> None:
    app, store, _ = build_app(
        service=StubStreamService(replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING)))
    )
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        socket.receive_json()
        socket.send_json({"type": "ping"})
        reply = socket.receive_json()

    assert reply == {"type": "pong"}


def test_the_connection_beats_while_nothing_happens() -> None:
    app, store, _ = build_app(
        service=StubStreamService(replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING))),
        heartbeat_seconds=1,
    )
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        socket.receive_json()
        beat = socket.receive_json()

    assert beat["type"] == "heartbeat"
    assert beat["taskId"] == str(TASK)
    # A heartbeat is not part of the task's history, so it takes no sequence
    # number a reconnecting client would have to account for.
    assert beat["seq"] == 0


def test_an_unknown_client_message_is_ignored() -> None:
    app, store, _ = build_app(
        service=StubStreamService(replay_of(StatusEvent(task_id=TASK, status=TaskStatus.ACTING)))
    )
    ticket = store.issue(USER, TASK).value

    with TestClient(app) as client, client.websocket_connect(url(ticket)) as socket:
        socket.receive_json()
        socket.send_json({"type": "something-else"})
        socket.send_json({"type": "ping"})

        assert socket.receive_json() == {"type": "pong"}
