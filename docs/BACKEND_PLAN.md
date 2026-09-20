# BACKEND_PLAN.md: browser-agent-backend (AI-07)

Plan of record for the backend repo. Derived solely from [PROJECT_SPEC.md](PROJECT_SPEC.md); `docs/reference/` is empty, so nothing here is sourced from the original PRD or Complete Documentation PDFs. Section 4 lists everything I could not resolve from the spec alone. Nothing in section 4 has been decided.

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
10. Cache verified action sequences by task signature, replay them on an exact match, settle credit usage on terminal states, and expose health and Prometheus metrics for the KPIs in spec section 9.

## 2. Folder structure

Module paths that spec section 4.1 pins (`agent/browser/driver.py`, `services/task_runner.py`, `domain/state_machine.py`, and the rest) are reproduced exactly. See ambiguity A1 on the package root.

```text
browser-agent-backend/
├─ .github/workflows/ci.yml          lint, type-check, test, build
├─ agent/
│  ├─ orchestrator.py                the loop, state transitions, event publishing
│  ├─ schemas.py                     Pydantic models for LLM structured output
│  ├─ browser/
│  │  ├─ driver.py                   isolated context, action execution, state capture
│  │  ├─ sessions.py                 multi-tab tracking, popup capture, tab ids
│  │  └─ actions.py                  action allow-list and dispatch
│  ├─ perception/
│  │  ├─ collector.js                injected DOM collector
│  │  ├─ dom_extractor.py            run collector, stamp data-agent-idx
│  │  ├─ summarizer.py               token-budgeted page summary
│  │  └─ bs4_fallback.py             static extraction path
│  ├─ vision/grounding.py            set-of-marks annotation, vision call, bbox
│  ├─ planner/
│  │  ├─ planner.py                  prompt build, LLM call, validation, repair retry
│  │  └─ prompts/                    versioned prompt templates
│  ├─ llm/
│  │  ├─ base.py                     provider-agnostic, vision-capable interface
│  │  ├─ anthropic.py openai.py gemini.py mock.py
│  │  └─ registry.py                 provider selection from config
│  ├─ safety/
│  │  ├─ risk.py                     deterministic plus LLM risk scoring
│  │  └─ injection.py                page_state delimiting, hidden-element rules
│  ├─ verification/verifier.py       deterministic outcome checks
│  ├─ recovery/
│  │  ├─ strategies.py               the seven ordered strategies
│  │  └─ recovery.py                 bounded retry driver
│  └─ cache/replay.py                signature, store, verified replay
├─ api/
│  ├─ deps.py                        auth, RBAC, pagination dependencies
│  ├─ errors.py                      error envelope and exception handlers
│  ├─ ops.py                         health, metrics, JWKS (root paths)
│  └─ v1/
│     ├─ auth.py tasks.py steps.py approvals.py cache.py grounding.py ws.py
├─ core/
│  ├─ config.py                      pydantic-settings, env schema
│  ├─ logging.py                     structlog, request ids
│  ├─ redaction.py                   password and card-like value scrubbing
│  ├─ security.py                    hashing, token issue and verify
│  ├─ jwt_keys.py                    RS256 keypair load, JWKS document
│  ├─ middleware.py                  CORS, secure headers, request context
│  └─ rate_limit.py                  auth and task-creation limits
├─ db/
│  ├─ base.py session.py
│  ├─ models/                        the nine tables from spec section 5
│  └─ repositories/                  task, step, approval, cache, audit
├─ domain/
│  ├─ enums.py                       statuses, actions, risk levels
│  └─ state_machine.py               legal transitions, raises on illegal
├─ schemas/                          request and response models
├─ services/
│  ├─ task_service.py                task lifecycle orchestration
│  ├─ task_runner.py                 asyncio background execution, semaphore, cancel
│  ├─ approval_service.py            approval lifecycle and timeout
│  ├─ screenshot_store.py            local FS now, S3/MinIO behind the same interface
│  ├─ payments_client.py             reserve, settle, release, BILLING_ENABLED
│  ├─ events.py                      WS event bus, seq allocation, replay
│  └─ metrics.py                     Prometheus collectors for section 9 KPIs
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

## 3. Parts B01 to B31

Build order is dependency order. Every part ships its own tests and leaves `main` green; see A2 on the TEST-001 to TEST-010 mapping.

### Foundation

| Part | Delivers |
|---|---|
| B01 | Repo scaffolding: uv, pyproject, ruff, mypy, Makefile, `.env.example`, FastAPI entrypoint, Dockerfile, compose with Postgres, CI workflow. |
| B02 | `core/config.py`: typed settings for every env var, fail-fast validation at startup, no secret ever defaulted in code. |
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
| B10 | CORS locked to the frontend origin, secure headers, and rate limiting on auth and task creation. |

### Domain and task API

| Part | Delivers |
|---|---|
| B11 | `domain/enums.py` and `domain/state_machine.py`: every status, the legal transition table, and hard failure on illegal moves. |
| B12 | Task repository, `task_number` allocation, and `POST /tasks` including the credit reservation and its 402 path. |
| B13 | `GET /tasks` with status, date-range, query and pagination filters, `GET /tasks/{id}`, and `POST /tasks/{id}/cancel`. |
| B14 | Step persistence and `GET /tasks/{id}/steps` plus `GET /tasks/{id}/steps/{stepId}` with its decision, verification, and recovery detail. |
| B15 | `screenshot_store.py` with task-scoped keys, and `GET /tasks/{id}/steps/{stepId}/screenshot` served only to the owner. |
| B16 | Approvals: `POST /tasks/{id}/approve`, `POST /tasks/{id}/reject`, RBAC, and the approval timeout job. |
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
| B30 | `orchestrator.py` and `services/task_runner.py`: the section 4.2 loop, budgets, concurrency semaphore, cancellation, credit settlement. |
| B31 | `cache/replay.py` signature and verified replay, the SkyFare demo site with its chaos toggles, and the agent evaluation and chaos test suites. |

## 4. Ambiguities and contradictions

Nothing below is decided. Each carries a proposal, and I will take the ones you confirm.

### Blocking (I need an answer before the part that touches them)

**A1. The spec pins module paths with no package root.** Section 4.1 says `agent/browser/driver.py`, not `app/agent/browser/driver.py`. Taken literally, `agent`, `services`, `domain` and `core` become top-level importable packages, which is faithful but risks colliding with installed distributions. *Proposal: keep the literal top-level layout as drawn in section 2. Alternative is an `app/` prefix and a stated mapping. Affects B01.*

**A2. TEST-001 to TEST-010 are referenced but not defined anywhere I can read.** Spec section 11 requires each to map to at least one automated test, but only TEST-006 is identifiable, from the aside in section 4.5. `docs/reference/` is empty. *Proposal: I draft ten candidate IDs from the spec's own requirements and you confirm or replace them before B31. I cannot claim compliance against IDs I have not seen. Affects every part's test section.*

**A3. `RECOVERING` is called a "UI-visible sub-state" but `tasks.status` has no defined value set.** It is unclear whether `RECOVERING` and `VERIFYING` are persisted statuses or transient states that only appear on the WebSocket. *Proposal: persist the full set, including `RECOVERING`, so a reconnecting client and the task detail screen agree with the live view. Affects B11.*

**A4. An expired approval has no defined task outcome.** `approvals.approval_status` includes `EXPIRED`, and the terminal task states include both `REJECTED` and `TIMED_OUT`, but the spec does not say which one an `APPROVAL_TIMEOUT_SECONDS` expiry produces. *Proposal: approval `EXPIRED` drives the task to `TIMED_OUT`, reserving `REJECTED` for an explicit human rejection. Affects B11 and B16.*

**A5. `reservations.task_id` is unique, but `POST /tasks/{id}/replay` re-runs the same task.** A replay of an already-settled task cannot create a second reservation for that `task_id` without violating the constraint. *Proposal: replay settles against the original reservation if still HELD, otherwise opens a new reservation keyed by a replay attempt id. This needs agreement with whoever owns the payments repo, since the constraint lives there. Affects B12 and B31.*

**A6. The signup grant and the reservation size leave a new user exactly one task.** Signup grants 50 credits; `POST /tasks` reserves `MAX_STEPS × 2` = 50. Billing is 1 credit per step, so the 2x reservation looks like deliberate headroom for vision steps, but the result is that a new user's first task consumes their entire grant. *Proposal: confirm this is intended. Options are to raise the grant, reserve `MAX_STEPS × 1` plus a vision allowance, or leave it. Affects B12.*

### Non-blocking (I will proceed on the proposal unless you say otherwise)

**A7. `MAX_TIME` in the section 4.2 pseudo-code versus `MAX_TASK_SECONDS` in the defaults.** Same budget, two names. *Proposal: `MAX_TASK_SECONDS` is canonical.*

**A8. `task_steps.status` values are never enumerated,** unlike task status. *Proposal: `PENDING`, `AWAITING_APPROVAL`, `EXECUTING`, `VERIFYING`, `RECOVERING`, `VERIFIED`, `FAILED`, `SKIPPED`.*

**A9. `/health` and `/metrics` appear under the heading "Backend API (`/api/v1`)".** JWKS is explicitly called out as a root path; these two are not. *Proposal: serve both at the root, outside the version prefix, matching normal scrape and probe conventions.*

**A10. The `step` WebSocket event embeds a base64 JPEG, and a REST screenshot endpoint also exists.** Inline images are the fastest path to the ≤1 to 2 second live target but make each event large, and reconnect replay could burst many of them at once. *Proposal: keep inline base64 for live events as specified, cap replayed events to metadata plus the REST URL, and make the cutover configurable.*

**A11. `audit_events.metadata` collides with SQLAlchemy's reserved `metadata` attribute** on declarative models. *Proposal: keep the column named `metadata` in Postgres, map it to `event_metadata` in the ORM.*

**A12. Demo sites are "inside backend repo" but served on :8200 while the API is on :8000.** *Proposal: a separate ASGI app under `demo_sites/` with its own compose service, never mounted into the main app, so chaos behaviour cannot leak into API routes.*

**A13. `MAX_CONCURRENT_TASKS=3` does not say global or per-user.** *Proposal: global process semaphore of 3, plus a separate configurable per-user cap so one user cannot occupy every slot.*

**A14. Cache invalidation counts "runs" without defining one.** Section 4.12 invalidates below 0.5 success after at least 3 runs, but a replay that fails on step 4 of 9 and falls back to the planner is neither clearly a success nor clearly a failure. *Proposal: a run is any replay attempt; success means every replayed step verified with no planner fallback.*

**A15. `POST /tasks/{id}/ground` has no defined request or response body.** *Proposal: accept an optional target hint and tab id, return the grounded index and bbox, and record it as a grounding invocation for the vision-rate KPI.*

**A16. Redaction versus execution for the `type` action.** Section 10 requires redaction "everywhere, including DB and WS", but the driver needs the real value to type it. *Proposal: the real value exists only in memory for the duration of the action; `task_steps.value_redacted` and every event carry the masked form. Worth stating explicitly since it is a security rule.*

**A17. Rate limits and page size are required but unquantified.** *Proposal: 5 per minute per IP on login and register, 10 per minute per user on task creation, page size 20 with a 100 maximum, all configurable.*

**A18. `ADR-004` (section 4.7) and `BR-008` (section 1) are cited but absent.** *Proposal: treat the spec text as the whole requirement and note the dangling reference. Flagging in case those documents carry detail the spec dropped.*

## 5. Dependencies

Grouped as they will appear in `pyproject.toml`. Everything in spec section 3 is here; the four additions the spec does not name are marked and justified.

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
| `slowapi` *(addition)* | Section 10 requires rate limiting on auth and task creation but names no library. Small, FastAPI-native, and swappable. |

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
| `anthropic`, `openai`, `google-genai` | The three providers behind `agent/llm/base.py`. All three are vision-capable, which grounding requires. |

### Observability

| Package | Why |
|---|---|
| `structlog` | Structured JSON logs with per-request context, and the place the redaction filter is enforced. |
| `prometheus-client` *(addition)* | `GET /metrics` must emit Prometheus text for the section 9 KPIs; the spec names the format but no library. |

### Demo sites

| Package | Why |
|---|---|
| `jinja2` *(addition)* | SkyFare needs server-rendered templates to support the chaos toggles (shuffled class names, layout variant B, injected hidden text). Section 12 requires the behaviour but names no templating. |

### Development and test

| Package | Why |
|---|---|
| `pytest`, `pytest-asyncio`, `pytest-cov` | The async test suite and coverage, as specified. |
| `ruff`, `mypy` | Lint and type-check, as specified, both enforced in CI. |
| `respx` *(addition)* | Mocks httpx so the payments integration can be tested without running the payments service. |
| `testcontainers[postgresql]` *(addition, optional)* | Section 11 requires integration tests against Postgres in docker. Compose alone can cover this if you would rather not add it. |

## 6. Conventions

- Every part leaves `main` green: ruff, mypy, and the full test suite pass before it is handed back.
- Every part ships its own tests. No part defers testing to B31.
- No secret is ever defaulted in code; `.env.example` documents each one.
- Snake_case in Postgres, camelCase on the wire, per spec section 1 and the section 6.1 event bodies.
- `browser-use` is never imported and never copied, per spec section 3.

## 7. Status

Awaiting review. Section 4 items A1 to A6 need answers before the parts they name. Nothing is implemented yet.
