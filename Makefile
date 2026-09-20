# browser-agent-backend (AI-07)
# Run `make help` for the target list.

.DEFAULT_GOAL := help
.PHONY: help install dev test test-unit test-int lint fmt typecheck check migrate db-up db-down

UV      ?= uv
COMPOSE ?= docker compose

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install all dependencies and the git hooks
	$(UV) sync
	$(UV) run pre-commit install

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

check: lint typecheck test-unit  ## the gate a part must pass
	@echo "check passed"

migrate:  ## apply database migrations
	@if [ ! -f alembic.ini ]; then \
		echo "alembic.ini does not exist yet - migrations arrive in a later part."; \
		exit 1; \
	fi
	$(UV) run alembic upgrade head

db-up:  ## start Postgres
	@if [ ! -f docker-compose.yml ]; then \
		echo "docker-compose.yml does not exist yet - it arrives in a later part."; \
		exit 1; \
	fi
	$(COMPOSE) up -d postgres

db-down:  ## stop Postgres
	@if [ ! -f docker-compose.yml ]; then \
		echo "docker-compose.yml does not exist yet - it arrives in a later part."; \
		exit 1; \
	fi
	$(COMPOSE) down
