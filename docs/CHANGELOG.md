# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

### B18 - SkyFare demo site

Added

- `demo_sites/skyfare/`: a mock airline search site on port 8200, served by `make skyfare`. Standalone, and nothing in `app/` imports it. Search with autocomplete over ten Indian cities, a results list with sort, stop and airline filters and load-more, a flight detail page, a passenger form ending at a "Pay now" button, and a payment page that makes clear nothing was charged.
- Deterministic generation: 15 to 30 flights seeded from the origin, destination and date, hashed with sha256 rather than `hash()`, which is salted per process and would give different flights after every restart. The same search returns the same flights on any machine, which is what makes an agent run scorable.
- Nine chaos toggles, set per request with `?chaos=` or for a session with `SKYFARE_CHAOS`: `delay`, `cookie`, `popup`, `shuffle`, `layoutb`, `iframe`, `newtab`, `inject`, `slowtype`, plus `all`. Toggles carry through every link on the page, so one that switches itself off on the first click cannot happen.
- `GET /api/cheapest`: ground truth for scoring a run, behind a header and returning 404 without it, so an agent that guesses the path learns nothing from trying.
- README section with screenshots of the search, results, detail, booking and chaos pages.
- `127.0.0.1:8200` added to the allowed domains, and SkyFare named in the comment next to them.
- 46 tests over the site, driven through ASGI rather than a browser.

Fixed

- The collector masked any field whose name contained `pass`, `pin` or `card` as a substring, so a passenger count read as `••••` and a shipping address was treated as a secret. Known benign words are now stripped from the hint before the secret check runs, which keeps `cardNumber` and `newCreditCard` masked while leaving `passengers` alone.
- README status was still describing the repository as of B06.

### B17 - Task runner and task API

Added

- `app/services/task_runner.py`: one runner per process, started and stopped by the lifespan. Owns Playwright and the Chromium it launches, a semaphore for `AGENT_MAX_CONCURRENT_TASKS`, and the per-task cancel flags. A queued task stays `SUBMITTED` because the orchestrator writes `PLANNING` as its first act. The browser is shared and the context is per task, so one run's cookies and storage cannot reach another's.
- Cooperative cancellation: `CancelSignal` in `app/agent/contracts.py`, checked by the orchestrator at phase boundaries, plus closing the task's browser context so an action already in flight fails at once. Writing `CANCELLED` to the row is what actually stops the task; the signal only makes it prompt.
- Shutdown cancels running tasks and marks anything that did not settle `CANCELLED` with reason "server shutdown". Startup finishes off tasks abandoned by a crash: `EXECUTION_FAILED` with reason "interrupted by restart", or `TIMED_OUT` for one left `AWAITING_APPROVAL`, since nobody answered.
- `app/services/task_service.py`: instruction normalisation, the start-URL allow-list check, duplicate detection, ownership, and the cancel rules. Routers hold no logic.
- `app/api/v1/tasks.py`: `POST /tasks`, `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `GET /tasks/{id}/steps`, `GET /tasks/{id}/steps/{stepId}` and `GET /tasks/{id}/steps/{stepId}/screenshot`, each with a summary, description and error responses.
- `app/schemas/task.py`: camelCase request and response models with examples on every field, since the frontend generates its TypeScript from `/openapi.json`.
- Duplicate protection: the same user submitting the same normalised instruction while an earlier one is still live gets 409 with the existing task id in the message.
- Screenshots are served only to the owner or an admin, as `image/jpeg` with `Cache-Control: private, max-age=0, no-store`.
- An after-commit hook list on the request session, so work handed to a background worker starts only once the row it will read has been committed.
- `SUBMITTED -> EXECUTION_FAILED` in the task lifecycle, for a task whose browser will not launch and one left queued by a crash. Without it neither had a legal terminal state.
- `make check-all` (lint, typecheck, unit and integration) as the gate before a pull request, with `make check` unchanged as the fast loop.
- 52 tests.

Fixed

- The loop discarded whatever an `extract` action read. `ActionResult.extracted` now reaches the planner through `StepSummary`, capped so a page of text cannot crowd out the current page state. Before this an agent would read the results list, learn nothing, and read it again until the step budget ran out.
- `GET /tasks/{id}/steps/{stepId}` tried to lazy-load three `lazy="raise"` relationships while serialising. Built explicitly from the pieces the service loaded instead.

### B16 - Orchestrator core loop

Added

- `app/agent/orchestrator.py`: the spec section 4.2 loop. Perceive, plan, ground, assess risk, act, verify, record, repeat, ending at `COMPLETED`, `EXECUTION_FAILED`, `TIMED_OUT`, `REJECTED` or `CANCELLED`. Every status change goes through the state machine and publishes a `status` event; a `final` event is published whatever happens, including when the settling write itself fails.
- `app/agent/contracts.py`: the collaborators the loop depends on, each a protocol with a do-nothing default. Verification, recovery, visual grounding, risk policy, approval gate, flow cache and session manager. The approval gate is the exception: its default refuses, because a stub that waves high-risk actions through is a hole that looks like working software.
- `app/agent/browser/agent_browser.py`: `DriverBrowser`, binding the Playwright driver to what the loop asks for (perceive, screenshot, capture state, execute).
- Per step, in one transaction: the screenshot goes to storage first, then `task_steps`, `llm_decisions` and `verifications`, then commit. The `step` event is published only after the commit, so the database is never behind the live view and a reconnecting client can always be caught up.
- `RiskLevel.rank` and `RiskLevel.highest`, because these are strings and `max(HIGH, MEDIUM)` compares alphabetically and answers `MEDIUM`.
- Values typed into password or masked fields are stored and sent as the mask, in `task_steps.value_redacted` and in `llm_decisions.raw_output`, which carries the model's own copy of the value.
- Failure reasons are written by us, never taken from an exception message: a message can carry a URL with a token in it.
- `docs/agent.md`: a sequence diagram of one iteration, why the write order is what it is, and a table of what is still a stand-in.
- 43 tests, 34 unit against fakes and 9 integration against real Postgres and real Chromium.

Fixed

- The whole integration suite. The settings isolation added in B14 also hid `.env` from `integration_settings`, so every integration test tried to reach a Postgres that is not there. Integration settings now read the real environment through a `real_environment` helper, and `api_app` takes its database URL from them.

### B15 - Task state machine and event bus

Added

- `app/domain/state_machine.py`: the spec section 4.3 lifecycle as one table. `TRANSITIONS`, `allowed_from`, `can_transition`, `is_terminal`, and `transition(task, new_status)` which returns the previous status because that is what the section 6.1 `status` event carries. An illegal move raises `IllegalTransition` naming what was allowed instead. `CANCELLED` and `TIMED_OUT` are reachable from every live status; terminal statuses have no outgoing edges at all.
- `app/services/events.py`: frozen, camelCase models for all eight section 6.1 event types, sharing `type`, `taskId`, `seq` and `timestamp`.
- `EventBus`, an abstract base class, and `InMemoryEventBus`. `publish` stamps a per-task monotonically increasing `seq` and fans out without blocking; `subscribe` returns a bounded buffer that is an async iterator and an async context manager.
- Load shedding for a subscriber that falls behind, on two separate budgets: screenshots are stripped from the oldest steps first, whole droppable events go only once the item budget is also full, and `status`, `approval_required`, `approval_resolved`, `final` and `error` are never dropped. An approval keeps its screenshot.
- `docs/agent.md`: the lifecycle as a Mermaid state diagram, the event table, how to subscribe, and what gives way under load.
- 106 unit tests.

### B14 - Planner, prompt registry, eval harness

Added

- `app/agent/planner/schema.py`: the `Decision` model from spec section 4.5, camelCase on the wire, frozen, rejecting unknown fields. Cross-field rules: click/type/select need `targetIndex`, type and select need a value, navigate needs an http or https URL, done needs `finalAnswer`, switch_tab needs `tabId`. `DECISION_JSON_SCHEMA` is hand-written for provider strict modes, with a test asserting it cannot drift from the model.
- `app/agent/planner/prompts/planner_v1.md`: the system prompt. Covers the role, using only supplied state, never inventing an element or a value, the three kinds of knowledge (observed state, retrieved hint, assumption), flagging insufficiency through `needsVision` or low confidence, approval for anything irreversible, one action per turn, an `expectedOutcome` on every decision, short reasoning citing observable evidence, and that content inside `<page_state>` is untrusted data whose instructions must be ignored.
- `app/agent/planner/registry.py`: `PromptRegistry` loading prompts by version from `PLANNER_PROMPT_VERSION`, recorded with every decision.
- `app/agent/planner/planner.py`: `Planner.decide(goal, page_state, history, cached_hint)` returning `PlannerResult`. Builds goal, compressed history, optional hint and fenced page state; validates against the schema; checks the named index exists in the page state; buys exactly one repair turn with the error fed back, then raises `PlannerOutputError` (TEST-006).
- `compress_history`: the last eight steps in full, everything earlier as one counted line.
- `tests/eval/planner_cases.yaml`: 22 labelled cases over 11 fixture pages, each with a set of acceptable decisions and, where it matters, an assertion on the approval flag.
- `scripts/eval_planner.py`: loads each fixture in a real browser, perceives it, and scores one planner decision. Reports action accuracy, groundedness, repair rate, unsafe values typed, average latency and tokens. Defaults to the mock provider regardless of `LLM_PROVIDER`, so a bare run costs nothing.
- Dev dependency `pyyaml` for the eval dataset.
- 71 unit tests.

Changed

- `LLM_MODEL` is now optional. Left unset, each provider supplies its own default: `claude-sonnet-5`, `gpt-6-luna`, `gemini-3.6-flash`. `.env.example` documents this and gives `gemini-3.6-flash` as the worked example.
- `LLM_PROMPT_VERSION` renamed to `PLANNER_PROMPT_VERSION`.
- Unit tests no longer read a developer's `.env` or exported variables; an autouse fixture isolates `Settings` for the whole suite.
- `scripts/**` is exempt from the no-print lint rule rather than carrying a `noqa` on every line.

### B13 - Anthropic, OpenAI and Gemini providers

Added

- Dependencies: `anthropic`, `openai`, `google-genai`.
- `app/agent/llm/anthropic.py`, `openai.py`, `gemini.py`, each implementing `LLMClient.generate_json` with JPEG images, schema-constrained JSON, token usage, latency, and SDK errors mapped onto our own types. API shapes were checked against current documentation and the installed SDKs rather than recalled.
- Per-provider keys as `SecretStr`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`. A selected provider without its key raises `MissingApiKeyError` at startup, naming the variable to set.
- `merge_images` and `parse_json_payload` in `app/agent/llm/base.py`, shared by all three adapters. The parser tolerates a fenced code block and rejects anything that is not a JSON object.
- `scripts/llm_smoke.py`: one text prompt and one image prompt against the configured provider, printing parsed JSON, token counts and latency.
- A `live` pytest marker for tests that call a real API, skipped unless `LIVE_LLM=1`.
- 38 unit tests, all against fake SDK clients. Nothing in the suite calls a real API.

Changed

- A mock provider in production is now fatal rather than a logged error. An agent on the mock accepts tasks and reports success while making no real decisions.
- `app/core/redaction.py` gained `SAFE_KEYS`, a small exact-match allowlist so token counts (`input_tokens`, `output_tokens` and similar) are not masked. They contain "token" but are measurements, and they are the column names `llm_decisions` already uses.
- `LLM_API_KEY` replaced by the three per-provider keys.

### B12 - LLM adapter base and mock provider

Added

- `app/agent/llm/base.py`: `LLMMessage` (role, text, JPEG images), `LLMUsage`, `LLMResponse`, and the `LLMClient` protocol with a single `generate_json` method. Errors: `LLMTimeout`, `LLMRateLimited` (carrying a server-supplied `retry_after`), `LLMUnavailable` and `LLMBadOutput`.
- `RetryingLLM`: exponential backoff with full jitter, three attempts by default, for transport failures only. `LLMBadOutput` is never retried, because re-sending an identical prompt buys an identical bad answer. Sleep and RNG are injectable so tests neither wait nor flake.
- `FallbackLLM`: tries a second provider when the first is unreachable, but not when the model merely answered badly.
- `app/agent/llm/mock.py`: `MockLLM` with a scripted mode (a queue of decisions) and a rule-based mode that reads the goal and the page summary and picks a sensible action. It records every call, and can be told to fail N times or to return malformed JSON. Zero network.
- `app/agent/llm/factory.py`: `build_llm(settings)` over `LLM_PROVIDER`, with `LLM_FALLBACK_PROVIDER`. A typo and a not-yet-implemented provider give different errors, both at startup.
- Setting `LLM_FALLBACK_PROVIDER`.
- 64 unit tests.

### B11 - DOM summarizer, BeautifulSoup fallback, untrusted wrapping

Added

- `app/agent/perception/state.py`: `PageState` with url, title, tabs, active tab, scroll, elements, summary text, dom hash and capture time. `compute_dom_hash` fingerprints the URL plus ordered element signatures, deliberately excluding position so scrolling does not change it.
- `app/agent/perception/summarizer.py`: `build_summary` and `build_header` producing the spec section 4.6 line format. Selection is by priority under a token budget (default 3000, roughly four characters per token): modal-covered elements, then in-viewport form fields, buttons and links, then below the fold. Selected lines are emitted in index order.
- `app/agent/perception/bs4_fallback.py`: `extract_main_text`, `extract_tables` (row dictionaries), `extract_list_items` and `summarise_static`, with script and hidden content dropped and the same masking rules as the collector, including card-shaped numbers in body text.
- `app/agent/perception/untrusted.py`: `wrap_page_state`, `neutralize` and `strip_invisible`. A page cannot close its own `<page_state>` fence, whatever the casing or spacing, and zero-width, bidi and control characters are removed first.
- `app/agent/perception/perceive.py`: `perceive(page, session)` running extraction, summarisation and hashing in one pass.
- `scripts/perceive.py <url>`: prints the summary, element counts and dom hash, with `--show-fenced` and `--budget`.
- Dependencies: `beautifulsoup4`, `lxml`.
- 90 unit tests.

Changed

- `app/agent/browser/driver.py`: `PageState` renamed to `BrowserSnapshot`, so the cheap before/after snapshot is not confused with the full perception state. `PlaywrightDriver` gained public `tab_ids` and `active_tab_id`.

### B10 - DOM extractor

Added

- `app/agent/perception/dom_extractor.js`, loaded as a package resource and run through `page.evaluate`. Collects visible interactive elements: semantic controls, ARIA roles, `[onclick]`, `[contenteditable]`, `tabindex >= 0`, and anything the page styles with `cursor: pointer`. Nested pointer wrappers are dropped in favour of the outermost meaningful target.
- Per element: index, tag, role, accessible name, text (collapsed, 80 chars), placeholder, input type, value, href (path and query only), bbox in top-level viewport coordinates, `in_viewport`, `covered`, `frame_id`, `css_path`, `xpath`, and disabled/checked/selected/expanded state.
- Exclusion of everything spec section 4.11 requires: `display:none`, `visibility:hidden`, zero opacity, `aria-hidden` ancestors, the `hidden` attribute, elements off the document, and elements smaller than 8px in either axis.
- Value masking at the point of collection for `type=password` and for fields whose name, id or autocomplete suggests a card number, CVV, OTP, PIN or similar.
- Elements covered at their centre point are dropped, unless the thing covering them is a modal, in which case they are kept with `covered=true` so the recovery ladder can dismiss the overlay.
- Same-origin iframes are walked with their offset applied so every bbox shares one coordinate space; cross-origin iframes are listed as opaque entries.
- `data-agent-idx` stamping, with stale stamps from previous runs cleared first.
- `app/agent/perception/dom_extractor.py`: `extract(page) -> list[ElementInfo]` with a time limit and `PageNotReadyError`, plus `locator_for_index(page, index)` which searches every frame.
- Fixtures: `flight_search`, `results`, `cookie_modal`, `hidden_injection`, `iframe` with `iframe_child`, `password_form`, `dynamic`.
- 46 unit tests.

### B09 - Screenshots, storage, budgets

Added

- `pillow` dependency.
- `app/agent/browser/screenshots.py`: `capture(page)` grabs the viewport as PNG, downscales to `SCREENSHOT_MAX_WIDTH` (1280) preserving aspect ratio, and encodes JPEG at `SCREENSHOT_JPEG_QUALITY` (60), matching spec section 6.1. `Screenshot` carries the image and the viewport dimensions separately so a client can map a bbox onto a scaled frame. `to_base64()` for the WebSocket payload. Compression runs off the event loop.
- `app/services/storage.py`: the `ScreenshotStorage` protocol and `LocalFileStorage` under `STORAGE_DIR/screenshots/{task_id}/{step:04d}.jpg`. Refs are opaque relative strings validated against an anchored pattern, with a containment check behind it for symlinks. Writes go to a temporary name and are renamed, so no reader sees a partial file. All filesystem calls run in a worker thread.
- `app/agent/budget.py`: `TaskBudget` with `check()`, `consume_step()` and `reset()`, raising `BudgetExceeded(reason=MAX_STEPS|MAX_TIME)`. Monotonic, injectable clock.
- Settings `SCREENSHOT_MAX_WIDTH` and `SCREENSHOT_JPEG_QUALITY`.
- `make test-fast` (`pytest tests/unit -m "not browser"`) for a tight edit loop, documented in the README.
- 71 new unit tests (19 budget, 17 screenshot, 35 storage).

Changed

- `STORAGE_LOCAL_PATH` renamed to `STORAGE_DIR`.

### B08 - Playwright driver and action executor

Added

- `playwright` dependency and Chromium. `make browsers` installs it, `make install` now does too, and the README lists it as a prerequisite.
- `app/agent/browser/actions.py`: a frozen, `extra="forbid"` Pydantic model per allow-listed action, as a discriminated union. `navigate` and `new_tab` accept only http/https; `wait` is capped at 10 s; `scroll` at 10000 px. `parse_action()` raises `ActionNotAllowedError` for anything outside spec section 4.4.
- `app/agent/browser/errors.py`: `ActionErrorCode` (TIMEOUT, ELEMENT_NOT_FOUND, NOT_INTERACTABLE, NAVIGATION_FAILED, DOMAIN_NOT_ALLOWED, ACTION_NOT_ALLOWED, TAB_NOT_FOUND, UNKNOWN), `ActionNotAllowedError`, `DomainNotAllowedError`.
- `app/agent/browser/driver.py`: the `BrowserDriver` protocol and `PlaywrightDriver`. One isolated `BrowserContext` per task with `accept_downloads=False`, viewport 1280x800, locale en-IN, timezone Asia/Kolkata, headless and timeouts from settings. `ActionResult` carries ok, error_code, error_message, duration_ms, url_before, url_after. Popups are tracked as switchable tabs.
- `host_is_allowed()`: the spec section 4.13 sandbox check, on the parsed hostname. A bare host matches any port; a port-qualified entry matches only that port; an empty allow-list blocks everything.
- Settings `BROWSER_ACTION_TIMEOUT_MS` and `BROWSER_NAVIGATION_TIMEOUT_MS`; `AGENT_ALLOWED_DOMAINS` now lists the demo port.
- `tests/fixtures/html/`: simple_form, buttons, long_page, select, download; a session-scoped fixture serving them on a random localhost port. No test reaches the internet.
- 96 new unit tests (51 action, 45 browser), marked `browser` where they drive Chromium.

### B07 - Refresh rotation, RBAC, rate limiting

Added

- Login now sets an httpOnly refresh cookie: an opaque 384 bit token, stored only as its SHA-256 hash, `SameSite=Lax`, `Secure` when `APP_ENV=prod`, scoped to `path=/api/v1/auth`.
- `POST /api/v1/auth/refresh`: single-use rotation. The presented token is revoked and points at its successor via `replaced_by_id`.
- Reuse detection: presenting an already-revoked token revokes every session for that user, writes a `TOKEN_REUSE_DETECTED` audit row, and returns 401.
- `POST /api/v1/auth/logout`: revokes the token and clears the cookie, idempotent, always 204.
- `GET /api/v1/auth/me`.
- `require_roles(*roles)` in `app/api/deps.py`: 403 for the wrong role, 401 for no token. ADMIN is not implied; each route lists what it accepts.
- `app/core/rate_limit.py`: a `RateLimiter` interface with an in-memory token bucket, keyed on route plus client address, with idle-bucket eviction. Applied at 5/min to login and 3/min to register; 429 in the error envelope with a `Retry-After` header.
- `scripts/create_admin.py`: creates an ADMIN or APPROVER from the CLI, prompting for the password rather than taking it as an argument.
- `AppError` carries response headers, so 429 can send `Retry-After`.
- Settings: `RATE_LIMIT_ENABLED`, `RATE_LIMIT_LOGIN_PER_MINUTE`, `RATE_LIMIT_REGISTER_PER_MINUTE`, `RATE_LIMIT_TASK_CREATE_PER_MINUTE`, `RATE_LIMIT_TRUST_FORWARDED_FOR`.
- `docs/auth.md` gains the rotation and reuse diagram, the cookie table, the roles section and the rate limiting section.
- 12 unit tests and 39 integration tests.

Changed

- Reuse handling runs in the request's own transaction and the route returns the 401 rather than raising it, so the chain revocation commits. Raising would have rolled it back at exactly the wrong moment.
- Audit metadata uses `session_id` rather than `token_id`: the redaction rule matches any key containing "token" and was masking the identifier a security review needs.

### B06 - Auth: JWT, JWKS, register, login

Added

- Dependencies: `pyjwt[crypto]`, `argon2-cffi`.
- `scripts/gen_keys.py`: generates an RSA 2048 keypair into `keys/` (gitignored, private key 0600), prints the env lines, and refuses to overwrite without `--force`.
- `app/core/jwt_keys.py`: `KeyPair`, `load_key_pair`, and the JWK/JWKS document (`kty`, `kid`, `use`, `alg`, `n`, `e`, base64url unpadded). Keys are loaded once at startup and the service fails to boot without them.
- `app/core/security.py`: argon2id `hash_password` / `verify_password`, and `TokenService.create_access_token` / `decode_access_token`. Tokens carry `sub`, `role`, `iss`, `aud`, `iat`, `exp`, `jti` with `kid` in the header; decode pins `algorithms=["RS256"]` and requires every claim.
- `GET /.well-known/jwks.json` at the root path, unauthenticated, cached five minutes.
- `POST /api/v1/auth/register`: email normalised to lowercase, password policy (min 10, not all digits, max 256), duplicate email 409, role `TASK_SUBMITTER`, response never carries the hash.
- `POST /api/v1/auth/login`: returns `accessToken`, `tokenType`, `expiresIn`, `user`. Unknown email and wrong password fail identically, and an unknown email still runs an argon2 verification so the timing does not differ.
- `get_current_user` bearer dependency, plus `SettingsDep`, `KeyPairDep`, `TokenServiceDep`, `AuthServiceDep`.
- `app/schemas/base.py` (`CamelModel`) and `app/schemas/auth.py`; responses are camelCase on the wire.
- `InvalidCredentials` error (401 `INVALID_CREDENTIALS`) and the `LOGIN_FAILED` audit action.
- `docs/auth.md`: two sequence diagrams, the token contents, what decode rejects, and why the failed-login audit needs its own transaction.
- 23 unit tests and 39 integration tests, including `alg: none`, the RS256-to-HS256 downgrade, tampered payload and signature, wrong issuer and audience, and a token verified using only the published JWKS.

Changed

- Settings: `JWT_ACCESS_TTL_SECONDS` and `JWT_REFRESH_TTL_SECONDS` replaced by `ACCESS_TOKEN_TTL_MIN=15` and `REFRESH_TOKEN_TTL_DAYS=7`, with `access_token_ttl` / `refresh_token_ttl` properties. Added `JWT_AUDIENCE` and `JWT_KID`.
- Integration tests clean committed audit rows before each test, because the failed-login audit deliberately commits outside the test transaction.

### B05 - Repositories and audit service

Added

- `app/repositories/base.py`: generic `BaseRepository[Model]` (`get`, `get_or_404`, `add`, `add_all`, `delete`, `count`, `list`, `paginate`) and a `Page` result carrying `items`, `total`, `page`, `page_size` plus `total_pages`, `has_next`, `has_previous`. Page size is capped at 100 (decision A17).
- `UserRepository` (`get_by_email`, case-insensitive; `email_exists`) and `RefreshTokenRepository` (`get_by_token_hash`, `list_active_for_user`, `revoke` with rotation chain, `revoke_all_for_user`).
- `TaskRepository`: `get_owned` (NotFound, not Forbidden, for other users' tasks; admins exempt), `list_for_user` with status, date range and escaped text search, `get_by_number`, `list_active`, `count_active_for_user`.
- `TaskStepRepository`: `next_step_number`, `list_for_task`, `list_after` (WebSocket replay), `get_by_number`.
- `LlmDecisionRepository`, `VerificationRepository`, `RecoveryAttemptRepository` (including `list_for_task` and `next_attempt_number`), `ApprovalRepository` (`get_pending_for_task`, `list_for_task`, `list_expired`), `AuditEventRepository`, `CachedFlowRepository` (`get_by_signature`, `upsert` via `ON CONFLICT`, `record_run`, `should_invalidate`).
- `app/repositories/unit_of_work.py`: `UnitOfWork` bundling every repository over one session, with a `UnitOfWorkDep` FastAPI dependency. Deliberately has no `commit()`.
- `app/services/audit.py`: `AuditService.record()` plus `AuditAction` constants. Metadata is redacted before it is stored.
- `app/core/redaction.py`: the masking rules, extracted from `app/core/logging.py` now that the audit trail is a second consumer. Logs and audit rows mask identically, asserted by a test.
- 32 integration tests covering every repository method, pagination maths, ownership, filters, and audit redaction.

Fixed

- `CachedFlowRepository.upsert` returned the stale identity-mapped instance after `ON CONFLICT DO UPDATE`; it now uses `populate_existing` so the returned object reflects what the database holds.

### B04 - Data model and initial migration

Added

- `app/domain/enums.py`: `UserRole`, `TaskStatus` (with `is_terminal`), `StepStatus`, `ActionType`, `RiskLevel`, `ApprovalStatus`, `VerificationMethod`, `RecoveryOutcome`. All stored as native Postgres enum types holding the member value, not its name.
- `app/models/`, grouped by aggregate: `user.py` (User, RefreshToken), `task.py` (Task, TaskStep, LLMDecision, Verification, RecoveryAttempt), `approval.py`, `audit.py`, `cache.py`.
- `refresh_tokens`, an addition to spec section 5, now documented there: refresh rotation needs a server-side record so a stolen token can be revoked and reuse of a rotated one detected. Only the hash is stored.
- `task_number` generated by the Postgres sequence `task_number_seq`, formatted `T-000001`.
- Migration `a78013bebd3a`, hand-corrected after autogenerate. Upgrade, downgrade and re-upgrade all verified against a throwaway database.
- `docs/database.md`: Mermaid ER diagram and a table-by-table explanation.
- `CreatedAtMixin` split out of `TimestampMixin`, for the tables that carry only `created_at`; `enum_column()` helper in `app/db/base.py`.
- 48 new tests (99 unit, 32 integration).

Changed

- `docs/PROJECT_SPEC.md` section 5 documents `refresh_tokens` and why it exists.
- The B03 migration test now compares against alembic's head rather than a hardcoded revision.

Notes

- Indexes are exactly those spec section 5 lists, plus `unique(task_id, step_number)` and, for the added table, `refresh_tokens.token_hash` and `user_id`. `approvals.step_id`, `tasks.created_by` and `audit_events.actor_id` are deliberately unindexed.
- All relationships are `lazy="raise"`: nothing loads implicitly.

### B03 - Postgres, async database layer, Alembic

Added

- `docker-compose.dev.yml`: postgres:16 with a named volume, a `pg_isready` healthcheck, and `scripts/db/init.sql` creating `agent_core`, `agent_core_test` and `payments` on first boot. `make db-up` waits for healthy.
- Dependencies: sqlalchemy[asyncio] 2.x, asyncpg, alembic.
- `app/db/base.py`: `Base` with a constraint naming convention, `UUIDPrimaryKeyMixin` (client and server defaults) and `TimestampMixin` (timezone-aware, server defaults).
- `app/db/session.py`: `Database` owning the async engine and session factory, a `session()` context manager that commits or rolls back, a `healthcheck()` that runs `SELECT 1` under a timeout and never raises, and the `get_database` / `get_session` dependencies.
- Alembic in `migrations/` on the async template. `env.py` takes its URL from `Settings` and imports `app.models` for autogenerate. Baseline revision `0001_baseline`.
- Makefile: `revision`, `downgrade`, `db-reset`; `db-up` now waits for the healthcheck and reports the published port.
- Health reports `db: ok | down` and returns 503 when Postgres is unreachable.
- Test fixtures: a stub database for unit tests, a session-scoped engine against `agent_core_test` that builds and drops the schema, and a per-test transactional session using `join_transaction_mode="create_savepoint"`.
- 10 integration tests and 51 unit tests.

Changed

- Database credentials moved to `POSTGRES_*` settings; `database_url` and `test_database_url` are derived from them, with `DATABASE_URL` still available as an override. compose and the app read the same variables, so they cannot drift.
- `migrations/script.py.mako` rewritten so generated revisions satisfy ruff (modern typing, no trailing whitespace); `make revision` formats its output.
- `docs/BACKEND_PLAN.md` section 2: `alembic/` corrected to `migrations/`.
- README quick start covers the database, and documents the port 5432 conflict.

### B02 - App core, errors, logging, health

Added

- Runtime dependencies: fastapi, uvicorn[standard], pydantic, pydantic-settings, structlog, httpx.
- `app/core/config.py`: `Settings` over pydantic-settings with `.env` support, covering app, database, JWT, LLM, agent budget, billing and storage groups. Secrets are `SecretStr`; list settings accept comma-separated values; `get_settings()` is cached.
- `app/core/logging.py`: structlog with JSON rendering in prod and a console renderer in dev, `request_id` merged from contextvars, and a `redact_sensitive` processor that masks password, token, authorization, api_key, secret, card, cvv, screenshot and similar keys at any depth.
- `app/core/errors.py`: `AppError` base plus `ValidationFailed` (400), `Unauthorized` (401), `InsufficientCredits` (402), `Forbidden` (403), `NotFound` (404), `Conflict` (409), `UnprocessableAction` (422), `RateLimited` (429) and `DependencyUnavailable` (503). Handlers render the spec section 6 envelope for `AppError`, request validation, bare `HTTPException` and anything unhandled.
- `app/core/middleware.py`: `RequestContextMiddleware` honours an incoming `X-Request-ID` or mints `req_<12 hex>`, echoes it on the response, binds it for logging, and emits one access line with method, path, status and duration_ms.
- `app/api/deps.py`: `SettingsDep`, reading settings from app state so `create_app(settings)` configures handlers too.
- `app/main.py`: `create_app()` factory with a lifespan hook carrying startup and shutdown placeholders for the database, browser and task runner, CORS from settings, and the v1 router mounted under `API_PREFIX`.
- `GET /api/v1/health` returning status, version, env and time.
- 49 unit tests across config, logging redaction, error envelopes, request-id propagation and health.

Changed

- `.env.example` now documents all 40 settings, enforced by a test.
- `docs/BACKEND_PLAN.md` section 2 corrected: `models/` and `repositories/` are siblings of `db/`, and the api and core subtrees match the repo.

Notes

- Request validation returns **400**, not FastAPI's default 422. Spec section 6 assigns 400 to validation and reserves 422 for an action the agent cannot carry out.

### B01 - Repo scaffolding

Added

- `CLAUDE.md` with the permanent working rules (source of truth, async and typing standards, architecture direction, secret and logging bans, definition of done).
- uv application project pinned to Python 3.12, with ruff, mypy, pytest, pytest-asyncio, pytest-cov and pre-commit as dev dependencies.
- Tooling configuration in `pyproject.toml`: ruff at line length 100, mypy strict over `app/`, pytest with `asyncio_mode=auto`, coverage scoped to `app`.
- Empty package structure for `app/` (api, core, db, models, schemas, repositories, services, domain) and `app/agent/` (browser, perception, vision, planner, llm, safety, verification, recovery, cache).
- Repo-root directories: `demo_sites/`, `tests/` (unit, integration, chaos, eval, fixtures), `scripts/`, `docs/` (adr, phase-notes, benchmarks), and a gitignored `storage/`.
- `Makefile` with install, dev, test, test-unit, test-int, lint, fmt, typecheck, check, migrate, db-up, db-down.
- `.gitignore`, `.editorconfig`, `.env.example`, and `.pre-commit-config.yaml` with ruff, ruff-format, end-of-file, trailing-whitespace and a gitleaks secret scanner.
- `README.md` skeleton and this changelog.
- Smoke tests so the suite runs and the async mode is proven.
