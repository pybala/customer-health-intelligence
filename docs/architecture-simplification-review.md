# Architecture Simplification Review

Status: **approved 2026-10-29 (D1 to D5), with conditions.** Option A for worker listing; RLS stays on every table including `organizations`; `api_keys` and all migrations are kept; no database role is dropped automatically. Outcome and implementation are recorded in ADR-022 and `phase-1-setup-and-changes.md`. The text below is the original proposal, left as written.

Scope: reassess whether the tenancy, authentication, role, and worker design is justified for the Fello.AI assessment, and propose the smallest change that keeps organisation boundaries real and tested.

## 1. Context

The assessment needs a portable Python application: 50 accounts and 1,000 records across five sources, three synthetic organisations, a fitted model, explainable scores traceable to source events, an unattended worker, a CSM dashboard, and outreach that is dry-run by default. It runs on one machine with one operator.

The Phase 0 design treated the organisations as independent SaaS tenants. That produced per-organisation API keys, RLS with fail-closed policies, three database roles, and claim-mode policies for concurrent multi-tenant workers. Most of that exists for a threat model the assessment does not have: mutually untrusting customers sharing a hosted service.

## 2. What exists today

Only Phase 1 is built: two migrations and about 580 lines of application code. Everything from migration 0003 onward, the worker, ML, generator, and dashboard are design documents only.

| Component | State | Notes |
|---|---|---|
| Tables `organizations`, `api_keys`, `audit_events`, `idempotency_keys` | Implemented (migration 0002) | All empty in the dev database |
| RLS with `FORCE` and `tenant_isolation` on `api_keys`, `audit_events`, `idempotency_keys` | Implemented, tested | Fail closed with no tenant context |
| RLS on `organizations`: `own_organization` (SELECT), `operator_create_organization` (INSERT, owner only) | Implemented, tested | See finding F1 |
| `api_key_lookup` policy on `api_keys` | Implemented, tested | Exists only so key authentication can run before tenant context is set |
| API keys: HMAC with pepper, `chi_<prefix>.<secret>`, `issue-key` CLI, revocation | Implemented, tested | |
| Audit immutability trigger | Implemented, tested | Keep |
| Idempotency key table | Implemented | Keep |
| Roles `chi_owner`, `chi_app` | Implemented | |
| Role `chi_scheduler` | Created, no grants, no use | Exists only in the init script, compose file, `.env`, and tests |
| Claim-mode policies (ADR-021), `SKIP LOCKED` claiming, leases, one-running-run index | Designed only | Nothing to remove from the database |
| Tables for accounts, events, features, predictions, actions, outbox (0003 to 0006) | Designed only | |
| Route stubs for 12 endpoints plus health | Implemented | No response models yet |
| Worker, ML, generator, dashboard | Designed only | Placeholder or empty package directories under `app/` (`agent`, `features`, `ml`, `notifications`, `ingestion`, `dashboard`); no code. There is no `app/worker` package yet. |

## 3. What the assessment needs

| Need | Why |
|---|---|
| `org_id` on every tenant table, in seed data, and in every query | Multiple organisations is a stated requirement |
| API and dashboard consistently filtered by organisation | Same |
| 50 accounts and 1,000 records with deterministic seeds (one canonical dataset) | Stated scale |
| Idempotent ingestion and idempotent runs | Integrity and unattended operation |
| Append-only predictions and audit trail, evidence per score | Traceability |
| Approval gate, outbox, dry-run default | Safe outreach |
| One shared model, evaluated per organisation | Agreed approach |
| One scheduled worker | Unattended execution |
| A tested organisation boundary | Agreed: preserve and test boundaries |

## 4. Component review

### 4.1 Authentication

**Today.** Per-organisation API keys with a pepper, prefix lookup, hashing, revocation, and an operator CLI. This is production-shaped, and the assessment does not need it.

**Proposal.** A development-token adapter, clearly labelled:

- `AUTH_MODE=dev_tokens` is explicit in configuration, and the application logs a startup warning that this mode is not for production.
- A configuration file maps each token to an organisation slug and a role. The organisation ID is derived from the slug with a fixed UUIDv5 namespace, so the application resolves tokens to an organisation without reading the database.
- The client never supplies an organisation ID. The API still ignores any `org_id` in a path, query, or body.
- Roles keep their meaning, with rules written down and tested: `readonly` can only read, `csm` can read and approve actions, `admin` can additionally read operational endpoints.
- Tokens are never logged, and the token file is gitignored with a committed `.example` file.

**Why the deterministic UUID.** The current `organizations` RLS policy blocks reading the table before a tenant is set. Deriving the ID from the slug avoids needing a special lookup policy for the new adapter.

### 4.2 `api_keys` table and key code

Dependencies found by search:

| Area | Files | What depends on `api_keys` |
|---|---|---|
| Application | `app/security/principal.py`, `app/security/api_keys.py`, `app/cli.py`, `app/db/models.py` | Lookup, hashing, `issue-key`, ORM model |
| Migration | `0002_tenancy_and_auth.py` | Table, index, RLS, `api_key_lookup`, grants; downgrade drops it |
| Foreign keys | none | `audit_events.actor_key_id` is a plain `uuid` with no foreign key, so nothing in the schema blocks a drop |
| Tests | `test_cli.py` (9 matching lines), `test_tenant_isolation.py` (9), `test_grant_matrix.py` (8), `test_api_auth.py` (7), `test_config.py` (6, plus 9 lines mentioning the pepper), `test_api_keys.py` (4), `test_migrations.py` (1) | Fixtures, key issuance, revocation, wrong-pepper, grant checks |

The test counts are lines matching `api_keys`, `issue_key`, or `api_key` (44 in total across seven files), measured by grep. They indicate effort, not the number of tests that would change.

Dropping the table removes the whole `app/security` package, needs a migration, and touches those seven test files. Keeping it costs one unused table and one unused policy.

**Recommendation: defer the drop.** Adding the dev-token adapter does not require removing anything, so removal does not materially simplify that work. Keep the table, migration 0002, and the existing key code in place. Make `dev_tokens` the default mode, and mark the key path as legacy in the docs. Revisit removal after the end-to-end workflow works, as a separate approved migration.

One open question this raises: `audit_events.actor_key_id` is typed as a UUID. A dev token has no key row. See decision D4.

### 4.3 Row-level security

**What it protects against.** A repository method or new endpoint that forgets the `org_id` predicate would otherwise return another organisation's rows. RLS makes the database refuse, so one missed `WHERE` clause cannot leak across organisations. This is the concrete threat, and it is a realistic one for a codebase that will grow to a dozen tables.

**What it does not protect against.** A compromised application process can set any organisation. The dev tokens give no isolation between whoever holds them. A superuser connection ignores it.

**What exists is already minimal.** One identical policy per tenant table, and one `set_config` call per transaction in `tenant_session`. It is built and tested, so keeping it costs little, and removing it would cost a migration plus rewrites of the isolation tests.

**Recommendation: keep `tenant_isolation` on the three tenant tables, and add no new RLS.** Each later tenant table (0003 and onward) gets the same single policy, generated by one helper rather than hand-written. Apply no other RLS pattern.

**What is not minimal.** Two pieces exist only to support key authentication and per-tenant operator creation:

- `api_key_lookup` becomes unused once dev tokens are the default.
- The two `organizations` policies are the source of finding F1.

### 4.4 Finding F1: listing organisations is the real friction point

The worker must process all three organisations in turn. `organizations` has `FORCE ROW LEVEL SECURITY` and a `SELECT` policy that only allows `id = current_org_id`. Under `chi_app` with no tenant context, `SELECT id FROM organizations` returns zero rows. The old design solved this with the `chi_scheduler` role and claim policies. A single sequential worker should not need either.

Options, from smallest change to largest:

| Option | Change | Schema change? | Trade-off |
|---|---|---|---|
| A | The worker iterates the organisations named in configuration, using the same deterministic IDs as the token adapter | None | Organisations exist in two places, configuration and the table. A mismatch is caught by a startup check. |
| B | Disable RLS on `organizations` only and drop its two policies; `chi_app` keeps `SELECT` only | Yes, small: one `ALTER TABLE` and two `DROP POLICY` | Organisation slug and display name become readable to any `chi_app` session. They are not sensitive for this assessment. |
| C | Keep the claim-mode design | Yes, large | Rejected: it adds the complexity this review set out to remove |

**Recommendation: A first.** It needs no database change and unblocks the worker. If it proves awkward, B is a small, reversible follow-up. Neither option touches data.

### 4.5 Database roles

| Role | Keep? | Reason |
|---|---|---|
| `chi_owner` | Keep | Runs migrations and operator commands. Separation of migration and runtime is retained. |
| `chi_app` | Keep | Runtime role. Not a superuser, `NOBYPASSRLS`, owns nothing. |
| `chi_scheduler` | Remove | No grants and no use. With a single sequential worker it has no purpose. |

Removing `chi_scheduler` touches the init script, compose file, `.env.example`, `.env`, `tests/conftest.py`, the grant-matrix test, and one audit-trail test. The init script only runs on a fresh volume, so on the existing dev volume the role would stay until the volume is rebuilt or the role is dropped by hand. Dropping a role is a destructive database change, so it needs your approval (decision D5).

### 4.6 Worker

One scheduled process, one organisation at a time, in a fixed order.

| Mechanism | Keep? | Reason |
|---|---|---|
| Unique `(org_id, cutoff_at)` on `pipeline_runs` | Keep | Idempotency: the same cutoff cannot create a second run |
| Run and stage status records | Keep | Traceability and crash recovery |
| Idempotent stage keys | Keep | A re-run after a crash must not duplicate predictions, actions, or outbox rows |
| Postgres advisory lock held for the process lifetime | Add | If a second worker starts by mistake, it exits instead of racing. Cheaper than claim policies. |
| `FOR UPDATE SKIP LOCKED` claiming, leases, one-running-run partial index | Defer | Only needed for concurrent workers |
| Claim-mode policies, `app.worker_claim` | Drop from the design | Only needed because RLS blocks cross-tenant claiming |
| An organisation failing does not stop the others | Keep | Each organisation's run is isolated by a try/except boundary and recorded |

The outbox dispatcher also loses its cross-tenant claim policy. It runs inside the same process, one organisation at a time, under that organisation's context.

### 4.7 ML

Unchanged in substance. One shared model trained on pooled synthetic outcomes. Evaluation is reported per organisation, with the number of accounts and churn cases shown beside every metric. Where an organisation has too few churn cases for a stable estimate, the report says so instead of printing a number that looks precise. The documentation states that pooled training on independent real customers would need their consent, and that this is not a question for synthetic data.

## 5. Security trade-offs

After these changes the application is a **single-operator demonstration with organisation scoping at two layers**. It is not a production multi-tenant security boundary, and the documentation must say so.

| Property | Before | After | Note |
|---|---|---|---|
| Client cannot pick its organisation | Yes | Yes | Organisation comes from the token |
| A missed `org_id` predicate cannot leak rows | Yes (RLS) | Yes (RLS, retained) | Tested at SQL level and, from Phase 4, through the API |
| Tokens are hashed at rest | Yes (HMAC and pepper) | No | Dev tokens sit in a local, gitignored file |
| Per-key revocation and last-used tracking | Yes | No | Not needed for a local demo |
| Worker cannot read other organisations by accident | Yes (claim policies) | Partly | The worker sets one organisation at a time, but nothing stops it setting another |
| Independence between tenants who hold tokens | Partial | None | A token holder trusts the operator |

Statements the documents must not make: that the application is secure for independent production tenants, that dev tokens are an authentication system, or that the worker is isolated per tenant.

## 6. Proposed changes and their impact

### Step 1: documentation (no approval beyond this review)

- Add ADR-022, superseding the parts of ADR-004, 010, 019, and 021 described above.
- Update `schema.md` (roles, cross-tenant operations, API key lookup, global tables, migration plan), `architecture.md`, `worker-design.md`, and `implementation-plan.md`, so the design matches this decision.
- Re-baseline the Phase 1 closure table in `phase-1-setup-and-changes.md`: criteria 7 and the key-hashing evidence describe the legacy path.

Existing defect found while reviewing: `schema.md` line 86 holds a stray `| postgres | ... |` table row after the claim-policy bullets, outside any table. It should be fixed in this step.

### Step 2: authentication adapter (application code, needs approval)

- Add an authenticator interface, the dev-token adapter, and `AUTH_MODE` in `Settings` with a startup warning.
- Add role-rule tests: each role against each route.
- Keep the key path working and tested. No file is deleted, and no migration is involved.

### Step 3: worker enumeration (needs approval only if option B is chosen)

- Option A: configuration-driven organisation list, with a startup check against the `organizations` table. No schema change.

### Step 4: role removal (needs approval, destructive on the dev volume)

- Remove `chi_scheduler` from the init script, compose, `.env.example`, fixtures, and tests, and drop the role from the dev database.

### Step 5: repository layer (from Phase 2)

- One organisation-scoped repository base class, and one helper that applies `tenant_isolation` to each new table, with a test that fails if a table has an `org_id` column and no policy.

### Migration impact summary

| Change | Migration needed? | Destructive? |
|---|---|---|
| Dev-token adapter | No | No |
| Keep `tenant_isolation` | No | No |
| Worker lists organisations from configuration (option A) | No | No |
| Disable RLS on `organizations` (option B) | Yes, forward migration 0003 | No data loss |
| Drop `api_key_lookup` and `api_keys` | Yes | Yes, though the table is empty. Deferred. |
| Drop role `chi_scheduler` | No migration; role change on the dev database | Yes, role only |

Any change goes in a new forward migration. Migration 0002 is not edited.

### Test impact

| Change | Tests affected |
|---|---|
| Step 2 | New tests only. Existing tests keep passing. |
| Step 4 | `tests/conftest.py` (the `scheduler_url` field and its fixture), `test_grant_matrix.py` (the scheduler privilege test), `test_tenant_isolation.py` (`test_scheduler_role_cannot_update_or_delete_audit_events`) |
| Option B (disable RLS on `organizations`) | `test_every_tenant_table_has_rls_and_force` asserts RLS on every table, so it needs an explicit exception for `organizations`. `test_no_tenant_context_returns_no_rows` and `test_cross_tenant_reads_are_invisible` both assert on `organizations` and would need those assertions removed. |
| Deferred table drop | 44 matching lines across seven files (see 4.2) |

Tests retained throughout: cross-tenant reads, writes, and updates; fail-closed reads and inserts; audit immutability; grant matrix; migration round trip. Added from Phase 4: organisation-scoped API tests for predictions and actions.

## 7. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| D1 | Approve Step 1 (documentation updates and ADR-022)? | Yes |
| D2 | Approve Step 2 (dev-token adapter, `api_keys` path kept, `dev_tokens` the default)? | Yes |
| D3 | Worker organisation listing: option A (configuration) or B (disable RLS on `organizations`)? | A |
| D4 | `audit_events.actor_key_id` is a UUID with no foreign key. For dev tokens, store a stable UUID derived from the token label, or add a text `actor` column in a migration? | Derived UUID, no migration |
| D5 | Approve removing `chi_scheduler` and dropping the role from the dev database? | Yes, after D1 |

## 8. After approval

The priority is the working end-to-end workflow. Once Steps 1 and 2 are done, Phase 2 starts: the three synthetic organisations with deterministic IDs and seeds, the generator, and ingestion with organisation-scoped repositories.
