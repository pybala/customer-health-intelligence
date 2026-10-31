# Architecture Decision Records

Format per record: context, decision, alternatives rejected, consequences. Status is `accepted` unless marked otherwise.

## ADR-001: Scheduled batch processing instead of streaming

**Context.** CSMs review risk daily or weekly. The requirement is an unattended scheduled worker.

**Decision.** Daily batch runs per organisation with a configurable interval.

**Alternatives rejected.** Kafka or CDC streaming: adds broker operations and ordering complexity with no benefit at this cadence. Event-driven per-event scoring: makes cutoff semantics harder to reproduce.

**Consequences.** Late data is handled by re-scoring at cutoffs and by visibility rules on `ingested_at`. Freshness is hours, not seconds. Stated in the brief.

## ADR-002: Deterministic worker, no LLM agent

**Context.** The brief asks for an unattended worker and excludes requirements for an external LLM. Auditable, reproducible outreach is required.

**Decision.** The worker is a fixed pipeline of stages. Decisions are pure functions in `agent/policies.py`. No LLM is called in any path.

**Alternatives rejected.** Autonomous LLM agent choosing actions: non-deterministic, hard to test, and it is difficult to show why an email was proposed. LLM-written explanations: cannot be verified against stored contributions.

**Consequences.** Every output is reproducible from stored inputs. Explanations are template-based and limited in wording.

## ADR-003: PostgreSQL as the only stateful store; tables as the queue

**Context.** The worker needs a durable queue, leases, and idempotency. The brief asks to avoid unnecessary infrastructure.

**Decision.** Use PostgreSQL for everything. Runs are claimed with `FOR UPDATE SKIP LOCKED`. Outbox is a table. No Redis, Celery, RabbitMQ, or Airflow.

**Alternatives rejected.** Celery with Redis: another service to run, configure, and secure, for a queue of at most a few thousand rows a day. Airflow: heavier than the pipeline itself and not needed for one DAG.

**Consequences.** Throughput is bounded by PostgreSQL write rates. At 10x this is still comfortable for this data volume. The path to a broker is the outbox dispatcher, which can be pointed at one without changing the write side.

## ADR-004: Tenant isolation through identity, explicit predicates, and RLS

> **Amended by ADR-022 (2026-10-29).** Identity now comes from an authenticated key or dev token, or from the worker's explicit organisation list (not a claimed run). RLS is unchanged.

**Context.** Multiple customer organisations share one database. The brief requires defence in depth and no trust in request-supplied org IDs.

**Decision.** Three layers: `org_id` derived from the authenticated key or claimed run; explicit `org_id` in every repository query; RLS with `FORCE` on every tenant table, reading `app.current_org_id` set with `SET LOCAL`. The application connects as a non-owner, non-superuser role with `NOBYPASSRLS`.

**Alternatives rejected.** Schema-per-tenant: heavier migrations and connection management for three tenants and no real gain for this threat model. Database-per-tenant: operationally expensive and not needed for the demonstration. Application-only filtering: one missed `WHERE` clause leaks data. Honouring `org_id` query parameters: rejected outright.

**Consequences.** Migrations need a separate owner role. The API key lookup needs its own policy (ADR-010). Tests must cover the non-owner role, which is the default test path, not an optional extra.

## ADR-005: Append-only prediction snapshots

**Context.** The brief requires traceability to model version, features, and timestamp, and asks to avoid silently overwriting history.

**Decision.** `prediction_snapshots` is insert-only for the application role. The latest score per account is a query. Re-scoring the same `(account, model_version, cutoff)` is idempotent through a unique key.

**Alternatives rejected.** Updating a `current_risk` column on `accounts`: loses history and makes audit impossible. Event-sourcing the full model state: more machinery than needed.

**Consequences.** Storage grows with runs times accounts. The demo dataset is 50 accounts and 1,000 records, so growth is negligible; the production sizing estimate is out of scope for this build and should be redone before any real-scale claim.

## ADR-006: Outreach through a human approval gate and an outbox; dry-run by default

**Context.** Customer emails are an external, irreversible effect. The brief requires safe, auditable outreach with dry-run, suppression, retries, and deduplication.

**Decision.** The worker proposes actions only. A CSM approval, in one transaction with suppression re-check and outbox insert, is the only path to a sendable message. The dispatcher sends with bounded retries. `OUTREACH_MODE` defaults to `dry_run`. The only adapter is the mock.

**Alternatives rejected.** Automatic sending after scoring: removes the human decision and makes errors reach customers directly. Sending inline from the scoring stage: a crash between send and record duplicates or loses messages.

**Consequences.** Adds an approval step to the demo flow. That is intentional, and it is the behaviour a real deployment needs.

## ADR-007: Synthetic generator owns ground truth; ground truth is never read by product code

**Context.** The brief requires realistic synthetic data and a model evaluated against it. A model graded on labels it can see, or on a generator it shares code with, shows nothing.

**Decision.** The generator writes `ground_truth.ndjson` and `defects.ndjson` as separate files. Product code (ingestion, features, training, API) has no import path to them. Only evaluation tests read them.

**Alternatives rejected.** Labels computed from the same feature code: circular. Hand-written CSV fixtures: too small to test the stated volumes or defect rates.

**Consequences.** The model's score is measured against planted structure, which is reported as a ceiling and a limitation, not as evidence about real churn.

## ADR-008: Model artefacts and evaluation records are platform-level, not tenant-scoped

**Context.** Three demo tenants share one feature definition and one model family. Per-tenant models would be separate artefacts with separate evaluations and no extra isolation value for this build.

**Decision.** `model_versions` and `model_evaluations` have no `org_id` and no RLS. They contain no customer data. Predictions and features are tenant-scoped.

**Alternatives rejected.** Per-tenant models: more training and more artefacts for three tenants, and tenant-specific training would reduce the data each model sees. Tenant-scoped model rows: unnecessary; model metadata does not identify customers.

**Consequences.** A model trained on one tenant's data is used for all. This must be stated in the security and model documents, and it is a real cross-tenant data flow. Per-tenant training is recorded as the production option to decide once the business confirms data-sharing rules.

## ADR-009: Logistic regression as the primary model; gradient boosting as candidate; selection rule fixed in advance

**Context.** The model must be explainable to CSMs and evaluated honestly on a time-held-out set.

**Decision.** Candidates: rules baseline, logistic regression, histogram gradient boosting. The selection rule is written in `ml-design.md` before training. Selection uses validation only; the test window is read once.

**Alternatives rejected.** Deep learning or AutoML: not explainable enough for the use case and not justified by tabular data of this size. Choosing the best test score: invalidates the evaluation.

**Consequences.** Logistic regression may win on simplicity, which is an acceptable result. The evaluation must report whether either model beats the baseline with confidence intervals, not only the winner.

## ADR-010: API key authentication with prefix lookup, HMAC-hashed secrets, and a narrow RLS lookup policy

> **Amended by ADR-022 (2026-10-29).** This path is implemented and tested, but the local demo defaults to `AUTH_MODE=dev_tokens`. Both modes share the `Principal` type. `api_keys` is kept.

**Context.** Requests must resolve a tenant before the tenant context exists. The key table is itself under RLS.

**Decision.** Keys have a public prefix and a secret. Only `HMAC-SHA256(pepper, secret)` is stored. The middleware sets `app.api_key_prefix` and reads one row through a policy that matches one prefix. Key issuance runs only through `cli` under the owner role, not through the API.

**Alternatives rejected.** `SECURITY DEFINER` function owned by the table owner: under `FORCE ROW LEVEL SECURITY` the owner is still subject to the policy, so the function would return nothing. Granting `BYPASSRLS` to the application role: makes every RLS control moot, and it is explicitly forbidden by the brief. Storing unhashed keys: rejected. Argon2 for API keys: adds latency per request for high-entropy random secrets, where HMAC is sufficient.

**Consequences.** The lookup policy allows reading one row by guessing a prefix. Prefixes are 12 random characters, so guessing is not practical, and the secret is still required. Documented in `security.md`.

## ADR-011: Metadata-only payloads for Gmail and Zendesk

**Context.** Email bodies and support ticket text contain personal data. The brief does not require sentiment or text analysis.

**Decision.** Synthetic payloads contain headers, timestamps, counts, participant domains, and status fields. No bodies. The schema has no column for message text.

**Alternatives rejected.** Storing bodies with encryption at rest: adds key management and retention decisions the business has not made. Storing bodies and redacting later: redaction is unreliable and bodies would be in backups.

**Consequences.** Text-derived signals (sentiment, topics) are out of scope and listed as a limitation. Solution brief question 6 asks the business to confirm.

## ADR-012: Streamlit calls the API, not the database

**Context.** The dashboard displays tenant data. If it reads the database directly, it needs its own tenant logic and a second authorisation path.

**Decision.** The dashboard is an API client. It holds an API key for the signed-in user, and every displayed value comes from an API response. It has no database connection string.

**Alternatives rejected.** Direct repository calls from Streamlit: duplicates authorisation and widens the attack surface of the UI container. Separate BFF service: an extra container for no current benefit.

**Consequences.** The dashboard depends on API latency. Acceptable at this scale. The `architecture.md` dashboard row reflects this decision.

## ADR-013: API conventions: no tenant in the URL, cursor pagination, RFC 9457 errors, 404 for foreign resources

**Context.** The API is consumed by a dashboard and by tests that check tenant isolation. Its conventions decide what leaks.

**Decision.** `/api/v1` base path. Tenant from the key only. Cursor pagination with a 200-item cap. Errors as `application/problem+json`. A resource belonging to another tenant returns `404`, identical to a missing resource, so existence is not revealed. Mutations require `Idempotency-Key`.

**Alternatives rejected.** Tenant in the path (`/orgs/{id}/accounts`): invites exactly the untrusted-parameter pattern the brief forbids. Offset pagination: unstable under concurrent inserts. `403` for foreign resources: reveals that the ID exists.

**Consequences.** Clients cannot distinguish "not yours" from "does not exist". That is intended, and it is tested.

## ADR-014: Idempotency keys stored with request hash and expiry

**Context.** Approval is a state change that a retried HTTP request could repeat.

**Decision.** `Idempotency-Key` is stored with `(org_id, key)`, a hash of the request body, the first response, and a 24-hour expiry. Same key and same body returns the stored response. Same key and different body returns `422`.

**Alternatives rejected.** Relying on the business unique key alone: returns a different error on retry rather than the original result. Idempotency without body hashing: a reused key could silently approve a different action.

**Consequences.** One extra table and a purge job.

## ADR-015: Typed, fail-closed configuration from environment variables

**Context.** Several settings are safety-relevant (outreach mode, adapter, pepper). A missing value must not fall back to something unsafe.

**Decision.** Pydantic settings classes. Required values have no default. Unknown enum values stop startup. `OUTREACH_MODE` defaults to `dry_run`.

**Alternatives rejected.** A single untyped dict of environment variables: silent typos become unsafe defaults. A YAML config file: adds a second source of truth.

**Consequences.** `.env.example` must list every variable. A test asserts that the example file and the settings class agree.

## ADR-016: Schema through Alembic under a separate owner role; application never runs DDL

**Context.** The brief requires migrations, and the application role must not own tables or be able to alter them.

**Decision.** `migrate` runs Alembic with `MIGRATION_DATABASE_URL` (owner role). The application connects with `DATABASE_URL` (non-owner). Migrations are the only DDL path.

**Alternatives rejected.** `create_all()` at startup: fails the brief directly, and it would need DDL privileges on the app role. Using the superuser for migrations: unnecessary privilege.

**Consequences.** Two connection strings and two roles in Compose. Setup is slightly more involved and is documented.

## ADR-017: Explanations from stored contributions only

**Context.** A CSM must be able to check each explanation against source events. Explanations must not be hardcoded or generated.

**Decision.** `prediction_snapshots.contributions` stores feature values, reference values, and contributions at scoring time. The UI formats these with templates from feature metadata.

**Alternatives rejected.** LLM-written prose: see ADR-002. Recomputing explanations on read: could differ from what was scored, which breaks traceability.

**Consequences.** Explanations are as good as the feature names. Feature metadata must be written carefully.

## ADR-018: FastAPI is the contract source; OpenAPI is exported and committed; Postman collection is generated and run locally

**Context.** The API is the boundary for the dashboard and for tests of tenant isolation. Its contract needs to be reviewable before handlers are filled in, and it should be testable outside Python.

**Decision.** FastAPI route and Pydantic models produce `artifacts/openapi.json`. The file is committed, and a check fails on drift. Phase 1 creates stubs and the spec; Phase 4 implements handlers. A Postman collection is generated from the spec and run with the Postman CLI against the Compose stack. Cloud workspace push requires explicit user consent and is not part of this build by default.

**Alternatives rejected.** Writing the spec by hand first and generating code from it: duplicates the source of truth and drifts. Postman cloud as the source of truth: makes the contract depend on an external account, and the brief requires running without cloud credentials. Skipping the spec: the contract would only exist in code and tests.

**Consequences.** Spec and implementation can diverge if the drift check is removed. The check is part of the Phase 1 acceptance criteria.

## ADR-019: Run claim by lease and uniqueness, not by an external scheduler

> **Partly superseded by ADR-022 (2026-10-29).** The `(org_id, cutoff_at)` uniqueness stays. `SKIP LOCKED` claiming, leases, and the one-running-run index are deferred until a requirement needs concurrent workers. A single sequential worker holds a Postgres advisory lock instead.

**Context.** The worker must run once per organisation per cutoff, even with several containers.

**Decision.** `pipeline_runs` unique on `(org_id, cutoff_at)`, claimed with `FOR UPDATE SKIP LOCKED`, with a partial unique index allowing one `running` run per organisation. Stage leases expire and are re-run idempotently.

**Alternatives rejected.** Cron on the host: no concurrency control and no record of runs. Kubernetes CronJob: needs a cluster, which the brief excludes.

**Consequences.** Lease expiry is a tuning value. Too short re-runs healthy stages; too long delays recovery. Default is set in `worker-design.md`.

## ADR-021: Cross-tenant worker steps use claim-mode policies, not a bypass role

> **Superseded by ADR-022 (2026-10-29).** Not implemented, and not planned. The worker is one sequential process using the application role, so no claim-mode policies, no `chi_scheduler` role, and no cross-tenant claiming are needed. Kept for history.

**Context.** The scheduler must list tenants, and executors and the dispatcher must claim work before any tenant context exists. Under `FORCE ROW LEVEL SECURITY` with fail-closed policies, those queries return nothing.

**Decision.** Three narrow mechanisms: a `chi_scheduler` role that can read only organisation IDs and status; claim policies on `pipeline_runs` and `outbox_messages` that are active only when a claim-mode setting is on and only permit `pending` to `running` or `sending` transitions; tenant context set immediately after each claim.

**Alternatives rejected.** `BYPASSRLS` on the worker role: removes every tenant control from the process that executes customer logic. `SECURITY DEFINER` claim functions: the function owner is still subject to `FORCE` RLS, so they do not solve the problem without a bypass. Separate per-tenant worker processes: removes cross-tenant claiming but adds an orchestrator for three tenants.

**Consequences.** Claim mode is a session setting, so it is a guard against accidental cross-tenant reads, not a boundary against a compromised worker. Documented as a limitation in `security.md` (Phase 6). Claim policies are covered by an integration test that asserts non-claim sessions cannot see pending rows of other tenants.

## ADR-020: Platform-level idempotency for ingestion by content hash and natural key

**Context.** Sources re-deliver records and sometimes change them. Duplicates are injected deliberately.

**Decision.** `raw_source_records` is unique on `(org_id, source, source_record_id, payload_sha256)`. `source_events` is unique on `(org_id, source, dedupe_key)`. Changed content with the same source ID is a new revision, not an update.

**Alternatives rejected.** Overwriting records on the source ID: loses the history needed to explain past features. Dedup by exact full-payload equality only: misses semantic duplicates where only timestamps differ. Accepted as a limitation, see `synthetic-data.md`.

**Consequences.** Storage grows with revisions. Revisions are rare in the generator (0.5%).

## ADR-022: Simplify authentication and the worker for the single-operator assessment

**Status.** Accepted 2026-10-29. Supersedes ADR-021. Partly supersedes ADR-019. Amends ADR-004 and ADR-010.

**Context.** The assessment runs on one machine with one operator, three synthetic organisations, and one scheduled worker. The Phase 0 design assumed mutually untrusting hosted tenants: claim-mode policies, a scheduler role, and concurrent claiming. Review found that RLS on `organizations` blocks listing organisations without a tenant context, which was the only reason for the scheduler role and claim policies. Details are in [architecture-simplification-review.md](architecture-simplification-review.md).

**Decision.**

1. **Auth.** `AUTH_MODE` is `api_keys` (default if unset) or `dev_tokens`. The local demo sets `dev_tokens`. A token file maps each token to an organisation ID and a role. The organisation always comes from the token, and the API never reads one from a request. Startup logs a warning that the mode is not production-ready.
2. **Roles.** `readonly` can only use GET, HEAD, and OPTIONS (403 otherwise). `csm` and `admin` can use every method. Admin-only endpoints do not exist yet, so `admin` and `csm` currently behave the same.
3. **Audit identity.** `audit_events.actor_key_id` is unchanged. For a dev token it holds a UUIDv5 derived from the token label. The secret is not an input, and rotating a token keeps the same identity. This identifies a label, not a cryptographically authenticated person.
4. **Worker.** One sequential process with an explicit list of organisation IDs. Startup validates each ID against `organizations` under that organisation's own tenant context. RLS stays on every tenant table, including `organizations`. Idempotency comes from `UNIQUE (org_id, cutoff_at)`. A Postgres advisory lock stops a second worker from running at once (planned, with the worker).
5. **Roles in the database.** `chi_scheduler` is removed from configuration, initialisation, and tests. Two roles remain: `chi_owner` and `chi_app`.
6. **Kept as is.** The `api_keys` table and key code, the audit immutability trigger, and every RLS policy including `api_key_lookup`.

**Alternatives rejected.** Disabling RLS on `organizations`: weakens the boundary to make listing easier, and was explicitly declined. Keeping claim-mode policies: complexity with no concurrent worker to justify it. Dropping `api_keys` now: no simplification of the new work, and about 44 matching test lines would change.

**Consequences.** Dev tokens are plaintext in a gitignored local file, cannot be revoked individually, and give no isolation between token holders. They are not an authentication system for independent production tenants. Organisation scoping is enforced in two layers (token to organisation, then RLS), and the worker sets one organisation at a time but nothing stops it setting another. The README must say this plainly.

**Deferred.** Dropping `api_keys` and `api_key_lookup` (needs approval and a migration). Dropping the `chi_scheduler` role from an existing local database (manual, see `phase-1-setup-and-changes.md`). Concurrent workers, leases, and `SKIP LOCKED`.

## Decisions resolved by the reviewer (2026-10-29)

| Topic | Decision | Consequence |
|---|---|---|
| Per-tenant models | One shared model trained on synthetic data from all three organisations. | Org boundaries are preserved in ingestion, storage, scoring and API access. Evaluation is reported overall and per organisation. The shared model is not assumed to perform equally for every organisation. |
| Prediction horizon | 90 days: the probability that an account fails to renew within the next 90 days. | See `docs/phase-2-data-and-ingestion.md` for the target, cutoff and censoring rules. |
| Approval policy | Automatic risk assessment and queueing. A human approves before any email is sent. | The worker runs unattended up to the queue. Sending stays disabled by default; the demo uses a local outbox or sandbox. |
| Email content | Template-based drafts that a CSM can review and edit before approval. | Deterministic templates populated with account details and traceable evidence. The outbox and actions design (Phase 5) must store an editable draft and record who edited it. No LLM is used for scoring or eligibility. |
