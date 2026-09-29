# ADR-001: A layered agent, with the loop depending only on protocols

- **Status:** accepted, implemented
- **Date:** 2026-08 (part B01), revisited every part since
- **Spec:** section 4.1

## Context

An agent that drives a browser has at least eight concerns: launching a browser,
reading a page, deciding what to do, executing it, checking it worked, recovering
when it did not, judging risk, and recording all of it. Written as one module they
become one function nobody can test, and the first thing that suffers is the
ability to run the loop without a browser.

The alternative shapes considered were a single orchestrator module with helper
functions, and a plugin system with runtime registration.

## Decision

Layers as in spec section 4.1, each its own module under `app/agent/`, and the
orchestrator depends on **protocols** rather than implementations. Every
collaborator is a `typing.Protocol` in `app/agent/contracts.py` with a default
implementation that does the least interesting correct thing.

Two rules hold it together:

- `app/agent/` never imports FastAPI. The loop is driven by `app/services/`, so
  it stays runnable without an HTTP stack.
- Dependencies point one way: `api → services → repositories → models`, and the
  agent sits beside services rather than above them.

## Consequences

**Good.** `tests/unit/test_orchestrator.py` drives the entire loop against a fake
browser, a scripted LLM and a fake database, in milliseconds. Each later part
replaced one default — the verifier, the recovery ladder, the risk policy, the
cache — without touching the loop. The defaults are named after what they do not
do (`NoCache`, `NoGrounding`, `DenyApprovals`), so a stack trace says which
scaffold is still in place.

**Bad.** There are more files than a single module would need, and a reader
tracing one step goes through several. The protocol indirection costs a level of
misdirection when debugging: the thing being called is whatever was injected.

**Rejected: a plugin system.** Runtime registration would have bought
configurability nobody asked for and lost the type checking that makes a protocol
worth having.

## Verification

`tests/unit/test_orchestrator.py` (the loop against fakes), and a grep in CI:
`app/agent/` importing `fastapi` would fail `mypy` and the import test.
