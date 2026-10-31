# Phase 2: Synthetic Data and Ingestion

Status: implemented. Not in this phase: features, model training, API endpoints, the worker loop, outreach policies, the outbox, editable drafts, and the dashboard.

## Approved decisions that shape this phase

| Decision | Effect here |
|---|---|
| One shared model across all three organisations | Data stays separated per organisation in generation, ingestion and storage. Evaluation will be reported overall and per organisation. |
| 90-day horizon: probability the account fails to renew within the next 90 days | See "Prediction target" below. |
| Automatic scoring and queueing, human approval before any send | Not built yet. Nothing in Phase 2 sends or drafts anything. |
| Template-based drafts that a CSM can edit | Recorded in `worker-design.md`. Not built yet. |

## How to run

```
uv run --env-file .env python -m app.cli generate-fixtures
uv run --env-file .env python -m app.cli seed
```

`generate-fixtures` rewrites `fixtures/canonical/` (committed) and validates it. `seed` loads that fixture for all three demo organisations: it creates the organisations if they are missing, checks each file against its manifest checksum before inserting, and runs the normal ingestion pipeline as `chi_app`. Re-running `seed` adds no rows. There is no separate large-data path; tests use the same fixture.

`WORKER_ORG_IDS` must list the IDs of the demo organisations (`uuid5` of each slug, see `app/ingestion/canonical.py`) before the worker starts. Startup fails if the list is empty or contains an unknown ID.

The development database has not been seeded in this phase. Seeding it is a manual step, listed in the implementation notes.

## Data counts (canonical fixture, seed 20261001, anchor 2026-10-01)

| | acme-saas | northwind-cloud | globex-data | Total |
|---|---|---|---|---|
| Accounts | 24 | 16 | 10 | **50** |
| Emitted source records (after defects) | 480 | 320 | 200 | **1,000** |

Per-source emitted records: product 450, Gmail 150, Zendesk 150, Salesforce 150, Stripe 100.

Injected defects (from the manifest, summed over organisations): duplicate 25, late_arrival 31, out_of_order_batch 6, missing_source 29, timezone_less 15, domain_freemail 4, name_variant 10, revised_record 2, sf_account_id_missing 9, stale_mapping 7.

Ingestion counts (distinct raw records and events, identity status, quality issues) are measured by running `seed` against a database and are not quoted here until that run is recorded. The integration suite asserts that every emitted record is either landed or removed as a duplicate, and that re-ingestion creates zero rows.

## Prediction target

- Target: `churn = 1` if the account does not renew at a scheduled renewal, or cancels effective within 30 days after it.
- Horizon: a renewal falls within `(cutoff, cutoff + 90 days]`.
- Observation window: features may use only events with `event_time <= cutoff` and `ingested_at <= cutoff`.
- Label observable at `renewal_date + 30 days`. Renewals whose label date is after the data end are censored: they receive no label and are excluded from training, validation and test. They are not counted as non-churn.
- Weekly cutoffs for training. Time-based split: train months 4 to 24, validation months 25 to 30, test months 31 to 36, after a 3-month warm-up. Never a random split by row.

Full details and the validity results are in `ml-design.md` sections 1, 2 and 12.

### Is the data fit for that split?

Not for statistical evaluation. The pipeline runs end to end, but the canonical dataset is too small to support a meaningful temporal evaluation. The test window has about 12 accounts and one churned account. The base rate also shifts across windows (18.1% train, 3.7% validation, 6.2% test). Details are in `ml-design.md` section 12. Phase 3 must not claim model performance from this dataset.

## Provenance

Every event row carries `raw_record_id` (the raw payload), `source_record_id`, `dedupe_key` (record ID plus a content hash prefix), `event_time`, `ingested_at`, the identity `match_method` and `match_confidence` in `attributes`. This is the chain a score will trace back through in Phase 3: score, feature snapshot, supporting `source_events`, raw payload. The feature snapshot and evidence tables are not built yet (migration 0004).

## Assumptions

- Source shapes are invented to look like exports from the five systems. They are not the real vendor schemas.
- A product timestamp with no offset is read in the account's configured timezone, falling back to UTC, and flagged as `timezone_assumed`.
- Duplicate deliveries count as first seen at their earliest `received_at`, so feature visibility does not depend on file order.
- A revised record (same ID, different content) is a new, later event. Earlier visibility is never rewritten.
- Fuzzy name matching uses a 0.88 similarity threshold and a 0.05 margin to the next candidate. These values were chosen on this data and are not tuned against anything real.

## Limitations

- Everything here is synthetic. Good results show the pipeline can recover planted structure, not that it would work on real accounts.
- The generator, not an observed process, defines churn. Defect rates are round numbers chosen for the demo.
- Defects are injected independently. Real failures are often correlated.
- Ingestion speed was not re-measured on the canonical dataset. The earlier 200,000-event run was slow and unexplained, and that run's numbers are no longer reproducible from this repository.
- `data_quality_issues.kind = 'missing_source'` is defined but never written. Coverage gaps will be measured as features in Phase 3.
- `data_quality_issues.run_id` is not yet a foreign key because `pipeline_runs` does not exist.
- The application role has no `UPDATE` or `DELETE` on these tables. Resolving an ambiguous event later needs a new, reviewed mechanism.
- Only one biennial renewal exists in the canonical dataset, too few to evaluate by term.
- The canonical dataset is too small for statistically meaningful evaluation. See `ml-design.md` section 12.

## Implemented versus designed

| Implemented | Designed, not built |
|---|---|
| Generator, manifest, validator, defect side file | Feature definitions and point-in-time builder |
| Five source adapters, identity resolution, ingestion, quality issues | Training, calibration, evaluation, model registry |
| Tenant-aware repositories with RLS and explicit org filters | Prediction snapshots and evidence tables |
| `WORKER_ORG_IDS` startup validation | Worker loop, stages, advisory lock |
| `generate-fixtures` and `seed` commands | Policies, actions, editable drafts, outbox |
| | Data quality and other API endpoints, dashboard |

## Files

Created: `migrations/versions/0003_accounts_and_ingestion.py`, `app/ingestion/` (generators, adapters, `identity.py`, `repository.py`, `pipeline.py`), `app/db/repository.py`, `app/worker/`, `tests/unit/test_generator.py`, `tests/unit/test_adapters_identity.py`, `tests/integration/test_ingestion.py`, `tests/integration/test_worker_startup.py`.

Changed: `app/config.py`, `app/cli.py`, `pyproject.toml`, `uv.lock`, `tests/security/test_grant_matrix.py`, `tests/integration/test_migrations.py`, and the design documents `adr.md`, `ml-design.md`, `synthetic-data.md`, `schema.md`, `worker-design.md`, `implementation-plan.md`.

Canonical-dataset change: `app/ingestion/canonical.py` (loader and organisation seeding), `fixtures/canonical/` (committed fixture), `tests/unit/test_generator.py` and `tests/integration/test_ingestion.py` now use the same fixture. Removed: the `ingest` command, the per-seed output directory, and the 5,000-account and 200,000-event configuration.
