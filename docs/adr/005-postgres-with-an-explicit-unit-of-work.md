# ADR-005: Postgres, with one explicit unit of work per operation

- **Status:** accepted, implemented
- **Date:** 2026-08 (parts B03–B05)
- **Spec:** section 5

## Context

The agent writes a lot per step: a task step, an LLM decision, a verification, and
zero or more recovery attempts, all of which describe the same moment and must
either all exist or none. Meanwhile the API reads the same tables, and a live
WebSocket client is told about a step it must then be able to fetch.

The question is who owns the transaction. Repositories committing their own writes
is the least code and the most surprising: a partial step becomes representable.

## Decision

Postgres, SQLAlchemy 2 async, and an explicit `UnitOfWork`
(`app/repositories/unit_of_work.py`) that owns the session. The rule is one
sentence: **whoever opens the session commits it, and nobody else does.**
Repositories flush, never commit. Services take a unit of work rather than making
one.

`lazy="raise"` on every relationship, so a lazy load in a request is an error
rather than a surprise query.

Consequences that fall out of this, each argued in a phase note:

- A step's four rows are one transaction.
- The step event is published **after** the commit, never inside it.
- The billing outbox row is written **in** the business transaction, which is what
  makes "task finished but never charged" unrepresentable.

## Consequences

**Good.** Partial writes are not possible. The ordering rules above are statements
about one object rather than conventions spread over a codebase. Tests run inside
a transaction that is rolled back, so they share a database without interfering.

**Bad.** Every service signature carries a unit of work. A caller that wants two
independent transactions has to open two, and two places genuinely need that: the
failed-login audit and refresh-token reuse detection, both of which must survive
the rollback a 401 triggers.

## Verification

`tests/integration/test_db.py`, `tests/integration/test_repositories_*.py`, and
`tests/unit/test_orchestrator.py`, whose fake store records when writes became
visible and asserts the event never precedes the commit.
