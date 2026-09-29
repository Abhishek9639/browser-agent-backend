# v1.0.0 — the backend, complete

The backend of AI-07: a supervised autonomous browser agent. Thirty-one parts,
built one at a time, each with its own phase note recording what was decided and
what broke.

This tag marks the backend as feature-complete against
[docs/PROJECT_SPEC.md](PROJECT_SPEC.md). The frontend and the payments service are
separate repositories and are not built; the backend runs and demonstrates itself
without either.

## What it does

A user writes a task in plain language. A server-side Chromium reads the page, a
language model proposes one structured action at a time, the backend executes it,
a deterministic verifier checks what actually happened, and a recovery ladder
handles the page breaking underneath it. Anything irreversible stops and waits
for a person. Every step is stored and streamed live.

## Against the targets

Spec section 9 sets two bars. Both are cleared, measured over thirty runs across
ten chaos configurations
([docs/benchmarks/2026-09-28-mock.md](benchmarks/2026-09-28-mock.md)):

| Metric | Target | Result |
|---|---|---|
| End-to-end success | ≥80% | **100%** |
| Recovery success | >70% | **100%** |
| Verification accuracy | — | 100% |
| Median time per task | — | 5.6s |
| p90 time per task | — | 27.4s |
| API p95, excluding agent work | <2s | within budget |

Success means the task finished **and** reported the fare SkyFare's oracle says
is cheapest. A completed task with the wrong number counts as a failure.

Those numbers are measured with the mock provider, which is rule-based and
offline: they measure the loop, the verifier and the recovery ladder rather than
a model's judgement. That distinction is stated wherever the numbers appear.

## What is in it

**The agent.** Perception from a real DOM with a ~3k-token summary and hidden
elements excluded; planning with structured output against a JSON schema and one
repair turn; four LLM providers behind one interface; set-of-marks visual
grounding on a trigger; eleven deterministic verification checks; a seven-rung
recovery ladder routed by the failure's error code; multi-tab sessions; a flow
cache that replays a previous run's verified steps.

**Safety.** Deterministic risk rules in English and Hinglish, human approval
before anything irreversible, and eight documented layers of prompt-injection
defence — the last three of which do not read the prompt at all.

**The service.** RS256 JWTs with refresh rotation and reuse detection, RBAC,
twenty-three endpoints, a WebSocket live view with single-use tickets and replay
from the database, sixteen Prometheus metrics, a stats endpoint, security headers,
rate limits, and a request body limit.

**The demo.** SkyFare, a flight search site with deterministic results, an oracle
that knows the right answer, and nine chaos toggles reproducing what real sites
do.

**The operations.** Two Docker images on the official Playwright base, a compose
stack that works from a clean checkout, and a seven-job CI workflow with one
required gate.

## Numbers

| | |
|---|---|
| Parts | 31 |
| Unit tests | 1,148 (one skipped) |
| Integration tests | 337 |
| Chaos tests | 23 |
| Endpoints | 23 |
| Database tables | 11 |
| Architecture decision records | 12 |
| Settings, all documented | 94 |

## Known limitations

Written down rather than discovered. Each has more detail in the phase note that
recorded it.

- **Scale is one process.** The event bus, the rate limiter and the WebSocket
  ticket store are all per process, so two workers would each see half the
  picture. All three sit behind interfaces ([ADR-012](adr/012-asyncio-runner-before-a-queue.md)).
- **The payments service does not exist.** This side of the contract is built and
  tested against a fake; the first real integration will find things the contract
  got wrong.
- **Live-provider numbers are missing.** The one attempt returned 503 from the
  provider on every call. The headline numbers are measured under the mock.
- **The prompt eval cannot see a prompt change**, because the mock ignores the
  system prompt. `planner_v2` is therefore unvalidated by that harness and rests
  on the chaos suite.
- **Shadow DOM is not traversed.**
- **A WebSocket frame is not size-limited**, unlike an HTTP body.
- **The flow cache is not partitioned by user**, so two users with the same
  instruction on the same site would share a flow.
- **Branch protection is documented, not enforced** — GitHub does not enforce it
  on a private free-plan repository.
- **Nothing reconciles billing** between this service and payments.
- **`docs/traceability.md` and `docs/security.md` are maintained by hand.** A
  renamed test makes them wrong and nothing fails.

## Getting it running

```bash
make up
```

Postgres, the API and the demo site, with migrations applied and a signing key
generated on first start. Then <http://localhost:8000/docs>.

## Thanks

The specification, the plan and the part briefs came from the OJT programme. The
decisions recorded in [docs/adr/](adr/) are this repository's own, and each one
names the alternative it rejected.
