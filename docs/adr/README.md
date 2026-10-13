# Architecture decision records

Thirteen decisions, each with the alternatives that were rejected and the tests that
hold it in place. An ADR here is not a design document: it records a choice that
was hard to reverse, so that somebody arriving later can tell the difference
between a decision and an accident.

| ADR | Decision | Status |
|---|---|---|
| [001](001-layered-agent-architecture.md) | A layered agent, with the loop depending only on protocols | Implemented |
| [002](002-structured-llm-output-with-one-repair-turn.md) | Structured output with schema validation and one repair turn | Implemented |
| [003](003-async-throughout.md) | Async throughout, no blocking call on a request or agent path | Implemented |
| [004](004-deterministic-verification.md) | Verification is deterministic and never calls the model | Implemented |
| [005](005-postgres-with-an-explicit-unit-of-work.md) | Postgres, with one explicit unit of work per operation | Implemented |
| [006](006-three-repositories.md) | Three repositories rather than one | One of three built |
| [007](007-mock-llm-provider.md) | A mock LLM provider, in the application rather than the tests | Implemented |
| [008](008-skyfare-instead-of-live-sites.md) | A demo site we control, instead of live flight sites | Implemented |
| [009](009-websocket-tickets.md) | Single-use tickets for the WebSocket, not the access token | Implemented |
| [010](010-credits-reservation-and-outbox.md) | Reserve synchronously, settle through an outbox | Implemented (client side) |
| [011](011-set-of-marks-visual-grounding.md) | Set-of-marks for visual grounding, not raw coordinates | Implemented |
| [012](012-asyncio-runner-before-a-queue.md) | An asyncio runner with a semaphore, before a job queue | Implemented; a queue is future work |
| [013](013-opt-in-own-browser-mode.md) | An opt-in mode that drives the user's own Chrome | Implemented |

## On the numbering

`docs/PROJECT_SPEC.md` cites **ADR-004** by name, in section 4.7, and that is the
only ADR number the consolidated specification uses. Decision A18 in
`docs/BACKEND_PLAN.md` settled what that means: the specification's own text is
the whole requirement, and the citation is provenance rather than a pointer to a
document that exists elsewhere.

So ADR-004 is written to the decision the specification names. ADR-001, 002, 003
and 005 record the other four decisions the architecture rests on, written from
what was actually built rather than from a source document — which is the only
honest way to write them, since a record of a decision nobody can read is not a
record. ADR-006 to 012 are decisions this project made and named itself, each
during the part that made it.

## The thread running through them

Five of the twelve are the same argument in different places: **the model
proposes, and something deterministic decides.**

ADR-002 constrains what the model may say. ADR-004 takes the judgement of whether
it worked away from it entirely. ADR-011 restricts even the vision model's answer
to an index that can be validated against a list. The risk policy takes the higher
of the rules and the model's own opinion, never the lower. And the approval gate
puts a person in front of anything irreversible.

Stated once, because it is the answer to most questions about this system: a model
that is wrong about a page is exactly the model that will be confident about it,
so nothing consequential is decided by asking it.
