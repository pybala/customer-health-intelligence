# Customer Health Intelligence

A local demo platform that scores SaaS accounts for churn risk over a 90-day horizon, explains each score from source events, and queues CSM outreach for human approval. It runs on one machine with Docker Compose.

Status: Phase 2 (data and ingestion) and Phase 3 Parts 1 and 2 (features, offline training, evaluation, model persistence) are implemented. The CSM dashboard is built as an API client, except the model metadata page (see `docs/implementation-plan.md`, Phase 6). A worker that runs once on demand (`make run-worker-once`) is built. There is no scheduler or recurring loop, and the explanation module is not built.

**Current model result: the selected model is the constant prior.** Neither learned candidate passed the validation gate, so every account receives the same score and the system cannot rank accounts by churn risk. See `docs/model-card.md`.

## The dataset

One canonical dataset serves demo, seeding, ingestion, ML, and tests. It is committed in `fixtures/canonical/`.

| | Count |
|---|---|
| Organisations | 3 (`acme-saas` 24 accounts, `northwind-cloud` 16, `globex-data` 10) |
| Accounts | **50** |
| Emitted source records (before deduplication) | **1,000** |
| Unique records after deduplication (expected database count) | **975** |
| Emitted per source: Salesforce / Gmail / Zendesk / product usage / Stripe | 150 / 150 / 150 / 450 / 100 |
| Intentional exact duplicates (removed at landing, logged as `duplicate` issues) | 25 |
| Size on disk | about 476 KB |

The dataset is synthetic and uses reserved example domains. It contains injected missing records, duplicates, late and out-of-order arrivals, identity mismatches, and timezone-less timestamps.

**It is too small for model evaluation.** The labelled test window has 12 accounts and one churned renewal term (13 positive example rows, all from that term). The validation window has two churned terms. Use it to exercise the pipeline and the workflow, not to measure model quality. See `docs/ml-design.md` sections 2.1 to 2.3 and section 12.

## Commands

Requires `uv`, Python 3.13, and Docker Compose. Copy `.env.example` to `.env` and replace the `CHANGE_ME` values first (`make env`).

```
make up                   # start the database, run migrations, start the API
docker-compose up -d --build dashboard   # CSM dashboard at http://127.0.0.1:8501 (also starts the API)
make generate-fixtures    # regenerate and validate fixtures/canonical (only needed after a config change)
make seed                 # load the canonical fixture for all demo organisations (idempotent)
uv run --env-file .env python -m app.cli train      # train, evaluate, write artifact and candidate rows
uv run --env-file .env python -m app.cli register --model-version logreg-v1   # mark it registered
make run-worker-once      # score accounts for WORKER_ORG_IDS, propose actions, simulated delivery
make test                 # all tests (unit tests and integration tests)
make test-unit            # unit tests only
make test-integration     # integration tests only (needs the database)
make lint
make typecheck
```

`seed` checks every fixture file against its manifest checksum before inserting anything. It creates the three demo organisations if they are missing, then ingests their files as the application role. Running it again adds no rows.

Training, registration, and scoring run on the host, not in Docker. Do not re-run `train` against an already registered model. See `docs/dev-services.md`, "Train, register, and score", for prerequisites, verification, and the warning.

`generate-fixtures` is deterministic: the same seed and configuration produce byte-identical files, and a test checks the committed fixture against a fresh generation.

## Documentation

- `docs/dev-services.md`: how to run the Compose stack, seed, authenticate, and test
- `docs/solution-brief.md`: problem, scope, and decisions
- `docs/architecture.md`: modules and boundaries
- `docs/synthetic-data.md`: how the canonical dataset is generated
- `docs/phase-2-data-and-ingestion.md`: ingestion, identity resolution, and quality issues
- `docs/ml-design.md`: target, leakage rules, evaluation protocol, and the data validity check
- `docs/model-card.md`: the current model result, its limitations, and the selection audit trail
- `docs/implementation-plan.md`: phases and acceptance criteria
- `docs/tools-and-technologies.md`: every tool, why it is used, and where

## Data notes

- No real data. Names come from a fixed synthetic lexicon, and domains use `example.test` or similar reserved TLDs.
- Ground truth (`ground_truth.ndjson`) is read only by the offline training and evaluation workflow. Ingestion, features, the API, the dashboard, and the worker never read it. The defect log (`defects.ndjson`) is read only by the generator and by the ingestion tests, as the expected answer to check against. The ingestion and feature code never read it. Import-scan and seed-path tests enforce this.
- Each account has a `renewal_date` (the pending renewal after the anchor, null if there is none). It selects which accounts fall in the 90-day scoring window. It is not a model feature, and the renewal outcome label is never shown at runtime.
- This is a synthetic assessment and demo dataset: 50 accounts, 1,000 emitted records, and 975 unique events after deduplication. It is not evidence of production model performance.
