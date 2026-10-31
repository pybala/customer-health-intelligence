# Unattended Worker Design

The worker is a deterministic, scheduled process. It runs the pipeline for every active organisation, records what it did, and never sends a customer message unless every policy check passes and the mode is live. There is no LLM in the loop.

## 1. Responsibilities and non-responsibilities

| Does | Does not |
|---|---|
| Trigger a run per organisation on schedule | Decide business policy (policies are pure functions in `agent/policies.py`) |
| Ingest, normalise, build features, score, and write snapshots | Train models (offline CLI only) |
| Create recommended actions (status `proposed` or `suppressed`) | Create outbox rows or call any email provider for a proposed action |
| Dispatch outbox messages through the configured adapter | Retry forever, or retry non-transient errors |
| Record stage status, errors, and dead letters | Read or write across organisations in one transaction |

## 2. Process model

> **Amended by ADR-022.** The design below is the original. What will be built: one sequential process, an explicit organisation list validated at startup, `UNIQUE (org_id, cutoff_at)` for idempotency, and a Postgres advisory lock so a second worker exits. The `SKIP LOCKED` claim, leases, the one-running-run index, claim-mode policies, `WORKER_CONCURRENCY`, and the scheduler role are deferred. Of this file, only the startup check is implemented so far (Phase 2): `WORKER_ORG_IDS` is parsed into a unique list of organisation IDs and `app/worker/startup.py::validate_worker_orgs` fails startup if the list is empty or any ID is not in `organizations`. The worker loop, stages, policies and outbox are not implemented.

- One worker process runs a scheduler loop and a small pool of run executors (default 1 for the demo, configurable).
- The scheduler does not run pipeline stages. It only inserts `pipeline_runs` rows with status `pending` for each active organisation whose last successful run is older than the schedule interval.
- Executors claim one run at a time:

```sql
UPDATE pipeline_runs SET status = 'running', claimed_by = $worker, claimed_at = now(), attempts = attempts + 1
WHERE id = (
  SELECT id FROM pipeline_runs
  WHERE status = 'pending' AND (not_before IS NULL OR not_before <= now())
  ORDER BY cutoff_at, id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

- Executors run in one organisation context at a time. `SET LOCAL app.current_org_id` is set inside each stage's transaction, not left on a pooled connection.
- Multiple worker containers are allowed. The claim makes them safe, and only one run per organisation can be `running` at a time (partial unique index on `pipeline_runs (org_id) WHERE status = 'running'`).

## 3. Schedule and cutoffs

| Setting | Default | Notes |
|---|---|---|
| `WORKER_SCHEDULE_INTERVAL` | `24h` | Demo default. Configurable. |
| `WORKER_CUTOFF_TZ` | `UTC` | Cutoff is 00:00 UTC of the run date. |
| `WORKER_CATCHUP` | `false` | When true, missed days are run in cutoff order, one per day, to rebuild history. Off by default because catch-up can create large writes. |

The cutoff is part of the run identity: `pipeline_runs.cutoff_at` plus `org_id`. Running the worker twice for the same cutoff finds the existing run and does not create a second one, so scheduling is idempotent by construction.

## 4. Stages

| Order | Stage | Input | Output | Idempotency key |
|---|---|---|---|---|
| 1 | `ingest` | Generated or delivered raw payloads for the org | `raw_source_records` rows | `(org_id, source, source_record_id, payload_sha256)` |
| 2 | `normalise` | New raw records | `source_events`, `account_identities`, `data_quality_issues` | `(org_id, source, dedupe_key)` |
| 3 | `features` | Events with `event_time <= cutoff` and `ingested_at <= cutoff` | `feature_snapshots` | `(org_id, account_id, cutoff_at, feature_set_version)` |
| 4 | `score` | Feature snapshots and the registered model | `prediction_snapshots`, `prediction_evidence` | `(org_id, account_id, model_version_id, cutoff_at)` |
| 5 | `policy` | Predictions, suppression rules, existing actions | Action decisions in memory | Pure, recomputed each run |
| 6 | `enqueue_actions` | Policy decisions | `actions` rows with status `proposed` or `suppressed`. No outbox rows. | `actions (org_id, account_id, action_type, scoring_window)` |
| 7 | `dispatch` | Outbox rows in `pending` or `retry_due` | Outbox state transitions, `mock_email_messages` | `outbox_messages` state machine |

### Stage rules

- Each stage is a function `run(ctx) -> StageResult`. It receives a connection bound to one organisation and returns counts. It does not commit across stages.
- A stage either completes in one transaction or commits per batch with a watermark, so a crash resumes from the last committed batch and does not restart the whole run.
- Stage status is recorded before and after execution in `pipeline_stage_runs`. A stage found `running` with an expired lease is treated as crashed and re-run; idempotent keys make that safe.

## 5. Action generation and policy

`policies.py` takes plain values and returns a decision. It has no database or model dependency, so its tests are unit tests.

Input: `risk_band`, `risk_score`, `coverage_count`, `contributions` (top drivers), `account.segment`, `suppression_rules` (as loaded values), `last_contact_at`, `open_escalation`, `do_not_contact`, `already_actioned_in_window`.

Decision order (first match wins):

1. `do_not_contact` is true: `suppressed` with reason `do_not_contact`.
2. `legal_hold` is true: `suppressed` with reason `legal_hold`.
3. `open_escalation` is true: `suppressed` with reason `open_escalation`. The escalation owner handles it.
4. `already_actioned_in_window`: no new action (deduplication, not recorded as suppression).
5. `last_contact_at` within `recent_contact_days` (default 14): `suppressed` with reason `recent_contact`.
6. `risk_band == 'high'` and `coverage_count >= 2`: propose `executive_check_in` if segment is `enterprise`, else `renewal_review`.
7. `risk_band == 'high'` and the top driver is a support signal: propose `support_follow_up`.
8. `risk_band == 'medium'`: propose `renewal_review` only if `risk_score` has increased by at least a configured delta since the last scored window.
9. Otherwise: no action.

Important: a proposed action is a recommendation for a CSM. Proposing an action does not send anything. The approval step (below) is the only way an action becomes sendable.

### Human approval gate

- The worker never creates an outbox row for a `proposed` action. Only the approval transaction does.
- `POST /api/v1/actions/{id}/approvals` runs one transaction: it inserts `action_approvals`, moves the action from `proposed` to `approved`, re-runs the suppression check, and inserts one `outbox_messages` row. The unique key on `(org_id, dedupe_key)` makes a repeated approval return the existing row.
- Dry-run mode (`OUTREACH_MODE=dry_run`, default): the approval creates the outbox row in state `pending`, and the dispatcher marks it `dry_run` without calling the adapter. The rendered message is stored for inspection.
- Live mode (`OUTREACH_MODE=mock_send`): the dispatcher calls the mock adapter. The real adapter is not implemented in this build.

Approval is required even in mock send mode, so the demo shows the same gate that would protect a real deployment.

### Editable drafts (approved 2026-10-29, not yet implemented)

The worker proposes an action automatically and attaches a draft message. A human decides what, if anything, is sent.

- The draft is rendered from a deterministic template, filled with account details (name, segment, renewal date) and evidence that traces to stored data (the top feature contributions and the `prediction_evidence` events behind them). No LLM is involved in scoring, eligibility, or drafting.
- The draft is stored on the action as `draft_subject` and `draft_body`, together with `template_id` and `template_version`, so the original rendering can always be reproduced.
- A CSM can edit subject and body before approving. An edit is recorded as an audit event with the editing key, time, and a hash of the previous and new text. The approval records the exact text approved, and that text is what goes to the outbox. Approving does not silently re-render over an edit.
- Edits are limited to length, and must keep the unsubscribe or opt-out line that the template adds. Evidence references stay attached even if the text around them changes.
- This needs one more API operation than the design in `architecture.md` lists (an edit endpoint, for example `PATCH /api/v1/actions/{id}/draft`), and a contract change before it is built.

## 6. Outbox and safe outreach

### Why an outbox

Writing the action and sending the email are two side effects. Without an outbox, a crash between them either loses the send or duplicates it. The outbox records the intent in the same transaction as the decision; the dispatcher performs the external effect afterward and records the result.

### Dispatcher algorithm

```
loop:
  claim up to N rows WHERE state IN ('pending','retry_due') AND next_attempt_at <= now()
        FOR UPDATE SKIP LOCKED
  mark them 'sending', attempts += 1, commit
  for each row:
     re-check policy (suppression rules may have changed since enqueue)
        -> if now suppressed: state = 'failed', reason recorded, audit row
     call adapter.send(message, idempotency_key = outbox.id)
        -> success: state = 'sent', sent_at, audit row
        -> transient error (timeout, 429, 5xx): if attempts < max_attempts:
               state = 'retry_due', next_attempt_at = now + backoff(attempts) + jitter
           else: state = 'dead_letter', audit row, alert record
        -> permanent error (4xx validation): state = 'failed', audit row
  sleep when queue empty
```

### Retry policy

| Parameter | Default |
|---|---|
| `max_attempts` | 5 |
| Backoff | Exponential, base 30 s, factor 4, cap 1 h, full jitter |
| Transient | Timeout, connection error, HTTP 429, HTTP 5xx |
| Permanent | HTTP 4xx other than 429, schema validation failure, suppression matched on re-check |

Retries are bounded and delayed. A message in `sending` longer than its lease (default 5 minutes) is reclaimed and retried. The adapter receives the same `idempotency_key` on retry, so a provider that supports idempotency cannot double-send.

### Deduplication

- `outbox_messages (org_id, dedupe_key)` is unique. `dedupe_key = hash(org_id, account_id, action_type, scoring_window, channel)`.
- The same action cannot produce two messages, even if the worker runs twice for the same window.
- Within one account, a configurable minimum gap (default 7 days) applies across action types, enforced in `policies.py` using the sent-message history.

### Suppression policy

Suppression is evaluated at three points: at proposal (policy stage), at approval (API), and at dispatch (re-check). Any one of them blocks the send. Checking at dispatch matters because a rule can become active between approval and send.

### Dry-run and audit

- Every decision, approval, and state change writes an `audit_events` row in the same transaction as the change.
- The dry-run output is visible in the dashboard and API (`GET /api/v1/outbox?status=dry_run_recorded`), with rendered content and the policy reasons.

## 7. Failure handling

| Failure | Behaviour | Recovery |
|---|---|---|
| Source file unreadable | `ingest` stage fails, run marked `failed` with error | Retry run with backoff (max 3 run attempts), then `dead_letter` |
| Malformed record | Record stored in `raw_source_records` with a parse error in `data_quality_issues`; not normalised | Visible in data-quality view; never blocks other records |
| Identity ambiguous | Event stored with `identity_status = ambiguous` | Excluded from features until resolved |
| Model artifact checksum mismatch | `score` stage refuses to run; run `failed`; alert record | Operator re-registers a valid version |
| Feature-set version mismatch | `score` refuses to run | Rebuild features for the registered version |
| Database unavailable | Process exits non-zero after retry with backoff; container restarts | Restart policy in Compose |
| Worker crashes mid-stage | Lease expires; stage re-run | Idempotent keys |
| Adapter timeout | Outbox retry per policy | Backoff, dead letter after max attempts |
| Clock skew | Cutoffs come from the run record, not the executing host's current time, when re-running | Tested |

Each failure writes: stage status, error text (truncated, no payload content), and an `audit_events` row with `event_type = 'worker.failure'`. Error text never includes raw payload fields or recipient addresses.

## 8. Observability

- Structured JSON logs with `run_id`, `org_id`, `stage`, `cutoff_at`, `attempt`, `duration_ms`, and counts. The organisation ID is logged as an internal ID, not a name.
- Logs never contain API keys, email addresses, or raw payloads. A log-filter test asserts this on a sample run.
- Metrics (counters in logs for the demo, Prometheus endpoint out of scope): runs by status, stage duration, events ingested, duplicates, late arrivals, unmatched identities, outbox state counts.
- Health: `GET /api/v1/health` reports database reachability, the registered model version, and the timestamp of the last successful run per organisation. It returns no tenant data beyond counts.

## 9. Configuration

All configuration comes from environment variables, validated at startup with pydantic settings. Unknown or missing required values stop the process.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | none (required) | Application role (`chi_app`) connection string |
| `WORKER_ORG_IDS` | none (required for worker) | Comma-separated organisation IDs to process, validated against `organizations` at startup (ADR-022). Replaces `SCHEDULER_DATABASE_URL`. |
| `MIGRATION_DATABASE_URL` | none | Owner role, used only by `migrate` |
| `WORKER_SCHEDULE_INTERVAL` | `24h` | Run frequency |
| `WORKER_CONCURRENCY` | `1` | Run executors |
| `OUTREACH_MODE` | `dry_run` | `dry_run` or `mock_send`. Any other value fails startup. |
| `EMAIL_ADAPTER` | `mock` | Only `mock` is implemented. Any other value fails startup. |
| `RECENT_CONTACT_DAYS` | `14` | Policy input |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Retry limit |
| `API_KEY_PEPPER` | none (required) | HMAC pepper for key hashes |
| `MODEL_ARTIFACT_DIR` | `artifacts/models` | Only location models are loaded from |

Defaults that are unsafe are not defaults. In particular, there is no default for `API_KEY_PEPPER` and no way to select a live sender without a code change. `OUTREACH_MODE=dry_run` is the default because a missing environment variable must not result in sends.

## 10. Testing the worker

| Test | Type | Assertion |
|---|---|---|
| Two runs for the same org and cutoff | Integration | One run row, identical outputs |
| Crash after `score` before `enqueue_actions`, then rerun | Integration | No duplicate predictions or actions |
| Second worker started while one runs | Integration | The second exits on the advisory lock; no org has two running runs |
| Outbox crash after `sending` | Integration | Lease expiry reclaims; one `sent` row |
| Adapter 503 five times | Integration | `dead_letter` after `max_attempts`, audit written |
| Adapter 400 | Integration | `failed` immediately, no retry |
| Suppression added between enqueue and dispatch | Integration | Dispatch re-check blocks send |
| Dry-run mode | Integration | No adapter call; state `dry_run` |
| Missing `API_KEY_PEPPER` or unknown `OUTREACH_MODE` | Unit | Startup fails |
| Policy decision table | Unit | Each rule in section 5, in order |
| Log output scan | Security | No email addresses, keys, or payload fields in logs |

## 11. What breaks first at 10x

The first limit is the `features` stage recomputing each account's features over the full history on every run. At 10x (roughly 50,000 accounts and 2 million events) that becomes the longest stage. The planned fix is incremental feature tables keyed by `(org, account, feature, as_of_day)`, which are described in the README, not built in this phase.

The second is the single-claim executor per organisation. Large organisations will serialise behind one run. Splitting the features stage by account shard is the fix.
