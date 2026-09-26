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
make check               # lint, typecheck, unit tests (drives Chromium, ~50s)
make test-fast           # same tests without Chromium, for a tight loop
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
| `test-unit` | Unit tests only, including the ones that drive Chromium |
| `test-fast` | Unit tests without Chromium, for a tight edit loop |
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

## SkyFare, the demo site

A mock airline search site the agent is pointed at, in `demo_sites/skyfare/`.
It is deliberately not a bare test page: the agent has to cope with a layout
that looks like something a person would actually book a flight on.

```bash
make skyfare
```

Then open <http://localhost:8200>. Nothing is ever charged and no seat is ever
reserved.

| Search | Results |
| --- | --- |
| ![The search form](docs/images/skyfare-search.png) | ![Flight results](docs/images/skyfare-results.png) |

| Flight detail | Booking |
| --- | --- |
| ![One flight in detail](docs/images/skyfare-detail.png) | ![The passenger form and Pay now](docs/images/skyfare-book.png) |

Results are generated from a seed derived from the origin, destination and
date, so the same search always returns the same flights on any machine. That
is what makes an agent run scorable: the cheapest fare for a given query is a
fixed answer rather than whatever was generated that time.

### Chaos toggles

Real sites misbehave. Each of these reproduces something that has broken a
browser agent in practice. Turn them on per request with `?chaos=` or for a
whole session with `SKYFARE_CHAOS`.

```bash
open "http://localhost:8200/results?from=Delhi&to=Bengaluru&date=2026-10-20&chaos=cookie,inject,layoutb"
```

![Cookie modal, injected instructions and the alternate layout](docs/images/skyfare-chaos.png)

| Toggle | What it does |
| --- | --- |
| `delay` | Results render after 0.5 to 3 seconds |
| `cookie` | A consent modal covers the page |
| `popup` | A newsletter appears two seconds in, after a fast agent has read the page |
| `shuffle` | Every id and class name is regenerated on each load |
| `layoutb` | Filters move to the top and the button becomes "Find flights" |
| `iframe` | Results move inside an iframe |
| `newtab` | Flight details open in a new tab |
| `inject` | Text addressed to the agent, both visible and hidden |
| `slowtype` | Inputs debounce, so suggestions vanish and come back |

`chaos=all` turns on everything at once.

The `inject` toggle is a test fixture for the spec section 4.11 defences: the
collector withholds hidden elements, the page state is fenced as untrusted, and
the planner can only name an element it was shown. A run where the injected
text changes what the agent does is the finding, not a bug in the site.

### Ground truth

`GET /api/cheapest?from=&to=&date=` returns the correct answer for a query, for
scoring an agent's run against. It is behind a header and returns 404 without
it, so an agent that guesses the path learns nothing from trying it. It is a
measuring instrument, not part of the site.

## Status

The agent runs end to end: submit a task and it drives a browser, plans each
step against an LLM, verifies what it did, and records every step with a
screenshot. Built so far are the app core, the database layer and migrations,
the data model and repositories, authentication, the browser driver, DOM
perception, the LLM adapters and planner, the task lifecycle and event bus, the
agent loop, the task runner and API, and the SkyFare demo site.

Still to come: live streaming over WebSocket, verification, recovery, visual
grounding, approvals, the flow cache, credits, and packaging. Parts land one at
a time.

Before running: `python scripts/gen_keys.py` to create the JWT signing keypair.
