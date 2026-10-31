# Tools and Technologies

Written for: anyone new to the project who wants to know what each tool is, why it was chosen, and where it lives in the repository.

Status key: **Used** means it is installed and exercised by code or tests today. **Planned** means it appears in the design documents but nothing in the repository uses it yet.

## Language and runtime

| Tool | Status | Why | Where |
|---|---|---|---|
| Python 3.13 | Used | One language for the API, ingestion, data generation, and (later) ML. Pinned to `3.13.*` so every machine and the container run the same version. | `pyproject.toml` (`requires-python`), `.python-version`, `Dockerfile` (`python:3.13-slim`) |
| uv | Used | Fast dependency resolver and runner with a lock file, so installs are reproducible. `uv run --env-file .env` loads local settings without exporting them to the shell. | `uv.lock`, `Makefile`, `Dockerfile` (uv 0.13.6 copied in, `uv sync --frozen --no-dev`) |

## Application libraries

| Library | Status | Why | Where |
|---|---|---|---|
| FastAPI | Used | Typed REST framework that generates an OpenAPI contract from the code, which the project treats as the API contract. | `app/api/main.py`, `app/api/routes/` |
| Uvicorn | Used | ASGI server that runs the FastAPI app. | `Dockerfile` (`CMD`), compose `api` service |
| Pydantic v2 | Used | Request and response models, so the contract and the validation are one definition. | `app/api/routes/health.py`, `app/config.py` (response models for the other endpoints are still stubs) |
| pydantic-settings | Used | Reads configuration from environment variables and validates it at startup, so a missing secret stops the process instead of failing later. Also parses `WORKER_ORG_IDS`. | `app/config.py` |
| SQLAlchemy 2 | Used | Database access. Core and `text()` SQL are used for the tenant-scoped repositories, where the exact SQL and row-level-security behaviour matter. | `app/db/session.py`, `app/db/repository.py`, `app/ingestion/repository.py` |
| psycopg 3 (binary) | Used | PostgreSQL driver. The `binary` build avoids needing a C compiler or `libpq` headers on the machine. | `pyproject.toml`, connection URLs in `.env` |
| Alembic | Used | Versioned schema migrations, run as the owner role. Migrations 0001 to 0003 exist, and `downgrade` is tested. | `migrations/`, `alembic.ini` |
| NumPy | Used | Seeded random number generation for the synthetic data. `SeedSequence` gives independent, reproducible streams per organisation, stage, and source. | `app/ingestion/generators/` |
| Standard library (`hashlib`, `hmac`, `secrets`, `json`, `difflib`, `zoneinfo`, `argparse`) | Used | Deliberately preferred over new dependencies. HMAC-SHA256 with a server pepper hashes API keys. `difflib` does fuzzy name matching. `zoneinfo` interprets timestamps with no offset. `argparse` powers the CLI. | `app/security/api_keys.py`, `app/ingestion/identity.py`, `app/ingestion/pipeline.py`, `app/cli.py` |
| scikit-learn, joblib | Used (Phase 3) | Logistic regression baseline and gradient boosting for the churn model, with the saved artifact checksum-verified before loading. | `docs/ml-design.md` |
| Streamlit | Used | Dashboard that calls the API only and never touches the database (ADR-012). Light theme is pinned in Compose and the theme picker is hidden. | `app/dashboard/`, `docker-compose.yml` (`dashboard` service) |
| Altair | Used | Donut chart on the Portfolio screen. Installed with Streamlit, not declared directly in `pyproject.toml`. | `app/dashboard/screens.py` |
| pandas | Used | Tables and row tints on the dashboard. Installed with Streamlit, not declared directly in `pyproject.toml`. | `app/dashboard/screens.py` |

## Data and database

| Tool | Status | Why | Where |
|---|---|---|---|
| PostgreSQL 16 | Used | One database for everything, with row-level security to separate tenants. Avoids running a second store for a project this size. | compose `db` service, `migrations/` |
| Row-level security (`ENABLE` plus `FORCE`) | Used | Tenant isolation enforced by the database, not only by application code. The policy is fail-closed: with no organisation set, queries return zero rows. `FORCE` makes it apply to the table owner too. | `migrations/0002`, `migrations/0003`, `tests/security/` |
| Two database roles: `chi_owner` and `chi_app` | Used | The owner runs migrations. The application role cannot bypass RLS, does not own tables, and has only the grants it needs. Ingestion tables are `SELECT` and `INSERT` only, so raw and normalised data is append-only for the app. | `migrations/0001`, `tests/security/test_grant_matrix.py` |
| Transaction-local `set_config('app.current_org_id', ...)` | Used | Sets the tenant for one transaction, so it cannot leak to another request on a pooled connection. | `app/db/session.py` (`tenant_session`) |
| Audit trigger | Used | Makes `audit_events` reject `UPDATE` and `DELETE` for every role, including the owner. | `migrations/0002` |
| NDJSON files | Used | Format for the canonical fixtures and side files. Chosen over Parquet to avoid a new dependency; 1,000 records do not need columnar storage. | `app/ingestion/generators/`, `fixtures/canonical/` (committed, checksum-verified) |

## Containers and local environment

| Tool | Status | Why | Where |
|---|---|---|---|
| Docker image (`python:3.13-slim`) | Used | Same runtime locally and in any deployment. Runs as a non-root user (`appuser`, UID 10001). | `Dockerfile` |
| Docker Compose | Used | Starts the database, runs migrations, and starts the API with one command. On this machine it is the standalone `docker-compose` binary, because the `docker compose` plugin is not registered. | `docker-compose.yml`, `Makefile` (`COMPOSE ?= docker-compose`) |
| Compose services (see below) | Used | Three services with explicit start order: database healthy, then migrations complete, then API. | `docker-compose.yml` |
| Colima | Used | Container runtime on macOS without Docker Desktop. | Host machine, described in `docs/phase-1-setup-and-changes.md` |
| Make | Used | Short, documented commands for common tasks (`make up`, `make test`, `make lint`, `make check-openapi`). | `Makefile` |
| `.env` and `.env.example` | Used | Local secrets and settings. `.env.example` holds placeholders, and `.env` is gitignored. Compose requires the password variables to be set. | project root |

### Compose services

| Service | Image or build | Why it exists | Notable settings |
|---|---|---|---|
| `db` | `postgres:16` | The single PostgreSQL instance. | Published on `127.0.0.1` only, port `${DB_PORT:-5434}`. Data in the named volume `chi-pgdata`. Init scripts mounted read-only from `docker/postgres/initdb` run on first start of an empty volume. A `pg_isready` healthcheck gates the other services. |
| `migrate` | Built from the `Dockerfile` | Runs `alembic upgrade head` once, then exits (`restart: "no"`). | Connects as `chi_owner` through `MIGRATION_DATABASE_URL`. Starts only when `db` is healthy. |
| `api` | Built from the `Dockerfile` | Runs the FastAPI app with Uvicorn. | Connects as `chi_app` (never the owner). Starts only after `migrate` completes successfully. Published on `127.0.0.1` only, port `${API_PORT:-8000}`. Mounts `./config` read-only at `/run/chi` for the dev tokens file. `restart: unless-stopped`. |
| `dashboard` | Built from the `Dockerfile` | Runs the Streamlit CSM dashboard on port 8501. | Calls `api` at `http://api:8000` and never connects to the database. Published on `127.0.0.1` only, port `${DASHBOARD_PORT:-8501}`. Light theme is set by `--theme.*` flags. Starts after `api` is healthy. |

Points worth knowing:

- Secrets are never defaulted. `POSTGRES_PASSWORD`, `CHI_OWNER_PASSWORD`, `CHI_APP_PASSWORD` and `API_KEY_PEPPER` use `${VAR:?message}`, so Compose refuses to start if one is missing from `.env`.
- Safe defaults are set in Compose itself: `OUTREACH_MODE=dry_run`, `EMAIL_ADAPTER=mock`, `AUTH_MODE=api_keys`. The `.env.example` file switches `AUTH_MODE` to `dev_tokens` for local demos.
- Both published ports bind to `127.0.0.1`, so the database and API are not reachable from other machines.
- The roles are created by `docker/postgres/initdb/01-roles.sh` on the first start of an empty volume: `chi_owner` and `chi_app`, both `NOSUPERUSER NOBYPASSRLS`, with `chi_owner` made owner of the database. Migration 0001 only verifies that they exist. Because the script runs only on an empty volume, changing a role password in `.env` later does not change the password already stored in the database.
- There is no worker service yet. `WORKER_ORG_IDS` is not passed to any container, so `generate-fixtures`, `seed`, `train`, `register`, and `run-worker-once` run from the host with `uv run --env-file .env`, against the published database port. See `docs/dev-services.md`, "Train, register, and score".
- `docker-compose down -v` deletes the `chi-pgdata` volume and all local data. Use it only to reset the development database on purpose.

## Authentication and security

| Tool | Status | Why | Where |
|---|---|---|---|
| Per-organisation API keys (HMAC-SHA256 with pepper) | Used | Only a hash is stored, and a stolen database dump cannot be used to forge keys without the pepper. The organisation comes from the key, never from request parameters. | `app/security/api_keys.py`, `api_keys` table |
| Dev tokens (`AUTH_MODE=dev_tokens`) | Used, demo only | Local-only authentication from a gitignored file, with no revocation and no isolation between token holders. Not an authentication system for production tenants, and the README says so. | `app/security/dev_tokens.py`, `config/dev_tokens.example.json` |
| Ruff `S` rules (bandit-style) | Used | Flags insecure patterns such as unsafe SQL string building, and each exception needs an explicit `noqa`. | `pyproject.toml` |

## Quality tooling

| Tool | Status | Why | Where |
|---|---|---|---|
| pytest | Used | Unit, integration, and security tests. Integration tests are marked and need the Compose database. | `tests/`, `pyproject.toml` (`integration` marker) |
| httpx | Used | HTTP client behind FastAPI's `TestClient`. | `tests/integration/test_api_auth.py` |
| Throwaway databases in tests | Used | Each integration test run creates and drops its own database, so tests never touch the development database. | `tests/conftest.py` |
| Ruff | Used | One tool for linting (`E,F,I,B,UP,S,SIM`) and formatting, with line length 100. | `pyproject.toml`, `make lint` |
| mypy (strict) | Used | Type checking on `app/`, which catches errors before runtime. | `pyproject.toml`, `make typecheck` |
| OpenAPI export and check | Used | `artifacts/openapi.json` is generated from the app, and `export-openapi --check` fails if the committed file differs. This keeps the written contract and the running API in step. | `app/cli.py`, `artifacts/openapi.json` |
| Postman CLI | Available, not used yet | Installed on this machine for API engineering work, such as contract tests against the exported OpenAPI. Nothing is pushed to a Postman cloud workspace without your consent. | Host machine |

## Design patterns that tie the tools together

| Pattern | Why | Where |
|---|---|---|
| Tenant repository: RLS plus an explicit `org_id` filter in every query | Two independent layers, so a mistake in one does not leak data. | `app/db/repository.py` |
| Raw landing, then normalised events | The original payload is kept, so parsing can be audited and replayed. | `raw_source_records`, `source_events` |
| Seeded, per-organisation generator with a committed canonical fixture | Reproducible data, and resizing one organisation does not change another's files. Seeding verifies checksums before inserting. | `app/ingestion/generators/`, `app/ingestion/canonical.py`, `fixtures/canonical/` |
| Ground truth kept apart from product code | The model cannot be graded against its own assumptions. An import-scan test enforces that ingestion never reads ground truth or the defect file. | `tests/unit/test_generator.py` |

## Planned but not yet present

- SHAP, if added (not a dependency today).
- Mock email adapter and outbox (Phase 5). Real email sending is disabled by default and not implemented.
- Postman collection for contract testing, once API endpoints return real data (Phase 4).
