# browser-agent-backend (AI-07)
# Run `make help` for the target list.

.DEFAULT_GOAL := help
.PHONY: help install browsers dev test test-unit test-fast test-int lint fmt typecheck check \
	check-all audit chaos bench skyfare up down logs build \
	migrate revision downgrade db-up db-down db-reset

UV      ?= uv
# The development database on its own, for running the suite from a checkout.
COMPOSE ?= docker compose -f docker-compose.dev.yml
# The whole backend in containers: Postgres, the API and SkyFare.
STACK   ?= docker compose

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install all dependencies, browsers and the git hooks
	$(UV) sync
	$(UV) run playwright install chromium
	$(UV) run pre-commit install

browsers:  ## install the Chromium build Playwright drives
	$(UV) run playwright install chromium

dev:  ## run the API with autoreload
	@if [ ! -f app/main.py ]; then \
		echo "app/main.py does not exist yet - the FastAPI entrypoint arrives in a later part."; \
		exit 1; \
	fi
	$(UV) run uvicorn app.main:app --reload --port 8000

test:  ## run the whole suite with coverage
	$(UV) run pytest --cov --cov-report=term-missing

test-unit:  ## run unit tests only
	$(UV) run pytest tests/unit

test-fast:  ## unit tests without the ones that drive Chromium
	$(UV) run pytest tests/unit -m "not browser"

test-int:  ## run integration tests (needs `make db-up`)
	$(UV) run pytest tests/integration -m integration

lint:  ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:  ## autofix and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck:  ## mypy strict on app/
	$(UV) run mypy

check: lint typecheck test-unit  ## the fast gate a part must pass
	@echo "check passed"

check-all: lint typecheck test-unit test-int  ## the full gate: adds integration (needs `make db-up`)
	@echo "check-all passed"

up:  ## build and start the backend stack (postgres, api, skyfare)
	$(STACK) up --build -d
	@echo "api      http://localhost:$${API_PORT:-8000}/api/v1/health"
	@echo "docs     http://localhost:$${API_PORT:-8000}/docs"
	@echo "skyfare  http://localhost:$${SKYFARE_PORT:-8200}/"

down:  ## stop the backend stack (keeps the volumes)
	$(STACK) down

logs:  ## follow the stack's logs
	$(STACK) logs -f

build:  ## build the images without starting anything
	$(STACK) build

audit:  ## known vulnerabilities in the locked dependencies, and a secret scan
	$(UV) run pip-audit --strict --progress-spinner off
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks detect --config .gitleaks.toml --redact --no-banner; \
	else \
		echo "gitleaks not installed; skipping the secret scan (brew install gitleaks)"; \
	fi
	@echo "audit passed"

chaos:  ## run the chaos suite against SkyFare (needs `make db-up` and browsers)
	$(UV) run pytest tests/chaos -m chaos

bench:  ## benchmark the agent: make bench runs=3 provider=mock
	$(UV) run python scripts/benchmark.py --runs $(or $(runs),1) \
		$(if $(provider),--provider $(provider),) $(if $(chaos),--chaos $(chaos),)

skyfare:  ## serve the SkyFare demo site on :8200
	$(UV) run uvicorn demo_sites.skyfare.app:app --host 127.0.0.1 --port 8200 --reload

migrate:  ## apply database migrations
	$(UV) run alembic upgrade head

revision:  ## autogenerate a migration: make revision m="add users"
	@if [ -z '$(m)' ]; then echo 'usage: make revision m="your message"'; exit 1; fi
	$(UV) run alembic revision --autogenerate -m '$(m)'
	@# alembic renders string literals with repr(), so single quotes; format them.
	$(UV) run ruff format -q migrations/versions
	$(UV) run ruff check --fix -q migrations/versions

downgrade:  ## roll back one migration
	$(UV) run alembic downgrade -1

db-up:  ## start Postgres and wait until it is accepting connections
	$(COMPOSE) up -d --wait postgres
	@echo "postgres ready at $$($(COMPOSE) port postgres 5432)"

db-down:  ## stop Postgres (keeps the data volume)
	$(COMPOSE) down

db-reset:  ## stop Postgres and delete the data volume
	$(COMPOSE) down -v
