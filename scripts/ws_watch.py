#!/usr/bin/env python
"""Watch a task's live stream from the terminal.

Logs in, asks for a stream ticket, connects, and prints events as they arrive.
Screenshots are reported by size rather than printed, because a base64 JPEG is
tens of thousands of characters and nobody wants that in a terminal.

    uv run python scripts/ws_watch.py <task-id> --email you@example.test --password ...

The WebSocket client comes from `websockets`, which arrives with
`uvicorn[standard]` and is therefore already here. It is not used by the
service itself, only by this script.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets

DEFAULT_BASE = "http://127.0.0.1:8000"

# Keeps a long watch readable: one line per event, the interesting field first.
FORMATTERS: dict[str, Any] = {
    "status": lambda e: f"{e.get('previousStatus') or '-'} -> {e['status']}",
    "step": lambda e: (
        f"#{e['stepNumber']} {e['action']}"
        f"{' ' + repr(e['target']) if e.get('target') else ''}"
        f"{' = ' + repr(e['value']) if e.get('value') else ''}"
        f"  {'verified' if e.get('verified') else 'not verified'}"
        f"{_shot(e.get('screenshot'))}"
    ),
    "recovery": lambda e: (
        f"#{e['stepNumber']} attempt {e['attemptNumber']} {e['strategy']} -> {e['outcome']}"
    ),
    "approval_required": lambda e: (
        f"risk={e['riskLevel']} action={json.dumps(e['proposedAction'])}"
    ),
    "approval_resolved": lambda e: (
        f"{'approved' if e['approved'] else 'rejected'} {e.get('reason') or ''}"
    ),
    "final": lambda e: (
        f"{e['status']} {e.get('finalAnswer') or e.get('failureReason') or ''}"
        f"  [{e['stats']['steps']} steps, {e['stats']['durationMs']} ms,"
        f" {e['stats']['creditsUsed']} credits]"
    ),
    "error": lambda e: f"{e['code']}: {e['message']}",
    "heartbeat": lambda _: "",
}


def _shot(value: str | None) -> str:
    if not value:
        return ""
    return f"  screenshot={len(value) * 3 // 4 / 1024:.1f} KB"


def _line(event: dict[str, Any]) -> str:
    kind = event.get("type", "?")
    stamp = (event.get("timestamp") or "")[11:23]
    formatter = FORMATTERS.get(kind, lambda e: json.dumps(e))
    seq = event.get("seq", 0)
    marker = "replay" if seq == 0 and kind != "heartbeat" else f"#{seq}"
    return f"{stamp}  {marker:>7}  {kind:<18} {formatter(event)}".rstrip()


async def sign_in(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    response = await client.post(
        f"{base}/api/v1/auth/login", json={"email": email, "password": password}
    )
    response.raise_for_status()
    return str(response.json()["accessToken"])


async def get_ticket(client: httpx.AsyncClient, base: str, token: str, task_id: str) -> str:
    response = await client.post(
        f"{base}/api/v1/tasks/{task_id}/stream-ticket",
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return str(response.json()["ticket"])


async def watch(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15) as client:
        token = await sign_in(client, base, args.email, args.password)
        ticket = await get_ticket(client, base, token, args.task_id)

    socket_base = base.replace("https://", "wss://").replace("http://", "ws://")
    url = (
        f"{socket_base}/api/v1/tasks/{args.task_id}/stream"
        f"?ticket={ticket}&after_step={args.after_step}"
    )

    # The ticket is in the URL and the access token is not, which is the whole
    # reason the ticket exists.
    print(f"connecting to {socket_base}/api/v1/tasks/{args.task_id}/stream (ticket hidden)")

    try:
        async with websockets.connect(url) as socket:
            print(f"connected at {datetime.now(UTC).isoformat(timespec='seconds')}\n")
            async for raw in socket:
                event = json.loads(raw)
                if event.get("type") == "heartbeat" and not args.heartbeats:
                    continue
                print(_line(event))
                if event.get("type") == "final":
                    print("\nstream finished")
    except websockets.exceptions.ConnectionClosedError as closed:
        print(f"\nclosed with code {closed.code}: {closed.reason or 'no reason given'}")
        return 1
    except websockets.exceptions.ConnectionClosedOK:
        print("\nclosed normally")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch a task's live stream.")
    parser.add_argument("task_id", help="The task to watch.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument(
        "--after-step",
        type=int,
        default=0,
        help="Resume after this step number, as a reconnecting client would.",
    )
    parser.add_argument(
        "--heartbeats",
        action="store_true",
        help="Print heartbeats as well, which are hidden by default.",
    )
    return asyncio.run(watch(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
