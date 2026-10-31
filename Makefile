COMPOSE ?= docker-compose
UV_RUN = uv run --env-file .env

.PHONY: env up down migrate test test-unit test-integration lint typecheck export-openapi check-openapi generate-fixtures seed run-worker-once

env:
	@if [ -f .env ]; then echo ".env already exists; not overwritten."; else cp .env.example .env && echo "Created .env. Replace every CHANGE_ME value before continuing."; fi

up:
	$(COMPOSE) up -d --build db migrate api

down:
	$(COMPOSE) down

migrate:
	$(COMPOSE) run --rm migrate

test:
	$(UV_RUN) pytest

test-unit:
	$(UV_RUN) pytest -m "not integration"

test-integration:
	$(UV_RUN) pytest -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

export-openapi:
	$(UV_RUN) python -m app.cli export-openapi

check-openapi:
	$(UV_RUN) python -m app.cli export-openapi --check

generate-fixtures:
	$(UV_RUN) python -m app.cli generate-fixtures

seed:
	$(UV_RUN) python -m app.cli seed

run-worker-once:
	$(UV_RUN) python -m app.cli run-worker-once
