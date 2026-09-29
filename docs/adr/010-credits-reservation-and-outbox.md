# ADR-010: Reserve synchronously, settle through an outbox

- **Status:** accepted, implemented (client side)
- **Date:** 2026-09 (part B29)
- **Spec:** section 7, "Backend integration"

## Context

Four calls cross into the payments service: a signup grant, a reservation when a
task starts, and a settle or a release when it ends. Each can fail because the
other service is restarting, and the consequences are not symmetric.

A reservation the user does not hear about is a task that runs unpaid. A grant that
is lost is credits somebody paid for and never received. A settle that is lost is
work somebody used and was never charged for. None of the three can be fixed by
retrying inside the request, because by then the request is over.

## Decision

**Reserve is synchronous.** A user submitting a task has to be told now whether
they can afford it, so an insufficient balance is a 402 on that response carrying
`available` and `required`, and an unreachable payments service is a 503. A task
that cannot be paid for must not run, and one whose payment is unknown must not
either.

**Everything else goes through an outbox.** The call is written to
`billing_outbox` **in the same transaction as the business change it belongs to**:
the user row and its grant commit together, and the terminal task status and its
settle commit together. A worker delivers the backlog afterwards with exponential
backoff, using idempotency keys derived from the thing they are about —
`signup:<userId>`, `settle:<reservationId>` — so a call that landed before a
timeout is not applied twice.

## Consequences

**Good.** "Task finished but never charged" is unrepresentable rather than
unlikely: a finished task always has its charge queued, and a queued charge always
belongs to a finished task. Payments being down for an hour breaks task
submission and nothing else — registration still works, running tasks still
finish, and the backlog drains when it returns. A row that exhausts its attempts
is left undone rather than deleted, because it is the evidence that somebody's
credits are unaccounted for.

**Bad.** The reservation is taken before the task row commits, so a rollback in
between leaves a hold with nothing attached to it, cleared only by the payments
expiry job. The alternative is worse: the client would be told the task was
accepted and then charged separately, or not at all. Nothing reconciles the two
sides — if a settle lands and payments records something different, nothing here
notices.

**Rejected: calling payments after the commit.** No transaction to be part of,
therefore a gap in which the process can die with the task finished and nothing
owed recorded.

## Verification

`tests/integration/test_billing.py` (queued rather than called, retries until
success, settle amounts for plain, vision and replayed steps, 402 and 503 through
the API, disabled mode), `tests/unit/test_billing_client.py` (the HTTP contract
against a fake payments app), `tests/unit/test_credits.py` (the pricing).
