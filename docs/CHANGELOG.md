# Changelog

All notable changes to the backend. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); parts are tracked by their id.

## [Unreleased]

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
