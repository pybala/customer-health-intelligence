# Phase 1 Setup and Changes

Status: Phase 1 is closed with three criteria explicitly unmet (see [Phase 1 closure](#phase-1-closure)). Scaffolding, Compose, migrations 0001 and 0002, API key issuance, tenant-aware auth, route stubs, and the OpenAPI export are in place and tested.

This document records the environment, how to reproduce the setup from scratch, every notable change made in Phase 1, and the verification results from the latest run.

## Environment

| Tool | Version | Notes |
|---|---|---|
| macOS | Darwin 25.6.0 (arm64) | Development machine |
| Python (local) | 3.13.11 at `/opt/homebrew/bin/python3.13` | Pinned by `.python-version` |
| Python (container) | 3.13.18 (`python:3.13-slim`) | Set in `Dockerfile` |
| uv | 0.13.6 | Dependency and venv management |
| Colima | Docker daemon 27.4.0 | Started with `colima start --cpu 4 --memory 8 --disk 100` |
| docker-compose | v2.29.7 (standalone binary) | The `docker compose` plugin is not registered on this machine |
| PostgreSQL | 16 (`postgres:16`, 16.14 in container) | Compose service `db` |

Local PostgreSQL 13 listens on port 5432, and an unrelated SSH tunnel listens on 5433. Both were left untouched.

## Repository layout

```
.
├── app/
│   ├── api/
│   │   ├── dependencies.py     get_settings, get_engine, get_principal, CurrentPrincipal
│   │   ├── errors.py           ApiProblem, RFC 9457 problem+json handler
│   │   ├── main.py             create_app(settings=None) factory; dev-token startup checks
│   │   └── routes/
│   │       ├── health.py       GET /api/v1/health (public)
│   │       └── v1.py           11 authenticated stub endpoints (501 after auth)
│   ├── db/
│   │   ├── models.py           SQLAlchemy 2.0 models
│   │   └── session.py          make_engine, tenant_session, missing_organizations
│   ├── security/
│   │   ├── api_keys.py         issue_key, split_token, hash_secret, verify_secret
│   │   ├── dev_tokens.py       load_dev_tokens, authenticate_dev_token, audit_actor_id (demo only)
│   │   └── principal.py        authenticate, Principal (org_id, actor_id, role)
│   ├── cli.py                  create-org, issue-key, init-dev-tokens, export-openapi
│   ├── config.py               Settings (pydantic-settings, fail-closed)
│   └── logging_setup.py        JSON log formatter
├── artifacts/
│   ├── .gitkeep
│   └── openapi.json            Exported OpenAPI spec (drift-checked)
├── docker/postgres/initdb/
│   └── 01-roles.sh             Creates chi_owner and chi_app on first start
├── config/
│   ├── dev_tokens.example.json Template. The real dev_tokens.json is gitignored.
│   └── dev_tokens.json         Local demo tokens (created by init-dev-tokens, mode 600)
├── docs/                       Phase 0 design docs, ADR-022, the simplification review, and this file
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_roles_and_extensions.py
│       └── 0002_tenancy_and_auth.py
├── tests/
│   ├── conftest.py             Throwaway migrated test database per session
│   ├── unit/                   test_api_keys.py, test_config.py, test_dev_tokens.py, test_dev_tokens_cli.py
│   ├── integration/            test_api_auth.py, test_dev_token_auth.py, test_cli.py, test_migrations.py
│   └── security/               test_tenant_isolation.py, test_grant_matrix.py
├── .env.example                Template for local secrets and ports
├── .env                        Local secrets (gitignored, never printed)
├── .python-version             3.13
├── .gitignore
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── pyproject.toml
└── uv.lock                     39 packages, resolved for Python 3.13
```

The `app/ingestion`, `app/features`, `app/ml`, `app/agent`, `app/notifications`, and `app/dashboard` directories exist as empty skeletons. Their contents belong to later phases. There is no `app/worker` package yet.

## Setup from scratch

Run these from the project root.

1. Start the Docker daemon.
   ```
   colima start --cpu 4 --memory 8 --disk 100
   ```

2. Create the local environment file. The `make env` target does this and refuses to overwrite an existing `.env`.
   ```
   make env
   ```
   Fill in the `CHANGE_ME` values. The generated `.env` already holds random secrets for the database roles and the API key pepper. Do not paste its values into tickets, logs, or chat.

3. Install dependencies on Python 3.13.
   ```
   uv sync
   ```
   `.python-version` selects 3.13, and `requires-python` is `==3.13.*`.

4. Start the database and apply migrations.
   ```
   docker-compose --env-file .env up -d db migrate
   ```
   `migrate` runs `alembic upgrade head` as `chi_owner` and exits 0 on success.

5. Start the API.
   ```
   docker-compose --env-file .env up -d api
   ```

6. Run the checks.
   ```
   uv run ruff check . && uv run ruff format --check .
   uv run mypy
   uv run --env-file .env pytest
   uv run --env-file .env python -m app.cli export-openapi --check
   ```

To reset the dev database to a clean state, run `docker-compose --env-file .env down -v`, then repeat steps 4 and 5. This deletes all local dev data in the `chi-pgdata` volume.

## Configuration

Settings are read from the environment (or `.env`). `app/config.py` fails closed: startup stops if a required value is missing or invalid.

| Variable | Purpose | Default or constraint |
|---|---|---|
| `POSTGRES_PASSWORD` | Admin password for the Postgres superuser used by the container and the test fixture | Required by Compose (`:?`) |
| `CHI_OWNER_PASSWORD` | Password for `chi_owner` | Required by Compose |
| `CHI_APP_PASSWORD` | Password for `chi_app` | Required by Compose |
| `DB_PORT` | Host port mapped to Postgres 5432 | `5434` |
| `API_PORT` | Host port mapped to the API | `8000` |
| `DATABASE_URL` | Runtime connection, as `chi_app` | Must be `postgresql+psycopg://` |
| `MIGRATION_DATABASE_URL` | Migration and operator connection, as `chi_owner` | Must be `postgresql+psycopg://` |
| `API_KEY_PEPPER` | HMAC key for API key hashing | Required, at least 32 characters |
| `AUTH_MODE` | `api_keys` (hashed keys) or `dev_tokens` (local demo, **not production-ready**) | Code default `api_keys`; `.env.example` sets `dev_tokens`; Compose passes `${AUTH_MODE:-api_keys}` |
| `DEV_TOKENS_FILE` | Path to the dev token file | Default `config/dev_tokens.json`; Compose mounts `./config` read-only at `/run/chi` |
| `OUTREACH_MODE` | Outreach behaviour | `dry_run` (default) or `mock_send` |
| `EMAIL_ADAPTER` | Email adapter | `mock` only |
| `LOG_LEVEL` | Log level | Standard level names |

Port 5434 replaces 5432 and 5433 because those ports are in use on this machine. The same value appears in `.env.example`, `.env`, and `docker-compose.yml`.

## Database roles

`docker/postgres/initdb/01-roles.sh` runs on first volume initialisation. It creates two roles, each `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE`:

| Role | Purpose | Privileges |
|---|---|---|
| `chi_owner` | Owns the schema, runs migrations, runs operator CLI commands | Owns the database and all tables. Subject to RLS because tables use `FORCE ROW LEVEL SECURITY`. |
| `chi_app` | Runtime role for the API | `CONNECT`, plus the grants listed under migration 0002 |

The initdb script runs only on a fresh volume. Changing these roles on an existing volume requires `down -v`.

### Removing `chi_scheduler` from an existing local database (manual, not run)

`chi_scheduler` was removed from configuration by ADR-022 (change 14). A volume created before that change still has the role. It is harmless: it holds no table grants, owns nothing, and has no sessions. A fresh volume never creates it. Nothing in the application or startup drops it.

Checked read-only against the dev database on 2026-10-29:

| Check | Result |
|---|---|
| Objects owned by the role | 0 |
| Table grants | 0 |
| Default ACL entries | 0 |
| Open sessions | 0 |
| Only dependency (`pg_shdepend`) | `CONNECT` on database `chi` (ACL entry `chi_scheduler=c/chi_owner`) |

To remove it from a local database, run this as the `postgres` superuser. It is destructive and has not been run:

```sql
REVOKE CONNECT ON DATABASE chi FROM chi_scheduler;
DROP ROLE chi_scheduler;
```

Do not run it against any shared or production database. Skipping it has no effect on the application or the tests.

## Migrations

### 0001: roles and verification

`0001_roles_and_extensions.py` verifies that `chi_app` exists in `pg_roles` and raises if it does not. It creates no tables. The `pgcrypto` extension was removed because nothing uses it. The downgrade is a no-op.

### 0002: tenancy, auth, audit, idempotency

Creates the following tables.

- `organizations`: `status` is checked against `active` or `suspended`.
- `api_keys`: `role` is checked against `csm`, `admin`, or `readonly`. Indexed on `org_id`.
- `audit_events`: `bigserial` primary key, `detail` as JSONB, indexed on `(org_id, created_at)`.
- `idempotency_keys`: unique on `(org_id, key)`.

Row-level security:

- `api_keys`, `audit_events`, and `idempotency_keys` have `ENABLE` and `FORCE` RLS. The `tenant_isolation` policy applies to both `USING` and `WITH CHECK`, comparing `org_id` with `NULLIF(current_setting('app.current_org_id', true), '')::uuid`. If no tenant is set, the comparison yields no rows.
- `organizations` has `ENABLE` and `FORCE` RLS, a `own_organization` SELECT policy, and an `operator_create_organization` INSERT policy for `chi_owner` only.
- `api_keys` also has an `api_key_lookup` SELECT policy keyed on `app.api_key_prefix`. This lets authentication find a key by prefix before a tenant is known (ADR-010). It replaces a SECURITY DEFINER design that would have returned no rows under `FORCE`.

Append-only audit:

- `reject_audit_mutation()` raises on any UPDATE or DELETE, and the `audit_events_immutable` trigger calls it. The trigger applies to every role, including the owner.

Grants to `chi_app`:

| Table | Privileges |
|---|---|
| `organizations` | SELECT |
| `api_keys` | SELECT, and UPDATE on `last_used_at` only |
| `audit_events` | SELECT, INSERT, and USAGE, SELECT on its sequence |
| `idempotency_keys` | SELECT, INSERT, UPDATE, DELETE |

The grants are narrower than the initial draft. `chi_app` has no INSERT or UPDATE on `api_keys`, and it has no DELETE on `audit_events`.

`tests/security/test_grant_matrix.py` reads the catalog and asserts the exact privilege set for `chi_app` and `chi_scheduler`. A migration that widens or narrows a grant fails the test until the expected set is updated on purpose.

## API

### Authentication

- Keys have the form `chi_<prefix>.<secret>`.
- The secret is stored as `HMAC-SHA256(pepper, secret)` in hex. The plaintext is shown once, at issuance.
- Verification uses `hmac.compare_digest`.
- `authenticate()` sets `app.api_key_prefix`, reads one row, rejects revoked keys and hash mismatches, sets `app.current_org_id` from that row, and updates `last_used_at`. The organization always comes from the key row, never from the request.
- A missing or malformed `Authorization` header returns 401 with `WWW-Authenticate: Bearer realm="chi"`.

### Development tokens (`AUTH_MODE=dev_tokens`)

**Not production-ready.** For the local assessment and demo only. The application logs a warning at startup, and `AUTH_MODE` defaults to `api_keys` when unset.

- A JSON file (`DEV_TOKENS_FILE`) lists tokens, each with a unique `label`, a `token` of at least 24 characters, an `org_id`, and a `role` (`csm`, `admin`, `readonly`). Create it with `python -m app.cli init-dev-tokens --token LABEL:ORG_ID:ROLE ...`. The command writes random tokens with file mode 600 and refuses to overwrite without `--force`. The file is gitignored, and `config/dev_tokens.example.json` is a template.
- The token decides the organisation. The API never reads an organisation ID from a header, path, query, or body.
- Startup fails if the file is missing or invalid, or if any token names an organisation that does not exist. Organisations are checked one at a time under their own tenant context, so RLS stays on.
- `readonly` tokens get 403 on any method other than GET, HEAD, and OPTIONS. `csm` and `admin` behave the same for now because no admin-only endpoint exists.
- **Audit identity.** `Principal.actor_id` is a UUIDv5 derived from the token label, never from the secret, so rotating a token keeps the same identity. It identifies the label, not an authenticated person. It is the value future code writes to `audit_events.actor_key_id`; nothing writes audit rows yet.
- Token secrets are never logged.
- **Limits.** Tokens are plaintext in a local file, not hashed, and not individually revocable. Anyone holding a token has that organisation's access. Nothing isolates token holders from each other.
- The `api_keys` table, key code, and `AUTH_MODE=api_keys` are unchanged and still tested.

### Endpoints

Errors use RFC 9457 `application/problem+json`, with type URIs under `https://chi.example.test/problems/`.

| Method and path | Auth | Phase 1 behaviour |
|---|---|---|
| GET `/api/v1/health` | Public | 200 `{"status":"ok","database":"ok"}`, or 503 if `SELECT 1` fails |
| GET `/api/v1/accounts` | Key | 501 after auth |
| GET `/api/v1/accounts/{account_id}` | Key | 501 after auth |
| GET `/api/v1/accounts/{account_id}/predictions` | Key | 501 after auth |
| GET `/api/v1/predictions/{prediction_id}` | Key | 501 after auth |
| GET `/api/v1/predictions/{prediction_id}/evidence` | Key | 501 after auth |
| GET `/api/v1/accounts/{account_id}/actions` | Key | 501 after auth |
| POST `/api/v1/actions/{action_id}/approvals` | Key | 501 after auth (201 declared in the spec) |
| GET `/api/v1/outbox` | Key | 501 after auth |
| GET `/api/v1/data-quality` | Key | 501 after auth |
| GET `/api/v1/models` | Key | 501 after auth |
| GET `/api/v1/models/{version}/evaluation` | Key | 501 after auth |

Every stub returns 401 without a key. The integration test checks this for all 12 routes.

### Operator CLI

`python -m app.cli` runs as `chi_owner` through `MIGRATION_DATABASE_URL`.

- `create-org --slug <slug> --name <name>`: creates an organization.
- `issue-key --org-id <uuid> --role csm|admin|readonly`: creates a key and prints the plaintext once.
- `export-openapi [--check]`: writes `artifacts/openapi.json`, or with `--check`, exits non-zero if the file differs from the app.

The CLI commands are not covered by tests yet.

## Change log

Phase 0 (design, pre-code) included changes to the solution brief, architecture, schema, synthetic data, ML design, worker design, implementation plan, and ADRs 001 to 021. The main corrections were:

- The churn definition was reversed. It is now non-renewal at the scheduled renewal date, or cancellation effective within 30 days after.
- The worker outbox was written before approval. It is now written only after approval.
- A SECURITY DEFINER lookup under FORCE RLS would return no rows. Replaced by the narrow `api_key_lookup` policy (ADR-010).
- Cross-tenant claim policies were added for worker use (ADR-021). Not implemented until Phase 5.

Phase 1 implementation changes, in order:

1. Scaffolding: pyproject, uv lock, Makefile, `.gitignore`, `.env.example`, `alembic.ini`, skeleton directories.
2. Compose: `db`, `migrate`, and `api` services, with `:?` markers for required secrets.
3. Port moved from 5432 to 5434 because of local conflicts.
4. `make env` changed to an if/else guard so it no longer prints "Created" when `.env` exists.
5. Migration 0001 changed to verify roles instead of creating `pgcrypto`.
6. Migration 0002 grants narrowed during review. The scheduler grant was removed, and `api_keys` was limited to SELECT and UPDATE on `last_used_at`.
7. Health endpoint moved from `/health` to `/api/v1/health`, and excluded from the auth-required test.
8. Alembic `path_separator = os` added to remove a deprecation warning.
9. Ruff fixes: E501 in `v1.py`, SIM117 nested `with`, and S608 f-string SQL in tests.
10. Python version moved from 3.11 or 3.12 to 3.13 in `pyproject.toml` (`requires-python`, ruff `target-version`, mypy `python_version`), in `Dockerfile` (`python:3.13-slim`), and in the new `.python-version`. The lock file and venv were regenerated on CPython 3.13.11.
11. Dev database rebuilt with `docker-compose down -v`. This removed an over-broad `GRANT ALL ON ALL TABLES TO chi_app` and seed rows that had been added by hand during verification.
12. Tests added: grant matrix (6), operator CLI (9), and migration round trip (2).
13. Tests added for closure: every authenticated route returns 501 with a valid key, insert without tenant context is rejected, app role cannot UPDATE `audit_events`, scheduler role cannot UPDATE or DELETE `audit_events`. The scheduler URL in `tests/conftest.py` now uses `CHI_SCHEDULER_PASSWORD` instead of reusing the owner's password. The cross-tenant `audit_events` check now asserts only org A is visible, not just that org A has a row. Test database setup in `tests/conftest.py` was split into `throwaway_database()` and `migration_env()` so the migration test can reuse them. The `TestDatabase` dataclass was renamed `ThrowawayDatabase` so pytest does not try to collect it.
14. **ADR-022 simplification (approved 2026-10-29, decisions D1 to D5).**
    - Added `AUTH_MODE` and `DEV_TOKENS_FILE` settings, `app/security/dev_tokens.py`, the `init-dev-tokens` CLI command, `missing_organizations` in `app/db/session.py`, and startup checks and the warning in `create_app`.
    - `get_principal` dispatches on `AUTH_MODE` and returns 403 for `readonly` tokens on unsafe methods. It also applies to `api_keys` mode.
    - `Principal.key_id` renamed `actor_id`, because it is not always an API key ID. No code outside `app/security` used it.
    - Removed `chi_scheduler` from `01-roles.sh`, `docker-compose.yml`, `.env.example`, `tests/conftest.py`, `test_grant_matrix.py`, and `test_tenant_isolation.py`. The role was not dropped from the existing dev database; see Database roles.
    - Compose passes `AUTH_MODE` (default `api_keys`) and mounts `./config` read-only.
    - Added `config/dev_tokens.example.json` and gitignored `config/dev_tokens.json`.
    - Not changed: migrations, `api_keys`, RLS policies, the audit trigger, `api_key_lookup`, grants.
    - Docs: ADR-022 added; ADR-004, 010, 019, 021 and `schema.md`, `architecture.md`, `worker-design.md`, `implementation-plan.md` annotated. `schema.md` and `worker-design.md` still hold the original design text beneath their notes.

## Verification

Latest run, on Python 3.13.11 locally and 3.13.18 in the container.

| Check | Command | Result |
|---|---|---|
| Lint | `uv run ruff check .` and `uv run ruff format .` | All checks passed, 39 files formatted |
| Types | `uv run mypy` | Success, no issues in 15 source files (strict, `app/` only) |
| Tests | `uv run --env-file .env pytest` | 96 passed, 1 warning |
| Unit tests | `pytest -m "not integration"` | 39 passed, 57 deselected |
| Integration tests | `pytest -m integration` | 57 passed, 39 deselected |
| OpenAPI | `make check-openapi` | Matches the application |
| Image | `docker-compose --env-file .env up -d --build migrate api` | Built on `python:3.13-slim` (earlier run) |
| Migrate | container exit code | 0, at revision `0002` (earlier run) |
| Health | `GET http://127.0.0.1:8000/api/v1/health` | 200 (earlier run) |
| Auth | `GET http://127.0.0.1:8000/api/v1/accounts` without key | 401 (earlier run) |
| Dev DB grants | `information_schema.role_table_grants` for `chi_app` | Only the grants listed under migration 0002 (earlier run) |
| Compose with dev tokens | not run after ADR-022 | The running `api` container predates this change. Rebuild and set up tokens before using it (see Next steps) |

Test count by file:

| File | Tests | Covers |
|---|---|---|
| `tests/unit/test_api_keys.py` | 9 | Key format, hashing, pepper, malformed tokens |
| `tests/unit/test_config.py` | 6 | Settings defaults and fail-closed validation |
| `tests/unit/test_dev_tokens.py` | 18 | Token file validation, token to organisation and role, wrong tokens, label-derived audit identity |
| `tests/unit/test_dev_tokens_cli.py` | 6 | `init-dev-tokens`: loadable output, mode 600, no overwrite without `--force`, bad specs write nothing |
| `tests/integration/test_dev_token_auth.py` | 16 | Valid and invalid dev tokens, 401 on every route, readonly 403, per-token organisation, startup warning, startup failure for a missing organisation or file, `api_keys` mode ignores dev tokens |
| `tests/integration/test_api_auth.py` | 11 | Health, 401 on every route, 501 on every route with a valid key, revoked and wrong-pepper keys |
| `tests/integration/test_cli.py` | 9 | `create-org`, `issue-key`, `export-openapi`, drift detection, committed spec |
| `tests/integration/test_migrations.py` | 2 | Full upgrade, downgrade to base, upgrade again; audit function removed on downgrade |
| `tests/security/test_tenant_isolation.py` | 14 | RLS, FORCE, fail-closed reads and inserts, cross-tenant reads and writes, append-only audit for app and owner |
| `tests/security/test_grant_matrix.py` | 5 | Exact `chi_app` privileges, no DDL, no RLS changes |

The test fixture creates a throwaway database named `chi_test_<hex>`, runs `alembic upgrade head` against it, and drops it when the session ends. Migration tests create their own database, so they do not touch the shared one.

## Phase 1 closure

Each criterion from `docs/implementation-plan.md`, with its status.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `docker compose up -d db migrate` completes on a clean machine | Met | Clean volume rebuilt with `down -v`; `migrate` exited 0 at `0002` |
| 2 | `alembic upgrade head` and `downgrade base` both succeed on a fresh database | Met | `test_upgrade_downgrade_upgrade_round_trip` |
| 3 | `chi_app` is non-superuser, non-BYPASSRLS, not a table owner; tenant tables have RLS and FORCE | Met | `test_app_role_is_not_superuser_and_cannot_bypass_rls`, `test_app_role_owns_no_tenant_tables`, `test_every_tenant_table_has_rls_and_force` |
| 4 | No tenant context: `SELECT` returns zero rows, `INSERT` rejected | Met | `test_no_tenant_context_returns_no_rows`, `test_insert_without_tenant_context_is_rejected` |
| 5 | Org A cannot read org B by id, by SQL under org A context, or by query without `org_id` predicate | Partly met | SQL-level: `test_cross_tenant_reads_are_invisible` checks by id, by `org_id` predicate, and that the only `audit_events` org seen is A. API-level: unmet, see below |
| 6 | `audit_events` rejects `UPDATE` and `DELETE` for the app and owner roles | Met | `test_app_role_cannot_update_audit_events`, `test_app_role_cannot_delete_audit_events`, `test_audit_events_reject_update_and_delete_even_for_owner`. The scheduler-role test was removed with the role (ADR-022) |
| 7 | API key hashes use HMAC with pepper; startup fails without `API_KEY_PEPPER` | Met, for `AUTH_MODE=api_keys` only. The demo default is dev tokens, which are not hashed (ADR-022) | `test_hash_is_keyed_by_pepper`, `test_wrong_pepper_fails_verification`, `test_missing_pepper_is_rejected`, `test_short_pepper_is_rejected` |
| 8 | `artifacts/openapi.json` exported and committed; drift check fails on difference | Met | `test_export_openapi_check_detects_drift`, `test_committed_openapi_matches_application`, `make check-openapi` |
| 9 | Every endpoint returns 401 without a key and a stub with a key; no tenant data | Met, in both modes | `test_every_v1_route_requires_a_key`, `test_every_v1_route_returns_501_with_a_valid_key`, `test_every_v1_route_requires_a_dev_token` |
| 10 | `ruff`, `mypy --strict` on `app/`, `pytest` pass; output reported verbatim | Met | `make lint`, `make typecheck`, `make test` (see Verification) |

### Unmet or deviated

These are not met in Phase 1. Each is listed with the reason.

1. **Criterion 5, API level.** The plan asks for an org A key to get 404 or zero rows for an org B resource through the API. No resource endpoints return data until Phase 4, so this cannot be tested through HTTP yet. The SQL-level check passes. Carry the API test into Phase 4.
2. **Pydantic response models.** The Phase 1 scope asks for "Pydantic response models for every endpoint." The stubs return no body and declare no response model, so they are missing from the spec. Add these when each endpoint is implemented in Phase 4.
3. **Tenant resolution middleware.** The scope says "tenant resolution middleware." Tenant resolution is a FastAPI dependency (`CurrentPrincipal` in `app/api/dependencies.py`), not ASGI middleware. The dependency gives the same guarantee: the org comes from the key row only. Recorded as a design deviation, not a gap in isolation.

### Other gaps

- `make up`, `make migrate`, and `make env` were not run through make in this pass. Their underlying commands were run directly.
- **Dev-token org scoping through the API is only partly tested.** The tests prove each token resolves to its own organisation and that a client-supplied organisation ID changes nothing, but every data endpoint is still a 501 stub that reads no rows. The end-to-end check that token A cannot read organisation B's data through HTTP belongs with the Phase 4 endpoints.
- Compose `api` has not been rebuilt or run with `AUTH_MODE=dev_tokens` after this change. Only the test suite exercised it.
- `chi_scheduler` still exists in the current dev database (harmless; see Database roles).
- The project is not a git repository. `.gitignore` has no effect yet, and `.env` holds real local secrets. No commits have been made, and no git commands should be run in this project.
- The Phase 0 open questions are unresolved: business questions in `solution-brief.md`, ML requirements, and the open decisions in `adr.md` (shared or per-tenant model, per-action or batch approval, email content, prediction horizon).
- Colima was started manually and is still running.

## Next steps

- To use dev tokens in Compose: create the organisations with `create-org`, run `init-dev-tokens` with their IDs, then `make up` (rebuild) so the container mounts `./config`.
- Phase 2: synthetic data and ingestion. Requires a go-ahead, and the Phase 0 open questions above should be answered first if they affect the generator.
- Phase 4: carry the API-level cross-tenant test and the response models into the resource endpoints.
