# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

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
