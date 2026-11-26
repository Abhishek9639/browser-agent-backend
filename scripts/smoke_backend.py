"""Exercise a running local backend and SkyFare with the configured planner.

Run: uv run python scripts/smoke_backend.py
Works with the mock or a configured real provider. Creates a uniquely named
smoke-test user and task; prints no credentials. A real provider uses API quota.
"""

import argparse
import asyncio
import json
import secrets
import uuid
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect


async def run(base: str, site: str, *, use_default_start: bool = False) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        health = await client.get("/api/v1/health")
        health.raise_for_status()
        assert health.json()["db"] == "ok"
        print("PASS health and PostgreSQL")
        credentials = {
            "name": "Backend smoke test",
            "email": f"smoke-{uuid.uuid4().hex}@example.test",
            "password": secrets.token_urlsafe(24),
        }
        response = await client.post("/api/v1/auth/register", json=credentials)
        assert response.status_code == 201, response.text
        response = await client.post("/api/v1/auth/login", json=credentials)
        response.raise_for_status()
        headers = {"Authorization": f"Bearer {response.json()['accessToken']}"}
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
        response = await client.post("/api/v1/auth/refresh")
        response.raise_for_status()
        headers = {"Authorization": f"Bearer {response.json()['accessToken']}"}
        print("PASS registration, login, identity and refresh")
        on = (date.today() + timedelta(days=24)).isoformat()
        response = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "instruction": (
                    f"Find the cheapest flight from Delhi to Bengaluru on {on} on SkyFare. "
                    "Report its flight number and total price in INR."
                ),
                "startUrl": None if use_default_start else site,
            },
        )
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        prefix = f"/api/v1/tasks/{task_id}"
        response = await client.post(f"{prefix}/stream-ticket", headers=headers)
        response.raise_for_status()
        ticket = response.json()["ticket"]
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        steps = []
        async with (
            asyncio.timeout(360),
            connect(
                f"{ws_base}{prefix}/stream?{urlencode({'ticket': ticket})}",
                max_size=4_000_000,
            ) as ws,
        ):
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "step":
                    steps.append(event)
                elif event["type"] == "final":
                    assert event["status"] == "COMPLETED", event.get("failureReason")
                    answer = event["finalAnswer"]
                    break
            else:
                raise AssertionError("Stream ended without a final result")
        assert steps, "No browser steps streamed"
        response = await client.get(f"{prefix}/steps", headers=headers)
        response.raise_for_status()
        stored = response.json()
        shot = next(step for step in stored if step.get("screenshotRef"))
        response = await client.get(f"{prefix}/steps/{shot['id']}/screenshot", headers=headers)
        assert response.status_code == 200 and response.content.startswith(b"\xff\xd8")
        assert (await client.get(f"{prefix}/steps/{shot['id']}/screenshot")).status_code == 401
        async with httpx.AsyncClient(timeout=10) as oracle:
            response = await oracle.get(
                f"{site.rstrip('/')}/api/cheapest",
                params={"from": "Delhi", "to": "Bengaluru", "date": on},
                headers={"x-skyfare-oracle": "scoring-only"},
            )
            response.raise_for_status()
            price = response.json()["price"]
        assert str(price) in answer.replace(",", ""), answer
        print(f"PASS Chromium, WebSocket, stored steps, protected JPEG and oracle fare ({price})")
        assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 200
        assert (await client.get("/api/v1/stats/overview", headers=headers)).status_code == 200
        assert (await client.get("/.well-known/jwks.json")).status_code == 200
        assert (await client.post("/api/v1/auth/logout")).status_code == 204
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401
        print("PASS task history, stats, JWKS, logout and revoked refresh")
        print(f"Task: {task_id}\nAnswer: {answer}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--site-url", default="http://127.0.0.1:8200/")
    parser.add_argument("--use-default-start", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(args.base_url.rstrip("/"), args.site_url, use_default_start=args.use_default_start)
    )
