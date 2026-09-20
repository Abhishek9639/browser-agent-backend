# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

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
