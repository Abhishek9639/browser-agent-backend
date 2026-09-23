# browser-agent-backend

Backend for the Browser Automation Agent (AI-07). One of three repositories: **backend**, frontend, payments.

A supervised autonomous browser agent. A user submits a natural-language task; a server-side Playwright browser perceives the page, an LLM proposes one structured action at a time, the backend executes it, a deterministic verifier confirms the outcome, and a recovery layer retries on failure. High-risk actions pause for human approval. Every step is persisted and streamed live over WebSocket.

This service owns identity (it is the only JWT issuer), the agent core, task and step persistence, the live stream, and the demo sites.

See [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) for the full specification and [docs/BACKEND_PLAN.md](docs/BACKEND_PLAN.md) for the build plan.

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose (for Postgres)
- Chromium for Playwright: `uv run playwright install chromium`, or `make browsers`

## Quick start

```bash
cp .env.example .env     # then edit if you need to
make install             # sync dependencies, install Chromium, install git hooks
make db-up               # start Postgres, wait until it accepts connections
make migrate             # apply migrations
make check               # lint, typecheck, unit tests
make test-int            # integration tests, against agent_core_test
```

Then run it:

```bash
make dev
curl localhost:8000/api/v1/health
```

```json
{"status":"ok","version":"0.1.0","env":"dev","db":"ok","time":"..."}
```

Health returns 503 with `"db":"down"` when Postgres is unreachable, so it works
as a readiness probe as well as a liveness one.

### If port 5432 is already in use

A Homebrew or Postgres.app install binds `127.0.0.1:5432` and shadows Docker's
wildcard bind, so the app would silently talk to the wrong server. Set a free
port in `.env` and both compose and the app follow it:

```bash
POSTGRES_PORT=5433
```

Run `make help` for every target.

| Target | What it does |
|---|---|
| `install` | Sync dependencies, install Chromium, install pre-commit hooks |
| `browsers` | Install the Chromium build Playwright drives |
| `dev` | Run the API with autoreload on :8000 |
| `test` | Whole suite with coverage |
| `test-unit` | Unit tests only |
| `test-int` | Integration tests (needs `make db-up`) |
| `lint` / `fmt` | ruff check / autofix and format |
| `typecheck` | mypy strict on `app/` |
| `check` | lint + typecheck + test-unit, the gate every part must pass |
| `migrate` | Apply database migrations |
| `revision` | Autogenerate a migration: `make revision m="add users"` |
| `downgrade` | Roll back one migration |
| `db-up` / `db-down` | Start and stop Postgres |
| `db-reset` | Stop Postgres and delete its data volume |

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
migrations/   alembic revisions
demo_sites/   mock sites for demos and chaos tests (:8200)
tests/        unit, integration, chaos, eval
docs/         spec, plan, ADRs, phase notes, changelog
```

## Status

Through B06: app core, database layer and migrations, the data model,
repositories, and authentication (register, login, JWKS). Parts land one at a
time.

Before running: `python scripts/gen_keys.py` to create the JWT signing keypair.
