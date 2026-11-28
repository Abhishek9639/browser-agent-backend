# Architecture

Five diagrams and what each one is for. Everything drawn here exists in the
repository unless a note says otherwise, and the future work is marked rather
than drawn optimistically: a diagram that shows a component nobody has written is
a diagram that gets somebody's hopes up in a review.

Paths are real. If a box names a file, that file is on disk.

## 1. System context

AI-07 is three repositories. This one is the backend and the agent; the other two
are separate services with their own databases.

```mermaid
graph TB
    user([User in a browser])

    subgraph frontend["browser-agent-frontend &mdash; NOT BUILT"]
        ui["Next.js app<br/>task form, live view, history"]
    end

    subgraph backend["browser-agent-backend &mdash; THIS REPOSITORY"]
        api["FastAPI<br/>app/api/v1/"]
        agent["Agent loop<br/>app/agent/orchestrator.py"]
        chromium["Chromium<br/>via Playwright"]
        db[("Postgres<br/>agent_core")]
    end

    subgraph payments["browser-agent-payments &mdash; NOT BUILT"]
        wallet["Credits wallet<br/>Razorpay TEST mode"]
        pdb[("Postgres<br/>payments")]
    end

    site["Target site<br/>any allow-listed host"]

    user -->|"HTTPS + WebSocket"| ui
    ui -->|"REST, JWT"| api
    ui -.->|"wallet, packs"| wallet
    api --> agent
    agent --> chromium
    chromium -->|"only allow-listed hosts"| site
    api --> db
    agent --> db
    api -->|"X-Internal-Token<br/>reserve, settle, grant"| wallet
    wallet --> pdb
    wallet -.->|"verifies our tokens<br/>/.well-known/jwks.json"| api
```

**Implemented:** everything inside the backend box, plus the client
side of the payments contract (`app/services/billing_client.py`) and its
`docs/billing-integration.md` specification.

**Not built:** the frontend and the payments service. The backend runs with
`BILLING_ENABLED=false` by default and is fully usable from `/docs` or curl, so
neither is a prerequisite for a demo.

The dashed lines are the two directions that cross a repository boundary without
a person in between: the backend calls payments over an internal API, and
payments verifies the backend's JWTs by fetching its JWKS. Both are specified in
`docs/billing-integration.md` and `docs/auth.md`.

## 2. Agent layers

Spec section 4.1's table, as a picture. The arrows are dependencies, so they all
point the same way: `app/agent/` never imports FastAPI, and nothing in the agent
imports a service.

```mermaid
graph TB
    subgraph api_layer["API &mdash; app/api/"]
        routers["routers<br/>tasks, auth, stream, stats, ops"]
    end

    subgraph service_layer["Services &mdash; app/services/"]
        runner["task_runner.py<br/>semaphore, cancel, lifespan"]
        tsvc["task_service.py"]
        billing["billing.py<br/>billing_client.py"]
        events["events.py<br/>stream.py"]
        storage["storage.py"]
    end

    subgraph agent_layer["Agent &mdash; app/agent/"]
        orch["orchestrator.py<br/>the loop"]
        planner["planner/planner.py<br/>prompts/planner_v2.md"]
        llm["llm/<br/>anthropic openai gemini mock"]
        perception["perception/<br/>dom_extractor summarizer bs4_fallback"]
        vision["vision/<br/>grounding.py marks.py"]
        driver["browser/<br/>driver.py sessions.py"]
        verify["verification/verifier.py"]
        recovery["recovery/<br/>strategies.py runner.py routing.py"]
        safety["safety/<br/>risk.py gate.py"]
        cache["cache/<br/>signature.py flow.py store.py"]
    end

    subgraph data_layer["Data &mdash; app/repositories/, app/models/"]
        repos["repositories<br/>unit_of_work.py"]
        models["models<br/>SQLAlchemy 2"]
    end

    routers --> tsvc
    routers --> runner
    routers --> events
    runner --> orch
    tsvc --> repos
    billing --> repos
    orch --> planner
    orch --> perception
    orch --> vision
    orch --> driver
    orch --> verify
    orch --> recovery
    orch --> safety
    orch --> cache
    orch --> repos
    planner --> llm
    vision --> llm
    recovery --> driver
    cache --> repos
    repos --> models
```

Every collaborator the loop uses is a Protocol in `app/agent/contracts.py` with a
default implementation. That is not decoration: it is what lets
`tests/unit/test_orchestrator.py` drive the whole loop against a fake browser
with no Chromium at all, and it is why each part could replace one default
without touching the loop.

## 3. One loop iteration

The sequence for a single step, in the order things actually happen. Spec section
4.2 is the pseudo-code; this is what the code does.

```mermaid
sequenceDiagram
    participant R as Runner
    participant O as Orchestrator
    participant B as Browser
    participant P as Planner
    participant V as Vision
    participant S as Risk + Gate
    participant D as Verifier
    participant DB as Postgres
    participant W as WebSocket

    R->>O: run(task_id)
    O->>DB: status PLANNING
    O->>B: perceive()
    B-->>O: PageState (elements, summary, hashes)
    O->>P: decide(goal, state, history, hint)
    P-->>O: Decision (validated against the schema)

    opt trigger fired (§4.9)
        O->>V: resolve(decision, state, screenshot, reason)
        V-->>O: corrected index, or declined
    end

    O->>O: resolve target index in the state
    O->>S: assess(decision, state)
    S-->>O: max(rules, model)

    opt HIGH risk
        O->>DB: approval row, then commit
        O->>W: approval_required
        S-->>O: approved / rejected / expired
    end

    O->>DB: status ACTING
    O->>B: execute(action)
    O->>DB: status VERIFYING
    O->>B: settle(), then perceive()
    O->>D: check(decision, before, after, result)
    D-->>O: Verdict

    opt verdict failed
        O->>DB: status RECOVERING
        O->>O: ladder, routed by the error code
    end

    O->>B: screenshot()
    O->>DB: step + decision + verification + attempts (one transaction)
    O->>W: step event (after the commit)
    O->>DB: status PLANNING
```

Two orderings in that diagram are load-bearing and were each argued about in a
phase note.

**The screenshot goes to storage before the row is written**, so the row can
carry a reference that already resolves. The reverse order leaves a committed row
pointing at a file that does not exist.

**The step event is published after the commit**, never inside the transaction.
A client told about a step it can never fetch again is worse than a client told
slightly late.

## 4. Task state machine

Generated from `app/domain/state_machine.py`. Illegal transitions raise rather
than being logged and ignored.

```mermaid
stateDiagram-v2
    [*] --> SUBMITTED

    SUBMITTED --> PLANNING
    SUBMITTED --> EXECUTION_FAILED: browser will not launch,<br/>or left queued by a crash

    PLANNING --> ACTING
    PLANNING --> AWAITING_APPROVAL: HIGH risk
    PLANNING --> COMPLETED: planner decides done
    PLANNING --> EXECUTION_FAILED: no valid decision

    ACTING --> VERIFYING
    ACTING --> EXECUTION_FAILED

    VERIFYING --> PLANNING: verdict ok, more to do
    VERIFYING --> RECOVERING: verdict failed
    VERIFYING --> COMPLETED
    VERIFYING --> EXECUTION_FAILED

    RECOVERING --> ACTING: a rung re-acts
    RECOVERING --> VERIFYING: a rung re-verifies
    RECOVERING --> EXECUTION_FAILED: ladder exhausted

    AWAITING_APPROVAL --> ACTING: approved
    AWAITING_APPROVAL --> REJECTED: a person said no

    COMPLETED --> [*]
    EXECUTION_FAILED --> [*]
    REJECTED --> [*]
    CANCELLED --> [*]
    TIMED_OUT --> [*]
```

`CANCELLED` and `TIMED_OUT` are reachable from **every** non-terminal state and
are left off the diagram's edges for legibility: drawing them would add ten
arrows and say one thing. A user may cancel at any point, either budget may
expire mid-step, and an unanswered approval expires to `TIMED_OUT` rather than
`REJECTED` — `REJECTED` means a person looked and said no (decision A4).

## 5. Deployment

What `make up` starts, and what the full stack adds.

```mermaid
graph TB
    subgraph host["Developer machine or one host"]
        subgraph compose["docker-compose.yml &mdash; IMPLEMENTED"]
            apic["api<br/>Dockerfile target: api<br/>:8000"]
            pg[("postgres:16<br/>:5433<br/>agent_core, agent_core_test, payments")]
            vol1[["api_storage<br/>screenshots"]]
            vol2[["api_keys<br/>RS256 keypair"]]
        end

        subgraph full["deploy/docker-compose.full.yml adds &mdash; NOT BUILT"]
            fe["frontend<br/>:3000"]
            pay["payments<br/>:8100"]
        end
    end

    ci["GitHub Actions<br/>.github/workflows/ci.yml"]

    apic --> pg
    apic --> vol1
    apic --> vol2
    fe -.-> apic
    apic -.-> pay
    pay -.-> pg
    ci -->|"builds, starts,<br/>health-checks"| compose
```

The API container runs migrations and generates a signing key on first start, so
`make up` on an empty machine produces a working service without a setup step.
The key lives on a volume: regenerating it per restart would invalidate every
token already issued.

**Not built:** any deployment beyond one host. CI builds the images and throws
them away — there is no registry, and nothing orchestrates more than one replica.
The application is written to tolerate several (`FOR UPDATE SKIP LOCKED` on the
outbox, Alembic serialising on its own version row), but three things are
explicitly per process and documented as such: the event bus, the rate limiter
and the WebSocket ticket store.

## What is implemented, in one table

| Area | State | Where |
|---|---|---|
| Auth, RBAC, refresh rotation | Implemented | `app/api/v1/auth.py`, `app/services/auth.py` |
| Task lifecycle and API | Implemented | `app/api/v1/tasks.py`, `app/domain/state_machine.py` |
| Browser driver, multi-tab | Implemented | `app/agent/browser/` |
| Perception and summarisation | Implemented | `app/agent/perception/` |
| Planner, four providers | Implemented | `app/agent/planner/`, `app/agent/llm/` |
| Visual grounding (set-of-marks) | Implemented | `app/agent/vision/` |
| Deterministic verification | Implemented | `app/agent/verification/verifier.py` |
| Recovery ladder, error-code routing | Implemented | `app/agent/recovery/` |
| Risk policy and human approval | Implemented | `app/agent/safety/` |
| Flow cache and replay | Implemented | `app/agent/cache/` |
| Live WebSocket stream | Implemented | `app/api/v1/stream.py`, `app/services/stream.py` |
| Metrics and stats | Implemented | `app/core/metrics.py`, `app/services/stats.py` |
| Billing client and outbox | Implemented (client side) | `app/services/billing*.py` |
| SkyFare test site, nine chaos toggles (chaos suite and benchmark only, not deployed) | Implemented | `tests/skyfare/` |
| Docker images, compose, CI | Implemented | `Dockerfile`, `docker-compose.yml`, `.github/` |
| Payments service | **Not built** | separate repository |
| Frontend | **Not built** | separate repository |
| S3 screenshot storage | **Not built** | `ScreenshotStorage` protocol is ready for it |
| Redis rate limiting, shared event bus | **Not built** | both behind interfaces |
| Deployment beyond one host | **Not built** | no registry, no orchestration |

## Where the numbers are

`docs/benchmarks/2026-09-28-mock.md` is the headline run: thirty runs, ten chaos
configurations, 100% end-to-end success and 100% recovery against targets of 80%
and 70%. It uses the mock provider, which is rule-based and offline, so it
measures the loop, the verifier and the recovery ladder rather than a model's
judgement.

`docs/benchmarks/2026-09-29-gemini.md` is a live attempt that produced no usable
measurement: the provider answered every request with 503. It is kept because
what it does show is the degradation path working — five clean failures with a
readable reason and no crash.
