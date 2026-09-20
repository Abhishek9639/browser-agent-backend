# BACKEND_PLAN.md: browser-agent-backend (AI-07)

Plan of record for the backend repo. Derived solely from [PROJECT_SPEC.md](PROJECT_SPEC.md); `docs/reference/` is empty, so nothing here is sourced from the original PRD or Complete Documentation PDFs.

All 18 open items from the first draft are now decided. Section 4 records each decision and where it is written down.

## 1. What the backend must do (10 lines)

1. Be the only identity provider for the system: register/login users, issue RS256 access tokens plus an httpOnly refresh cookie, and publish JWKS so the payments service can verify those tokens itself.
2. Accept a natural-language instruction and a start URL, reserve credits for it, and return a task that the caller can follow to completion.
3. Drive a real Chromium browser through Playwright in a fresh, isolated, download-disabled context per task, restricted to an allow-listed set of domains.
4. Perceive each page as a compact, token-budgeted summary of its visible interactive elements, falling back to BeautifulSoup for static extraction.
5. Ask an LLM, through a provider-agnostic adapter, for exactly one next action as structured JSON, and reject anything that fails schema, allow-list, or target-existence checks.
6. Treat all page content as untrusted input, and escalate payment-like, deletion-like, and submission-like actions to a human approver before executing them.
7. Execute the approved action, then prove the outcome with a deterministic verifier rather than trusting the model's claim.
8. On a failed verification, work through seven ordered recovery strategies within a bounded retry budget before failing the step.
9. Persist every step, decision, verification, and recovery attempt, then stream them live over WebSocket with gap-free ordering and replay-on-reconnect.
10. Cache verified action sequences by task signature, replay them into a fresh task on an exact match, settle credit usage on terminal states, and expose health and Prometheus metrics for the KPIs in spec section 9.

## 2. Folder structure

Per decision A1, the backend package root is `app/`, and the module paths pinned in spec section 4.1 are relative to it: `agent/browser/driver.py` is `app/agent/browser/driver.py` on disk. That mapping is now recorded in the spec itself.

`alembic/`, `tests/`, and `demo_sites/` sit at the repo root rather than under `app/`. The first two are not application modules, and `demo_sites/` is a separate ASGI app on its own port per decision A12.

```text
browser-agent-backend/
├─ .github/workflows/ci.yml          lint, type-check, test, build
├─ app/
│  ├─ main.py                        FastAPI app factory, router and middleware wiring
│  ├─ agent/
│  │  ├─ orchestrator.py             the loop, state transitions, event publishing
│  │  ├─ schemas.py                  Pydantic models for LLM structured output
│  │  ├─ browser/
│  │  │  ├─ driver.py                isolated context, action execution, state capture
│  │  │  ├─ sessions.py              multi-tab tracking, popup capture, tab ids
│  │  │  └─ actions.py               action allow-list and dispatch
│  │  ├─ perception/
│  │  │  ├─ collector.js             injected DOM collector
│  │  │  ├─ dom_extractor.py         run collector, stamp data-agent-idx
│  │  │  ├─ summarizer.py            token-budgeted page summary
│  │  │  └─ bs4_fallback.py          static extraction path
│  │  ├─ vision/grounding.py         set-of-marks annotation, vision call, bbox
│  │  ├─ planner/
│  │  │  ├─ planner.py               prompt build, LLM call, validation, repair retry
│  │  │  └─ prompts/                 versioned prompt templates
│  │  ├─ llm/
│  │  │  ├─ base.py                  provider-agnostic, vision-capable interface
│  │  │  ├─ anthropic.py openai.py gemini.py mock.py
│  │  │  └─ registry.py              provider selection from config
│  │  ├─ safety/
│  │  │  ├─ risk.py                  deterministic plus LLM risk scoring
│  │  │  └─ injection.py             page_state delimiting, hidden-element rules
│  │  ├─ verification/verifier.py    deterministic outcome checks
│  │  ├─ recovery/
│  │  │  ├─ strategies.py            the seven ordered strategies
│  │  │  └─ recovery.py              bounded retry driver
│  │  └─ cache/replay.py             signature, store, verified replay into a new task
│  ├─ api/
│  │  ├─ deps.py                     auth, RBAC, pagination dependencies
│  │  ├─ errors.py                   error envelope and exception handlers
│  │  ├─ ops.py                      JWKS and /metrics (root paths)
│  │  └─ v1/
│  │     ├─ auth.py tasks.py steps.py approvals.py cache.py grounding.py ws.py
│  │     └─ health.py                /api/v1/health
│  ├─ core/
│  │  ├─ config.py                   pydantic-settings, env schema
│  │  ├─ logging.py                  structlog, request ids
│  │  ├─ redaction.py                password and card-like value scrubbing
│  │  ├─ security.py                 hashing, token issue and verify
│  │  ├─ jwt_keys.py                 RS256 keypair load, JWKS document
│  │  ├─ middleware.py               CORS, secure headers, request context
│  │  └─ rate_limit.py               RateLimiter interface, in-memory token bucket
│  ├─ db/
│  │  ├─ base.py session.py
│  │  ├─ models/                     the nine tables from spec section 5
│  │  └─ repositories/               task, step, approval, cache, audit
│  ├─ domain/
│  │  ├─ enums.py                    statuses, actions, risk levels
│  │  └─ state_machine.py            legal transitions, raises on illegal
│  ├─ schemas/                       request and response models
│  └─ services/
│     ├─ task_service.py             task lifecycle orchestration
│     ├─ task_runner.py              asyncio background execution, semaphore, cancel
│     ├─ approval_service.py         approval lifecycle and timeout
│     ├─ screenshot_store.py         local FS now, S3/MinIO behind the same interface
│     ├─ payments_client.py          reserve, settle, release, BILLING_ENABLED
│     ├─ events.py                   WS event bus, seq allocation, replay
│     └─ metrics.py                  Prometheus collectors for section 9 KPIs
├─ alembic/                          migrations
├─ demo_sites/skyfare/               mock flight site plus chaos toggles, port 8200
├─ tests/
│  ├─ unit/ integration/ api/ ws/ agent_eval/ chaos/
│  └─ conftest.py fixtures/
├─ docker/ Dockerfile docker-compose.yml
├─ docs/                             PROJECT_SPEC.md, BACKEND_PLAN.md
├─ pyproject.toml uv.lock Makefile .env.example
└─ README.md
```

## 3. Parts

**The numbering below is indicative only. You supply each part one at a time, and the part id in your prompt is authoritative. Where the two disagree, your prompt wins and this section is updated to match.**

It is kept as a dependency-ordered sketch of the whole build, so scope that has no home yet stays visible.

### Foundation

| Part | Delivers |
|---|---|
| B01 | Repo scaffolding: uv, pyproject, ruff, mypy, Makefile, `.env.example`, FastAPI entrypoint, Dockerfile, compose with Postgres, CI workflow. |
| B02 | `app/core/config.py`: typed settings for every env var, fail-fast validation at startup, no secret ever defaulted in code. |
| B03 | structlog JSON logging, per-request ids, and the redaction filter that keeps page content, screenshots, and credentials out of logs. |
| B04 | The error envelope, exception handlers, and the 400/401/402/403/404/409/422/429/5xx status-code map from spec section 6. |
| B05 | SQLAlchemy 2 async engine, session dependency, declarative base, and the nine ORM models with their documented indexes. |
| B06 | Alembic wiring plus the initial migration, verified by an up/down round trip against a throwaway Postgres. |

### Auth and edge

| Part | Delivers |
|---|---|
| B07 | Password hashing, user repository, `POST /auth/register`, `POST /auth/login`, and the signup credit grant hook. |
| B08 | RS256 keypair handling, 15 minute access tokens, and `GET /.well-known/jwks.json` at the root path. |
| B09 | Refresh cookie issuance and rotation, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, and the RBAC dependencies. |
| B10 | CORS locked to the frontend origin, secure headers, and the `RateLimiter` interface with its in-memory token bucket applied to auth and task creation. |

### Domain and task API

| Part | Delivers |
|---|---|
| B11 | `app/domain/enums.py` and `state_machine.py`: the full persisted status set, the legal transition table, and hard failure on illegal moves. |
| B12 | Task repository, `task_number` allocation, and `POST /tasks` including the credit reservation and its 402 path. |
| B13 | `GET /tasks` with status, date-range, query and pagination filters, `GET /tasks/{id}`, and `POST /tasks/{id}/cancel`. |
| B14 | Step persistence and `GET /tasks/{id}/steps` plus `GET /tasks/{id}/steps/{stepId}` with its decision, verification, and recovery detail. |
| B15 | `screenshot_store.py` with task-scoped keys, and `GET /tasks/{id}/steps/{stepId}/screenshot` served only to the owner. |
| B16 | Approvals: `POST /tasks/{id}/approve`, `POST /tasks/{id}/reject`, RBAC, and the timeout job that expires an approval and times the task out. |
| B17 | `POST /tasks/{id}/stream-ticket` with single-use 60 second tickets and the WebSocket handshake that consumes them. |
| B18 | The event bus: seq allocation, persist-before-publish, `after_step` replay from the DB, heartbeat, and client `ping`. |

### Agent core

| Part | Delivers |
|---|---|
| B19 | `driver.py`: per-task isolated context, downloads disabled, domain allow-list, action execution, before/after state capture. |
| B20 | `sessions.py`: tab registry, stable `tabId` values, popup and new-tab capture, `new_tab`/`switch_tab`/`close_tab`. |
| B21 | `collector.js` and `dom_extractor.py`: visible interactive element extraction, `data-agent-idx` stamping, hidden-element exclusion. |
| B22 | `summarizer.py` with the ~3k token budget and in-viewport/forms/links priority, plus the `bs4_fallback.py` static path. |
| B23 | `llm/base.py` provider-agnostic vision-capable interface and the `mock` provider that makes the loop testable without quota. |
| B24 | The `anthropic`, `openai`, and `gemini` adapters behind that interface, with token and latency accounting. |
| B25 | `planner.py`, versioned prompts, Pydantic validation of the section 4.5 schema, and the single repair retry (TEST-006). |
| B26 | `grounding.py`: set-of-marks annotation with Pillow, vision call, index validation, and DOM-only degradation when vision is down. |
| B27 | `risk.py` and `injection.py`: the configurable HIGH-risk matcher, `max(deterministic, LLM)`, and the `<page_state>` untrusted-data envelope. |
| B28 | `verifier.py`: the six deterministic outcome checks and the `verifications` rows they write. |
| B29 | `strategies.py` and `recovery.py`: the seven ordered strategies, bounded retries, and a `recovery_attempts` row per attempt. |

### Orchestration and delivery

| Part | Delivers |
|---|---|
| B30 | `orchestrator.py` and `services/task_runner.py`: the section 4.2 loop, budgets, global and per-user concurrency caps, cancellation, credit settlement. |
| B31 | `cache/replay.py` signature and new-task replay, the SkyFare demo site with its chaos toggles, and the agent evaluation and chaos test suites. |

## 4. Decisions

All 18 items from the first draft are resolved. "Spec" marks the ones now written into PROJECT_SPEC.md as well.

### Blocking items

| Ref | Decision | Spec |
|---|---|---|
| A1 | Package root is `app/`. Spec section 4.1 paths are relative to it, so `agent/browser/driver.py` is `app/agent/browser/driver.py`. | §4.1 |
| A2 | The ten test IDs are recorded verbatim as a table, so they survive the missing reference PDFs. TEST-001 to TEST-008 and TEST-010 are High, TEST-009 is Medium. | §11 |
| A3 | Persist the full status set including `RECOVERING` and `VERIFYING`, so a reconnecting client and the task detail screen agree with the live view. | plan |
| A4 | An approval hitting `APPROVAL_TIMEOUT_SECONDS` is recorded `EXPIRED` and drives the task to `TIMED_OUT` with `failure_reason` "approval timed out". `REJECTED` is only for an explicit human rejection. | §4.3 |
| A5 | `POST /tasks/{id}/replay` always creates a new task row and returns it; it never re-runs the original task id. `reservations.task_id` stays unique and there is no cross-repo conflict. | §6 |
| A6 | Signup grant rises to 200 credits, roughly four tasks of headroom. The reservation stays at `MAX_STEPS × 2`. | §7 |

### Non-blocking items

| Ref | Decision |
|---|---|
| A7 | `MAX_TASK_SECONDS` is the canonical name for the task time budget; `MAX_TIME` in the section 4.2 pseudo-code refers to it. |
| A8 | `task_steps.status` values are `PENDING`, `AWAITING_APPROVAL`, `EXECUTING`, `VERIFYING`, `RECOVERING`, `VERIFIED`, `FAILED`, `SKIPPED`. |
| A9 | Health stays at `/api/v1/health`, since Docker and the frontend both use it. `/metrics` is served at the root, internal and admin only. |
| A10 | Live `step` events carry inline base64 JPEG as specified. Replayed events carry metadata plus the REST screenshot URL instead, with the cutover configurable, so a reconnect cannot burst a run's worth of images. |
| A11 | `audit_events.metadata` keeps that column name in Postgres and maps to `event_metadata` on the ORM model, avoiding SQLAlchemy's reserved attribute. |
| A12 | `demo_sites/` is a separate ASGI app with its own compose service on :8200, never mounted into the main app, so chaos behaviour cannot leak into API routes. |
| A13 | Concurrency is a global semaphore of 3 plus a per-user cap defaulting to 2. Both configurable. |
| A14 | A cache "run" is any replay attempt. Success means every replayed step verified with no planner fallback. That feeds `success_rate` and the below-0.5-after-3-runs invalidation. |
| A15 | `POST /tasks/{id}/ground` accepts an optional target hint and tab id, returns the grounded index and bbox, and records a grounding invocation for the vision-rate KPI. |
| A16 | The real value for a `type` action exists only in memory for the duration of that action. `task_steps.value_redacted` and every event carry the masked form. |
| A17 | Rate limits: 5/min per IP on login and register, 10/min per user on task creation. Page size 20, maximum 100. All configurable. |
| A18 | `ADR-004` and `BR-008` are cited in the spec but absent from `docs/reference/`. The spec text is treated as the whole requirement; the dangling references are noted, not chased. |

## 5. Dependencies

Grouped as they will appear in `pyproject.toml`. Two capabilities the spec requires are built in-house rather than pulled in, per your decision; both are called out below.

### Runtime and web

| Package | Why |
|---|---|
| `fastapi` | The API, WebSocket routing, dependency injection, and the OpenAPI document the frontend generates its types from. |
| `uvicorn[standard]` | ASGI server; the `standard` extra brings the WebSocket and HTTP tooling the live stream needs. |
| `pydantic` v2 | Request, response, and LLM output validation. Section 4.5 explicitly requires the structured output be Pydantic-validated. |
| `pydantic-settings` | Typed environment configuration, so a missing secret fails at startup rather than mid-task. |
| `httpx` | Async client for the payments internal API and any outbound provider call not covered by a vendor SDK. |

### Data

| Package | Why |
|---|---|
| `sqlalchemy` 2 (async) | ORM for the nine tables, with the async session the agent loop needs to persist steps without blocking. |
| `asyncpg` | The async Postgres driver SQLAlchemy runs on. |
| `alembic` | Versioned migrations, so the schema is reproducible across the three environments. |

### Auth and security

| Package | Why |
|---|---|
| `pyjwt` | Issue and verify the RS256 access tokens. |
| `cryptography` | Supplies the RSA primitives PyJWT needs for RS256 and for building the JWKS document. |
| `passlib[argon2]` | Password hashing with a modern KDF; argon2 is the first option the spec names. |

Rate limiting is **not** a dependency. `app/core/rate_limit.py` defines a small `RateLimiter` interface with an in-memory token-bucket implementation behind it, so the algorithm is explainable end to end and a Redis-backed implementation can replace it later without touching call sites.

### Browser and perception

| Package | Why |
|---|---|
| `playwright` | Drives Chromium: isolated contexts, locators for recovery strategies 2 to 4, screenshots. |
| `beautifulsoup4` | The static extraction fallback for `extract` actions, per section 4.6. |
| `lxml` | Fast parser backing BeautifulSoup on large pages inside the perception budget. |
| `pillow` | Set-of-marks annotation and the JPEG compression the WebSocket `step` event specifies. |

### LLM

| Package | Why |
|---|---|
| `anthropic`, `openai`, `google-genai` | The three providers behind `app/agent/llm/base.py`. All three are vision-capable, which grounding requires. |

### Observability

| Package | Why |
|---|---|
| `structlog` | Structured JSON logs with per-request context, and the place the redaction filter is enforced. |
| `prometheus-client` | `GET /metrics` must emit Prometheus text for the section 9 KPIs; the spec names the format but no library. |

### Demo sites

| Package | Why |
|---|---|
| `jinja2` | SkyFare needs server-rendered templates to support the chaos toggles (shuffled class names, layout variant B, injected hidden text). Section 12 requires the behaviour but names no templating. |

### Development and test

| Package | Why |
|---|---|
| `pytest`, `pytest-asyncio`, `pytest-cov` | The async test suite and coverage, as specified. |
| `ruff`, `mypy` | Lint and type-check, as specified, both enforced in CI. |
| `respx` | Mocks httpx so the payments integration can be tested without running the payments service. |

Integration tests run against the docker compose Postgres using a separate test database, created and dropped per run. No `testcontainers`.

## 6. Conventions

- Every part leaves `main` green: ruff, mypy, and the full test suite pass before it is handed back.
- Every part ships its own tests. No part defers testing to a later one.
- Each of TEST-001 to TEST-010 is claimed by the part that makes it testable, and the mapping is kept current in this document.
- No secret is ever defaulted in code; `.env.example` documents each one.
- Snake_case in Postgres, camelCase on the wire, per spec section 1 and the section 6.1 event bodies.
- `browser-use` is never imported and never copied, per spec section 3.

## 7. Status

All 18 decisions recorded; five are now written into the spec (§4.1, §4.3, §6, §7, §11). Nothing is implemented yet. Awaiting the first part prompt.
