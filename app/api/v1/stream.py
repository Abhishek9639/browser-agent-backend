"""The live view: a ticket, then a WebSocket (spec sections 6 and 6.1).

Two endpoints that belong together. The POST is ordinary authenticated HTTP and
hands back a ticket; the WebSocket takes that ticket in its query string. The
access token never appears in a URL, which is the whole point of the split: a
URL ends up in the server log, the proxy log and the browser history, and a
bearer token that reaches any of those is a bearer token that has leaked.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from pydantic import Field

from app.api.deps import (
    CurrentUser,
    EventBusDep,
    SettingsDep,
    StreamServiceDep,
    TaskServiceDep,
    TicketStoreDep,
)
from app.core.logging import get_logger
from app.schemas.base import CamelModel
from app.services.events import AnyEvent, EventType, HeartbeatEvent, Subscription
from app.services.stream import REPLAY_SEQ, dedupe_key, replayed_keys

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["stream"])

# Application close codes. 1000 is a normal close; anything in 4000-4999 is
# ours to define, and only reaches the client after a successful handshake,
# which is why the connection is accepted before it can be refused.
WS_INVALID_TICKET: Final = 4401
WS_NOT_OWNER: Final = 4403
WS_NO_TASK: Final = 4404


class StreamTicketResponse(CamelModel):
    ticket: str = Field(
        description="Single-use, valid once, for one task.",
        examples=[
            "kJ8s-3yT1QpX...",
        ],
    )
    expires_in: int = Field(description="Seconds until it stops working.", examples=[60])


@router.post(
    "/{task_id}/stream-ticket",
    response_model=StreamTicketResponse,
    summary="Get a ticket for the live stream",
    description=(
        "Returns a single-use ticket for opening the task's WebSocket. Valid "
        "for 60 seconds and for one connection. Pass it as the `ticket` query "
        "parameter on `WS /api/v1/tasks/{id}/stream`; never put an access "
        "token in that URL."
    ),
    responses={404: {"description": "No such task, or it belongs to someone else"}},
)
async def create_stream_ticket(
    task_id: uuid.UUID,
    user: CurrentUser,
    tasks: TaskServiceDep,
    tickets: TicketStoreDep,
    settings: SettingsDep,
) -> StreamTicketResponse:
    # Ownership is checked here, on an authenticated request, rather than at
    # connect time where the only credential is the ticket itself.
    await tasks.get(task_id, user)

    ticket = tickets.issue(user.id, task_id)
    return StreamTicketResponse(ticket=ticket.value, expires_in=settings.WS_TICKET_TTL_SECONDS)


@router.websocket("/{task_id}/stream")
async def stream(
    websocket: WebSocket,
    task_id: uuid.UUID,
    stream_service: StreamServiceDep,
    bus: EventBusDep,
    tickets: TicketStoreDep,
    settings: SettingsDep,
    ticket: Annotated[str, Query(description="From POST /tasks/{id}/stream-ticket")] = "",
    after_step: Annotated[int, Query(ge=0, description="Last step already seen")] = 0,
) -> None:
    """Replay what was missed, then stream what happens next."""
    await websocket.accept()

    redeemed = tickets.consume(ticket, task_id)
    if redeemed is None:
        await websocket.close(code=WS_INVALID_TICKET, reason="Invalid or expired ticket")
        return

    access = await stream_service.access(task_id, redeemed.user_id)
    if access == "missing":
        await websocket.close(code=WS_NO_TASK, reason="No such task")
        return
    if access == "forbidden":
        await websocket.close(code=WS_NOT_OWNER, reason="Not your task")
        return

    sender = _Sender(websocket)

    # Subscribed before the database is read. The other order has a window
    # between the read and the subscribe in which a step can be published and
    # seen by nobody, which is exactly the gap replay exists to close.
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        async with bus.subscribe(task_id) as subscription:
            replay = await stream_service.build(task_id, after_step)
            if replay is None:
                await websocket.close(code=WS_NO_TASK, reason="No such task")
                return

            for event in replay.events:
                await sender.send(event)

            logger.info(
                "stream_opened",
                task_id=str(task_id),
                after_step=after_step,
                replayed=len(replay.events),
                terminal=replay.is_terminal,
            )

            if replay.is_terminal:
                # Nothing more will ever happen on this task, so holding the
                # socket open would leave a client waiting on a feed that is
                # already over.
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                return

            await _pump(
                websocket,
                sender,
                subscription,
                task_id=task_id,
                seen=replayed_keys(replay.events),
                settings=settings,
            )

    with contextlib.suppress(RuntimeError):
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)


class _Sender:
    """Serialises everything that goes out.

    Three coroutines write to this socket: the event pump, the heartbeat and
    the reply to a ping. Starlette does not promise that concurrent sends
    interleave safely, and a torn frame kills the connection.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()

    async def send(self, event: AnyEvent) -> None:
        payload = event.model_dump(mode="json", by_alias=True)
        async with self._lock:
            await self._websocket.send_json(payload)

        if event.type is EventType.STEP:
            _log_latency(event)

    async def send_raw(self, payload: dict[str, object]) -> None:
        async with self._lock:
            await self._websocket.send_json(payload)


def _log_latency(event: AnyEvent) -> None:
    """How long a step took to reach a viewer.

    Spec section 9 wants the live view inside one to two seconds of the
    action. Measured from the moment the bus stamped the event, so it covers
    fan-out and the socket but not the browser's own rendering.

    Replayed events are skipped. Their timestamp is when the replay rebuilt
    them, not when anything happened, so recording that as latency would fill
    the KPI with a number that is always near zero and means nothing.
    """
    if event.seq == REPLAY_SEQ:
        return

    delay_ms = int((datetime.now(UTC) - event.timestamp).total_seconds() * 1000)
    logger.info(
        "stream_step_latency",
        task_id=str(event.task_id),
        seq=event.seq,
        latency_ms=delay_ms,
    )


async def _pump(
    websocket: WebSocket,
    sender: _Sender,
    subscription: Subscription,
    *,
    task_id: uuid.UUID,
    seen: set[tuple[str, object]],
    settings: SettingsDep,
) -> None:
    """Stream live events, heartbeat, and answer pings, until one stops."""

    async def forward() -> None:
        async for event in subscription:
            key = dedupe_key(event)
            if key is not None:
                if key in seen:
                    # Published while the replay was being built. The client
                    # already has it, and sending it twice would show the same
                    # step twice in the activity log.
                    continue
                seen.add(key)

            await sender.send(event)
            if event.type is EventType.FINAL:
                return

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.WS_HEARTBEAT_SECONDS)
            # Built here rather than published, so it never takes a sequence
            # number: a heartbeat is not part of the task's history and a
            # reconnecting client must not have to account for one.
            await sender.send(HeartbeatEvent(task_id=task_id))

    async def receive() -> None:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("type") == "ping":
                await sender.send_raw({"type": "pong"})

    runners = [
        asyncio.create_task(forward(), name="ws-forward"),
        asyncio.create_task(heartbeat(), name="ws-heartbeat"),
        asyncio.create_task(receive(), name="ws-receive"),
    ]
    try:
        # The first to finish decides: the run ended, or the client went away.
        await asyncio.wait(runners, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for runner in runners:
            runner.cancel()
        for runner in runners:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
