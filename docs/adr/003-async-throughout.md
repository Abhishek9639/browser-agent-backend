# ADR-003: Async throughout, with no blocking call on a request or agent path

- **Status:** accepted, implemented
- **Date:** 2026-08 (part B01)
- **Spec:** section 3

## Context

The service is almost entirely waiting: for a model, for a browser, for Postgres,
for a WebSocket client. A thread per concurrent task would spend its life blocked,
and the agent holds a browser context open for the length of a task, which can be
minutes.

## Decision

`async def` everywhere, with SQLAlchemy 2's async engine over asyncpg, httpx for
outbound HTTP, and Playwright's async API. Anything genuinely blocking is pushed
to a thread explicitly: the screenshot storage does its filesystem calls through
`asyncio.to_thread`, because the agent writes a screenshot every step and must not
stall the loop on disk.

`asyncio_mode = "auto"` in pytest, with one session-scoped event loop, so the
integration engine is built once.

## Consequences

**Good.** Three concurrent tasks cost three coroutines and three browser
contexts, not three threads. The WebSocket fan-out is a per-subscriber queue
rather than a thread each. A parked approval holds no database connection.

**Bad.** One blocking call anywhere stalls everything, and the ways to introduce
one are not obvious: `time.sleep`, a synchronous HTTP client, a `Path.read_bytes`
on a large file. `ruff`'s `ASYNC` rules catch some of it and not all. Debugging a
stalled loop is harder than debugging a stalled thread.

**Consequence nobody anticipated.** FastAPI's `BackgroundTasks` looked like the
way to start an agent run and is not: they run inside the exit stack that owns the
request's transaction, so a task started that way worked on a row the request had
not committed. That is what `app/services/task_runner.py` exists for.

## Verification

The whole suite runs under `asyncio_mode=auto`; `tests/integration/` exercises the
async engine against real Postgres. `ruff`'s `ASYNC` rule set runs in `make lint`.
