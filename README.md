# browser-agent-backend

Backend for the Browser Automation Agent (AI-07). One of three repositories: **backend**, frontend, payments.

A supervised autonomous browser agent. A user submits a natural-language task; a server-side Playwright browser perceives the page, an LLM proposes one structured action at a time, the backend executes it, a deterministic verifier confirms the outcome, and a recovery layer retries on failure. High-risk actions pause for human approval. Every step is persisted and streamed live over WebSocket.

This service owns identity (it is the only JWT issuer), the agent core, task and step persistence, the live stream, and the demo sites.

See [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) for the full specification and [docs/BACKEND_PLAN.md](docs/BACKEND_PLAN.md) for the build plan.

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose (for Postgres, from a later part)

## Quick start

```bash
make install     # sync dependencies and install git hooks
make check       # lint, typecheck, unit tests
```

Run `make help` for every target.

| Target | What it does |
|---|---|
| `install` | Sync dependencies, install pre-commit hooks |
| `dev` | Run the API with autoreload on :8000 |
| `test` | Whole suite with coverage |
| `test-unit` | Unit tests only |
| `test-int` | Integration tests (needs `make db-up`) |
| `lint` / `fmt` | ruff check / autofix and format |
| `typecheck` | mypy strict on `app/` |
| `check` | lint + typecheck + test-unit, the gate every part must pass |
| `migrate` | Apply database migrations |
| `db-up` / `db-down` | Start and stop Postgres |

## Layout

```text
app/          application package
  api/        FastAPI routers (v1)
  core/       config, logging, security, middleware
  db/         engine and session
  models/     SQLAlchemy models
  schemas/    Pydantic request and response models
  repositories/  data access
  services/   orchestration and business logic
  domain/     enums and the task state machine
  agent/      the agent core, never imports FastAPI
demo_sites/   mock sites for demos and chaos tests (:8200)
tests/        unit, integration, chaos, eval
docs/         spec, plan, ADRs, phase notes, changelog
```

## Status

Scaffolding only (B01). Nothing is wired up yet; parts land one at a time.
