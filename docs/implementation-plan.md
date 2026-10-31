# Phased Implementation Plan

Each phase ends with a review gate. Work does not start on the next phase until the current phase's acceptance criteria are either met or explicitly accepted as unmet by the reviewer. Every criterion is verified by a command or test, not by inspection alone.

Reporting format at the end of each phase: files changed, design decisions, commands to run, tests actually executed with results, known limitations, criteria met and unmet.

## Phase 0: Design (current)

Deliverables: `solution-brief.md`, `architecture.md`, `schema.md`, `synthetic-data.md`, `ml-design.md`, `worker-design.md`, `implementation-plan.md`, `adr.md`.

Acceptance criteria:

- [ ] Reviewer has read all design documents and answered or accepted the open questions in the solution brief.
- [ ] Reviewer has supplied the ML model requirements, or confirmed the demonstration defaults in `ml-design.md` section 11.
- [ ] No application code exists. `find app tests migrations -name '*.py'` returns nothing.

## Phase 1: Foundation, tenancy, and contract skeleton

Scope: repository scaffolding, Docker Compose with PostgreSQL, Alembic migrations 0001 to 0002 (roles, tenancy, auth, audit, idempotency), API key issuance, FastAPI application with tenant resolution middleware, route stubs and Pydantic response models for every endpoint in `architecture.md`, OpenAPI export.

Out of scope: ingestion, features, models, worker, dashboard content.

Acceptance criteria:

- [ ] `docker compose up -d db migrate` completes on a clean machine with no cloud credentials. Verified by a clean-volume run.
- [ ] `alembic upgrade head` and `alembic downgrade base` both succeed on a fresh database.
- [ ] Integration test: the `chi_app` role has `rolsuper = false`, `rolbypassrls = false`, and is not the owner of any table. Every tenant table has `relrowsecurity = true` and `relforcerowsecurity = true`.
- [ ] Integration test: with no `app.current_org_id` set, `SELECT` on every tenant table returns zero rows, and `INSERT` is rejected.
- [ ] Integration test: an org A key cannot read an org B row by id through the API, a direct SQL query under `chi_app` with org A context, or a query that omits the `org_id` predicate. Each returns 404 or zero rows.
- [ ] Integration test: `audit_events` rejects `UPDATE` and `DELETE` for every role, including `chi_owner`.
- [ ] Unit test: API key hashes use HMAC with the pepper. Startup fails when `API_KEY_PEPPER` is missing.
- [ ] `artifacts/openapi.json` is exported and committed. A CI-style check fails if the committed file differs from the generated one.
- [ ] Every endpoint in the endpoint table returns `401` without a key and `501` or a documented stub response with a key. No endpoint returns tenant data yet.
- [ ] `ruff`, `mypy --strict` on `app/`, and `pytest` pass. Test counts and output are reported verbatim.

## Phase 2: Synthetic data and ingestion (implemented; see `phase-2-data-and-ingestion.md`)

Scope: generator with seed and manifest (`ingestion/generators`), five source adapters, normalisation, identity resolution, deduplication, Alembic migration 0003, `cli generate-fixtures` and `cli seed` (one canonical dataset, 50 accounts and 1,000 emitted events, no separate large-data path). Also in scope after review: tenant-aware repositories, `WORKER_ORG_IDS` startup validation, and a pre-ML check that the data supports a valid temporal split and a non-trivial 90-day target.

Acceptance criteria:

- [x] Generator produces exactly 1,000 emitted source records and 50 accounts for the canonical seed, verified by the validator. Of the 1,000, 25 are intentional exact duplicates, so 975 unique records reach `raw_source_records` and `source_events` after deduplication. The 975 figure is the expected database count; "1,000" counts emitted records before deduplication. Changing the seed produces different output with the same totals. Evidence: `test_canonical_counts_are_exact`, `test_per_source_counts_match_the_budget`, `test_different_seed_changes_the_data`.
- [x] Two generation runs with the same seed have identical SHA-256 for every output file. Evidence: `test_fixture_matches_a_fresh_generation_byte_for_byte`; a manual regeneration left all 24 ndjson hashes unchanged.
- [x] Achieved defect rates are within tolerance of configured rates, written to `manifest.json`. Evidence: `defect_rates` section, checked by `validate()` and `test_achieved_defect_rates_are_within_tolerance_in_the_manifest`. The Stripe stale-mapping row uses `DefectRates.stripe_stale_mapping` (0.01) as its configured rate. At 0.01 the canonical seed injects no stale mappings, so the behaviour is covered by the in-memory test `test_stale_stripe_mapping_is_ambiguous_not_attributed_to_either_account` and the integration test checks that any stale mappings present are flagged ambiguous.
- [x] Every injected exact duplicate is removed by ingestion. Evidence: `test_every_emitted_record_is_landed_or_deduplicated` checks, per organisation, that emitted minus landed equals the injected duplicate count in `defects.ndjson`.
- [x] Ingesting the same batch twice creates zero new `raw_source_records` and zero new `source_events`. Evidence: `test_reingesting_creates_zero_new_rows`, `test_reseeding_the_canonical_load_is_idempotent`.
- [ ] Out-of-order and late-arrival fixtures produce the same `source_events` set as in-order delivery. Partly met, for `acme-saas` only: `test_arrival_order_does_not_change_the_normalised_events` covers file order, and `test_late_arrival_normalises_to_the_same_event_as_on_time_delivery` compares the original late-arrival copy with an on-time copy (same events, same identity status and canonical names; `ingested_at` excluded). Not yet run for `northwind-cloud` or `globex-data`.
- [ ] Identity mismatch: at least 95% of injected Zendesk name variants resolve to the correct account; all unresolved ones appear in `data_quality_issues`. Precision of resolved matches against ground truth is reported. Partly met: `test_zendesk_name_variants_resolve_at_least_95_percent` checks every organisation (expected 4, 6 and 0; correct 4, 6 and 0), asserts a non-zero total, and checks that every unresolved Zendesk event has a `data_quality_issues` row. Precision on the name-only path: `test_zendesk_name_resolution_precision_against_ground_truth` resolves all 10 canonical name variants by name alone (10 matched correctly, 0 wrong). Not covered: the organisation-ID path, and precision on Zendesk events that carry an organisation ID.
- [x] Ground truth is not imported by any module under `app/ingestion` (import-scan test). Evidence: `test_ingestion_code_never_reads_ground_truth_or_defects`.
- [x] Unit tests for each adapter parse at least one valid, one malformed, and one timezone-less payload. Evidence: valid and malformed payloads are parametrised over every source (`test_valid_payload_parses_with_utc_time`, `test_malformed_payloads_are_rejected_not_guessed`). Timezone-less handling: product is flagged (`test_product_timestamp_without_offset_is_flagged_not_guessed`); gmail, zendesk and salesforce are rejected as malformed (`test_timestamp_without_offset_is_rejected_for_iso_sources`). Stripe timestamps are epoch seconds, always UTC (`test_stripe_epoch_timestamp_is_always_utc`), so there is no timezone-less form to test.

## Phase 3: Features and ML

Part 1 (implemented): feature definitions, point-in-time builder, `feature_snapshots` (Alembic migration 0004) with tenant-scoped repository, `renewal_date` account contract field (migration 0003), `organizations` slug-collision handling, and the leakage and isolation tests in `tests/ml`.

Part 2 (implementation status, see `docs/model-card.md` and the Part 2 completion report): offline training dataset from ground-truth labels, temporal split, prior and learned candidates, evaluation with bootstrap intervals, joblib artifact with SHA-256 verification, runtime inference from the persisted artifact, model persistence (migration 0005 for `model_versions`, `model_evaluations`, `predictions`), `cli train`, `cli register`. `cli evaluate` is not implemented; evaluation runs inside `cli train`.

Acceptance criteria:

- [ ] All leakage tests in `ml-design.md` section 4 pass. Built: future-event, late-arrival, cutoff visibility, shuffle invariance, ground-truth isolation, tenant scope, imputer fit scope (logistic only), scoring-path ground-truth reference, scaler fit scope (`test_scaler_statistics_come_from_training_rows_only`), one-hot encoder fit scope (`test_one_hot_categories_are_learned_from_training_rows_only`), and the feature denylist (`test_feature_denylist_is_absent_from_model_inputs`). The denylist is a check on model column names only, not a scan of source columns. Scaler and encoder tests cover the logistic pipeline only.
- [x] Feature values for a fixed `(account, cutoff)` do not change when events after the cutoff are added to the database. Evidence: `test_future_event_does_not_change_the_snapshot`.
- [x] Training run with a fixed seed reproduces coefficients within `1e-9`. Evidence: `test_fitted_coefficients_are_reproducible_within_1e9` fits twice and compares `coef_` and `intercept_` with `atol=1e-9`. The determinism test (`test_fitted_predictions_are_deterministic`) checks predictions only.
- [x] Model selection follows the pre-registered rule, with the selection hash computed before test metrics. Met in the code, with a process disclosure: the test metrics were printed in one earlier run before the validation gate was added (`ml-design.md` section 5.2).
- [ ] Temporal test metrics are reported with bootstrap 95% confidence intervals on PR-AUC, ROC-AUC, Brier score, and top-10% precision. Partly met: intervals are computed for PR-AUC, ROC-AUC, and Brier; top-10% precision has no interval.
- [x] Rules baseline and constant-prior floor are evaluated on the same test examples and reported. Partly met: the constant prior is evaluated. The rules baseline is not implemented (its weights are not defined in the repository).
- [ ] The logistic model beats the constant prior on test PR-AUC by a margin whose confidence interval excludes zero. Not met. The test difference interval for logistic is 0.144 to 0.431, which excludes zero, but test has 1 churned term, so the comparison is unreliable and the criterion is reported as unmet. Validation shows no reliable difference (-0.056 to 0.069).
- [x] Slice metrics are reported for segment, coverage profile, plan tier, and region, with sample sizes; slices with fewer than 30 churn events are flagged. Met in the code. Every slice on the current test set is flagged unreliable.
- [ ] Explanation contributions sum to the logit within `1e-6` for logistic regression. Not built; explanation is Part 3.
- [x] Artifact loading verifies `artifact_sha256` and refuses a mismatch. Tested with a tampered file (`test_tampered_artifact_is_refused_before_loading`).
- [ ] Model card draft (`docs/model-card.md`) is generated from the stored metrics file, not typed by hand. Not met: the model card is written by hand. It cites the metrics file and the artifact, but it is not generated, and no regenerate-and-diff check exists.

## Phase 4: API implementation and contract testing

Scope: handlers for every endpoint, cursor pagination, RFC 9457 errors, idempotency-key handling for approval, Postman collection generated from the OpenAPI spec, collection run with the Postman CLI against the Compose stack.

Acceptance criteria:

- [ ] Every endpoint's response validates against its committed OpenAPI schema. Verified by the contract test suite.
- [ ] Ranked accounts endpoint returns the latest snapshot per account, sorted by score, with `model_version`, `scored_at`, and `feature_cutoff_at` on every item. Verified against the database directly.
- [ ] Prediction detail endpoint returns the stored contributions and feature values. Values match `feature_snapshots` byte-for-byte.
- [ ] Approval endpoint: first call returns `201`; repeated call with the same `Idempotency-Key` returns the same body and `200`; same key with a different body returns `422`; approving an action from another org returns `404`.
- [ ] Cursor pagination: walking all pages returns each item exactly once, including across a concurrent insert.
- [ ] Postman collection is generated from `artifacts/openapi.json`, and the Postman CLI run passes against the Compose stack. The run output is saved to `artifacts/postman-run.txt`.
- [ ] The cross-tenant tests from Phase 1 still pass through the complete HTTP path.
- [ ] No Postman workspace push has occurred unless the user has given explicit consent in this phase.
- [ ] Load smoke: `GET /api/v1/accounts?limit=200` at the default dataset completes with p95 under a value the reviewer sets. The value is recorded, not assumed.

## Phase 5: Worker, policies, and safe outreach

Scope: sequential worker over an explicit organisation list with an advisory lock (ADR-022; no claim policies or scheduler role), run executor, stage functions, `policies.py`, `actions.py`, outbox dispatcher, mock email adapter, template-rendered drafts that a CSM can edit before approval (draft columns, an edit operation and its audit event, which needs a contract change first), retry and dead-letter logic, Alembic migrations 0005 and 0006, `cli run-worker-once`.

Acceptance criteria:

- [ ] Running `cli run-worker-once` twice for the same cutoff creates one run and identical outputs. Verified by row counts and checksums.
- [ ] Crash injection after `score` and before `enqueue_actions`, then rerun: no duplicate predictions, actions, or outbox rows.
- [ ] A second worker started while one is running exits on the advisory lock. No organisation has two `running` runs at once. (Replaces the concurrent-claim criterion; see ADR-022.)
- [ ] Policy decision table: every rule in `worker-design.md` section 5 has a unit test, and the order of precedence is tested.
- [ ] No outbox row exists for an action without an `action_approvals` row. Verified by a query across the whole database after a full run.
- [ ] Dry-run default: with no `OUTREACH_MODE` set, the adapter's call counter is zero after a full run that includes approvals.
- [ ] Adapter returns 503 five times: message is `dead_letter` after exactly `OUTBOX_MAX_ATTEMPTS`, with one audit row per attempt.
- [ ] Adapter returns 400: message is `failed` with no retry.
- [ ] A suppression rule added after approval and before dispatch blocks the send.
- [ ] Log scan: a full run's logs contain no email addresses, API keys, or payload fields.
- [ ] Startup refuses an unknown `OUTREACH_MODE` or `EMAIL_ADAPTER`, and refuses a missing `API_KEY_PEPPER`.

## Phase 6: Dashboard, end-to-end, and documentation

Scope: Streamlit pages (ranked list, account detail with evidence, outbox view, data quality, model metadata), end-to-end test, demo script, finalised documents, README with limitations and "what breaks first at 10x".

Status (2026-10-31): partly done. Built: Portfolio, Accounts, Account detail, Actions, and Data quality screens, with search, source-grouped evidence, and approve and reject actions. Not built: the model metadata page, the end-to-end test, the trace test, `docs/walkthrough.md`, and `docs/security.md`. No visual browser check has been run. Checks run on 2026-10-31: `pytest -m "not integration"` 177 passed; dashboard tests 40 passed; `ruff check` and `ruff format --check` clean; `mypy app/dashboard` clean. Integration tests were not run in this update.

Acceptance criteria:

- [ ] Dashboard shows no hardcoded metric or explanation. Verified by a test that changes a stored metric and checks the rendered value changes.
- [ ] Browser check: the ranked list, account detail, and approval flow work in a browser on the Compose stack. Screenshots are saved to `artifacts/screenshots/`. If a browser check is not run, the report says so.
- [ ] End-to-end test: seed, migrate, train, evaluate, register, run worker, call API, approve one action, dispatch in dry-run, verify the trace from dashboard value to model version, feature values, and source event IDs.
- [ ] Trace test: for a random sample of 50 displayed predictions, the source event IDs in `prediction_evidence` exist, belong to the same org, and have `event_time <= cutoff_at`.
- [ ] `docs/walkthrough.md` and the demo script run from a clean clone with the commands in the README. Output is captured.
- [ ] `docs/security.md` lists each control, the test that verifies it, and each known gap.
- [ ] The README states what is not production-grade, with the evidence for each claim or the absence of it.

## Cross-phase rules

- Tests are never skipped to get a green run. A failing test is reported as failing, with its output.
- Any criterion that is not met is listed as unmet, with the reason, in the phase report.
- Scope changes after Phase 0 are recorded as new ADRs in `adr.md`, with the trigger.
- Postman cloud push, real email sending, and external network calls require explicit user consent in the relevant phase.
