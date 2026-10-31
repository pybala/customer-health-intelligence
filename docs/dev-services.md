# Running the dev services

Four Compose services make up the local stack: `db` (PostgreSQL 16), `migrate` (runs Alembic once and exits), `api` (FastAPI on port 8000), and `dashboard` (Streamlit on port 8501, started separately; see below). The worker has no container yet, so seeding, training, scoring, and tests run from the host with `uv` (see "Train, register, and score").

## One-time setup

1. Install `uv`, Python 3.13 and Docker. Compose is run as the standalone `docker-compose` binary.
2. Create the environment file and replace every `CHANGE_ME` value:

   ```
   make env
   ```

   Generate secrets with `openssl rand -hex 24`. `API_KEY_PEPPER` must be at least 32 characters.
3. Install Python dependencies:

   ```
   uv sync
   ```

## Start the stack

```
make up
```

This builds the images and starts `db`, `migrate` and `api`. `migrate` waits for the database to be healthy, applies migrations and exits. `api` starts after `migrate` succeeds.

Check that it is up:

```
docker-compose ps
curl -s http://127.0.0.1:8000/api/v1/health
```

`docker-compose ps` should show `db` as healthy and `api` as running. `migrate` shows as exited with code 0, which is expected.

Ports (set in `.env`):

| Service | Host address | Default |
|---|---|---|
| PostgreSQL | `127.0.0.1:${DB_PORT}` | 5434 |
| API | `127.0.0.1:${API_PORT}` | 8000 |
| Dashboard | `127.0.0.1:${DASHBOARD_PORT}` | 8501 |

Stop everything without deleting data:

```
make down
```

`make down` keeps the `chi-pgdata` volume, so the database contents survive.

## Start the dashboard

```
docker-compose up -d --build dashboard
```

This also starts `api` if it is not running. Open http://127.0.0.1:8501.

The dashboard calls the API and never connects to the database. It has five screens:

- **Portfolio:** health counts (healthy, at-risk, critical, other), a health distribution chart, accounts needing attention, upcoming renewals, and actions awaiting a decision.
- **Accounts:** all accounts, with search, a health filter, and sorting.
- **Account detail:** health banner, renewal, model risk score, policy reasons, feature values grouped by source with their cutoff time, cited source events, recommended actions, and recent activity.
- **Actions:** proposed actions with approve and reject controls, and the status of every action.
- **Data quality:** data-quality issues grouped by category.

Notes:

- Enter an API token in the sidebar. A blank field uses `DASHBOARD_API_TOKEN` from the environment. Under `api_keys`, use a key issued by `issue-key`.
- Scores are labelled "Model risk score (not a calibrated probability)". Calibration is deferred (see `docs/model-card.md`).
- Delivery is simulated. Approving records a simulated outbox row, and nothing is emailed.
- The outbox status on the Actions screen reflects only the first 200 rows the API returns.
- The theme is fixed to light in `docker-compose.yml`, and the theme picker is hidden.
- "Reload data" clears the loaded data and fetches it again from the API.
- The model metadata page is not built.

## Load the demo data

The database starts empty. Load the canonical dataset (50 accounts, 1,000 emitted records, three organisations):

```
make seed
```

`seed` runs on the host as the owner role (`MIGRATION_DATABASE_URL`) and the application role (`DATABASE_URL`). It creates the three demo organisations if they are missing, verifies each fixture file against its checksum, and ingests it. Running it again adds no rows. It prints one JSON line per organisation.

To regenerate the fixture after a config change:

```
make generate-fixtures
```

Run `make test` afterwards. The fixture is checksum-verified, so any change to the generator shows up as a diff in `fixtures/canonical/`.

## Authenticate against the API

Choose one mode in `.env` (`AUTH_MODE`).

**`dev_tokens` (local demo only).** Plaintext tokens in a file mounted into the API container. Not production-ready.

1. Find the organisation IDs. The demo organisations use deterministic IDs derived from their slugs:

   ```
   uv run python -c "from app.ingestion.canonical import org_id_for; print(org_id_for('acme-saas'))"
   ```

2. Write the token file (mode 600, git-ignored):

   ```
   uv run --env-file .env python -m app.cli init-dev-tokens \
     --token acme-csm:<acme-org-id>:csm
   ```

   Use `--force` to overwrite an existing file. The file path comes from `DEV_TOKENS_FILE`.
3. Restart the API so it reads the file: `docker-compose restart api`.
4. Call the API with the token:

   ```
   curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/v1/accounts
   ```

   The API routes are listed in `http://127.0.0.1:8000/openapi.json`.

**`api_keys` (default).** Hashed, per-organisation keys issued by the operator CLI. Use this for anything beyond a demo:

```
uv run --env-file .env python -m app.cli issue-key --org-id <org-id> --role csm
```

The plaintext key is printed once. Only its hash is stored.

## Train, register, and score

Training, registration, and scoring all run on the host through `uv`, not in Docker. No Compose service runs them: `api` and `dashboard` do not receive `WORKER_ORG_IDS` or `MIGRATION_DATABASE_URL`, and the image does not copy `fixtures/`, which `train` reads. The host reaches the database at `127.0.0.1:${DB_PORT}`.

Only `make run-worker-once` has a Makefile target. `train` and `register` are CLI commands with no target.

### Prerequisites

- `db` is running and migrations are applied through 0006 (`make up`, or `make migrate`).
- `make seed` has been run.
- `.env` sets `DATABASE_URL`, `MIGRATION_DATABASE_URL`, and `API_KEY_PEPPER` (at least 32 characters). Both database URLs must start with `postgresql+psycopg://`. `train` and `register` use the owner role in `MIGRATION_DATABASE_URL`, because `chi_app` has only `SELECT` on `model_versions` and `model_evaluations`.
- `.env` sets `WORKER_ORG_IDS`, a comma-separated list of organisation IDs, for the worker. An empty list or an unknown ID stops the run with an error.
- Optional: `REVIEW_CAPACITY_FRACTION` (default `0.10`). The CLI takes the artifact directory from `--artifacts`, not from `ARTIFACT_ROOT`.

To get an organisation ID:

```
uv run python -c "from app.ingestion.canonical import org_id_for; print(org_id_for('acme-saas'))"
```

### Commands

| Step | Command | Implemented in |
|---|---|---|
| Train and evaluate | `uv run --env-file .env python -m app.cli train` | `train_model` in `app/cli.py`, which calls `train()` in `app/ml/train.py` |
| Register | `uv run --env-file .env python -m app.cli register --model-version logreg-v1` | `register_model` in `app/cli.py`, which calls `mark_registered()` in `app/ml/registry.py` |
| Score and propose actions | `make run-worker-once` | `run_worker_once` in `app/cli.py`, which calls `run_once()` in `app/worker/run.py` |

There is no separate evaluate command and no standalone scoring command.

**`train`.** Fits three candidates (prior, logistic regression, gradient boosting) and picks one with the pre-registered selection rule, using validation data only. Evaluation runs inside `train()` through `app/ml/evaluate.py`. It writes `artifacts/models/logreg-v1/model.joblib` and `metadata.json`, then inserts one `candidate` row in `model_versions` and one test-split row in `model_evaluations` for each of the three organisations. Validation and global test metrics stay in `metadata.json`. The database holds only each organisation's test slice. It trains one shared model across all three organisations and has no per-tenant option. Options: `--fixtures` (default `fixtures/canonical`) and `--artifacts` (default `artifacts/models`). The version name is the constant `MODEL_VERSION` in `app/ml/train.py`, not a flag.

**`register`.** Sets `status = 'registered'` on the named version. It does not retire older versions. `--org <slug>` limits it to one organisation. The default is all three.

**`run-worker-once`.** Options: `--mode dry_run|simulated` (default `dry_run`) and `--artifacts` (default `artifacts/models`). For each organisation in `WORKER_ORG_IDS`, under a Postgres advisory lock, it:

1. Recovers stale outbox locks.
2. Loads the newest `registered` model for that organisation and checks the artifact's SHA-256 before unpickling (`load_artifact` in `app/ml/artifact.py`).
3. Scores every account at today's UTC-midnight cutoff. Only accounts whose renewal falls within 90 days are eligible (`eligibility` in `app/ml/inference.py`).
4. Saves a `predictions` row per account (`record_prediction` in `app/ml/registry.py`). It skips an account that already has a row for the same organisation, account, cutoff, and model.
5. Proposes actions, creates outbox rows for approved actions, and delivers them in simulated mode.

So the worker writes actions and outbox rows as well as predictions. It prints one JSON line per organisation with `status` and `counts` (`scored`, `eligible`, `out_of_window`, `status_*`). It exits 1 if any organisation fails. An organisation with no registered model fails with "no registered model version for this organisation".

**One tenant only.** `run-worker-once` has no `--org` flag. The organisation list comes only from `WORKER_ORG_IDS`. To score one tenant, set that variable to a single ID:

```
WORKER_ORG_IDS=<org-id> uv run --env-file .env python -m app.cli run-worker-once
```

Whether a shell variable overrides the same name in `--env-file` has not been verified. Edit `WORKER_ORG_IDS` in `.env` if the override does not take effect.

### Scheduling

Nothing is scheduled. The repository has no scheduler, cron entry, or loop. A recommended approach, not built: run `make run-worker-once` daily from host cron or launchd. A repeat run on the same day adds no duplicate predictions. Do not schedule `train` (see the warning below).

### Verify

- **Artifact files.** `ls artifacts/models/logreg-v1` shows `model.joblib` and `metadata.json`. `shasum -a 256 artifacts/models/logreg-v1/model.joblib` must equal `artifact_sha256` in `metadata.json`. The worker refuses a file whose digest differs from the registered one.
- **Through the API.** `GET /api/v1/models`, `GET /api/v1/models/{version}/evaluation`, and `GET /api/v1/accounts/{account_id}/predictions`, with a token for the organisation.
- **Through SQL.** Row-level security is forced for every role, so a query with no tenant returns zero rows. Set the tenant inside a transaction:

  ```
  BEGIN;
  SELECT set_config('app.current_org_id', '<org-id>', true);
  SELECT model_version, status, artifact_sha256 FROM model_versions;
  SELECT count(*), max(cutoff_at) FROM predictions;
  COMMIT;
  ```

### Runbook

```
make up
make seed
uv run --env-file .env python -m app.cli train
uv run --env-file .env python -m app.cli register --model-version logreg-v1
make run-worker-once
```

### Warning: do not re-run `train` against a registered model

`train` overwrites `artifacts/models/logreg-v1/` before it inserts into `model_versions`. That table has `UNIQUE (org_id, model_version)`, and the version name is fixed. Reading the code, a second run would likely fail on the insert after the files were already replaced. The new digest would then differ from the registered one, and the worker would refuse to load the model. This comes from reading the code. It has not been run. Before retraining, change `MODEL_VERSION` or retire the existing version, and decide that deliberately.

### Not implemented

A separate evaluate command, a standalone scoring command, a per-tenant flag on `run-worker-once`, any scheduler, and running training or the worker in Docker.

## Tests

Tests create a throwaway database on the running `db` service, so `db` must be up.

```
make test              # all tests
make test-unit         # no database needed
make test-integration  # needs db
make lint
make typecheck
```

The integration suite uses the same fixture as `make seed`, so it does not depend on what is currently in the dev database.

## Operator commands

Run from the host with the env file loaded:

| Command | What it does |
|---|---|
| `make seed` | Load the canonical dataset (idempotent). |
| `make generate-fixtures` | Regenerate and validate `fixtures/canonical/`. |
| `make export-openapi` | Write `artifacts/openapi.json`. |
| `make check-openapi` | Fail if `artifacts/openapi.json` is out of date. |
| `uv run --env-file .env python -m app.cli train` | Train and evaluate the candidates and write the artifact and candidate rows (owner role). |
| `uv run --env-file .env python -m app.cli register --model-version <v>` | Mark a candidate version as registered. |
| `make run-worker-once` | Score accounts, propose actions, and simulate delivery for the organisations in `WORKER_ORG_IDS`. |
| `uv run --env-file .env python -m app.cli create-org --slug <s> --name <n>` | Create a tenant (owner role). |
| `uv run --env-file .env python -m app.cli issue-key --org-id <id> --role <r>` | Issue an API key. |

## Troubleshooting

- **`connection refused` on port 5434.** `db` is not running. Run `docker-compose up -d db`. Check `DB_PORT` in `.env` matches the port the tests use.
- **`set POSTGRES_PASSWORD in .env` (or similar).** Compose refuses to start with an unset secret. Fill in `.env`.
- **`migrate` exits with an error.** Read its logs with `docker-compose logs migrate`. Fix the cause, then `make up` again. Migrations are idempotent.
- **API returns 401 with a dev token.** Confirm `AUTH_MODE=dev_tokens` in `.env`, that `config/dev_tokens.json` exists, and that you restarted `api` after writing it.
- **`WORKER_ORG_IDS is empty` or `references organisations that do not exist`.** Only `run-worker-once` reads this variable. Set it in `.env` to the organisation IDs from `org_id_for`, and run `make seed` first.
- **`no registered model version for this organisation`.** Run `train` and `register` first.
- **`digest ... does not match the registered ...`.** The files in `artifacts/models/<version>/` differ from the registered artifact. See the retraining warning above.
- **Empty database after `make down`.** Expected only if you removed the volume with `docker-compose down -v`. That command deletes all data and needs an explicit decision. Re-run `make seed` afterwards.

## Reset

Reset is destructive. Do not run these without deciding to lose the local database:

```
docker-compose down -v   # deletes the chi-pgdata volume and all rows
make up && make seed     # rebuild from the fixture
```

Nothing in the repository depends on the database contents, because the fixture regenerates them.
