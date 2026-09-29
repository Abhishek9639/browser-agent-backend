# ADR-012: An asyncio runner with a semaphore, before a job queue

- **Status:** accepted, implemented; a queue is future work
- **Date:** 2026-09 (part B17)
- **Spec:** sections 4.13 and 9, decision A13

## Context

A task takes minutes and holds a browser context, so it cannot run inside the
request that submits it. The textbook answer is a job queue — Celery, RQ, arq —
with workers in separate processes.

## Decision

`app/services/task_runner.py`: tasks run as `asyncio.Task`s in the API process,
behind a global semaphore of three with a per-user cap of two, both configurable.
The runner is owned by the FastAPI lifespan, so it starts with the process and
stops before the database does. Cancellation is cooperative — a flag checked at
phase boundaries — plus closing the browser context so an action already in flight
fails immediately rather than waiting out its timeout.

On startup, tasks left `ACTING` or `PLANNING` by a dead process are failed with
"interrupted by restart" rather than resumed, because their browser and page state
died with the process.

## Consequences

**Good.** No broker, no worker deployment, no serialisation format for a task. The
loop shares a process with the API, so the grounding endpoint can reach the live
browser of a running task, which a queue in another process could not. Backpressure
is one semaphore. Cancellation is prompt because the runner holds the driver.

**Bad, and the reason this is an ADR rather than a detail.** Scaling out means
scaling the API, and three things are per process and would need replacing first:
the event bus, so a client connected to worker B sees nothing about a task running
on worker A; the rate limiter, so two workers allow twice the configured rate; the
WebSocket ticket store, for the same reason. All three are behind interfaces, and
each is recorded as a known limitation where it lives. A parked approval also holds
a concurrency slot for its whole wait, so three tasks waiting on humans fill the
default limit.

**Rejected: a queue now.** It would have bought horizontal scale nobody needs for
an OJT demo and cost a broker, a second deployment, and the ability to hold a live
browser for the grounding endpoint. The spec asks for asyncio before a queue, and
the interfaces are where they need to be for the swap.

## Verification

`tests/integration/test_tasks_api.py` (the real runner against real Postgres:
submit, concurrency cap, cancel, restart recovery),
`tests/unit/test_orchestrator.py` (cooperative cancellation at phase boundaries).
