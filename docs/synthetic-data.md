# Synthetic Data Strategy

## Goals

- Reproducible: the same seed and configuration produce byte-identical output and identical checksums.
- Exact totals: 1,000 emitted source records and 50 accounts across all organisations, enforced by a post-generation check that fails the run if totals differ.
- One canonical dataset: the same committed fixture serves demo, seeding, ingestion, ML and tests. No separate large-data path exists.
- Small by design: 50 accounts is enough to exercise the pipeline, but not enough for statistically meaningful model evaluation (see `ml-design.md` section 12).
- Realistic messiness: missing records, duplicates, late and out-of-order arrivals, identity mismatches, and uneven source coverage, injected at documented rates.
- Learnable but not trivial: churn is driven by latent account health, so models can find signal, but the signal is noisy, and some churners look healthy until late.
- No real data. No real names, domains, email addresses, or ticket text. Names come from a fixed synthetic lexicon and domains use reserved example TLDs.

## Generation pipeline

```
config (seed, counts, rates)
   -> latent account generator     (accounts, segments, plans, latent health)
   -> renewal timeline generator   (contracts, renewals, churn outcomes)
   -> source event generators x5   (events per source, clean)
   -> defect injector              (duplicates, late, out-of-order, identity noise, gaps)
   -> raw payload writer           (per-source JSON payloads with source-native shapes)
   -> validator                    (totals, rate checks, referential checks, checksum)
   -> manifest.json                (seed, config hash, counts, file checksums)
```

Each stage takes its own derived random generator from `seed` (for example `numpy.random.SeedSequence(seed).spawn(...)` per stage). Adding or reordering a stage does not change the output of the others.

## Scale and history

| Parameter | Default | Notes |
|---|---|---|
| Organisations | 3 | Demo tenants with different sizes and mixes (see below). |
| Accounts | 50 total | Split 24 / 16 / 10 across the three orgs. |
| History | 1,096 days (36 months) | Ending at the generation anchor date, 2026-10-01. Default seed 20261001. |
| Temporal test window | renewals from 2026-04-01 (implemented split; see `ml-design.md` section 2) | Used for a single evaluation pass. It holds 1 churned renewal term, so it cannot support model conclusions. |
| Source records | exactly 1,000 emitted (after defects) | Split across five sources by the target mix below. |
| Renewal cycle | Per-org mix of monthly, quarterly, annual and biennial terms (see Organisations) | Churn is non-renewal at the scheduled renewal date, or cancellation effective within 30 days after it (see ML design). |

### Event budget (exactly 1,000 emitted)

The generator allocates the total to sources before generating, then checks the final count after defect injection. Duplicates count as events, so the budget applies to the emitted rows, and the clean and defective counts are reported separately in the manifest.

| Source | Target share | Target events | Typical event types |
|---|---|---|---|
| Product usage | 45% | 450 | `login`, `feature_used`, `seat_added`, `api_call_batch`, `export` |
| Gmail (metadata only) | 15% | 150 | `thread_sent`, `thread_received`, `meeting_invite` (metadata: counts, timestamps, direction) |
| Zendesk | 15% | 150 | `ticket_opened`, `ticket_solved`, `csat_response`, `escalation` |
| Salesforce | 15% | 150 | `opportunity_stage_change`, `activity_logged`, `contact_role_change`, `renewal_forecast_update` |
| Stripe | 10% | 100 | `invoice_paid`, `invoice_failed`, `subscription_updated`, `payment_method_expired` |

Coverage is uneven on purpose. Not every account has every source (see coverage below), so the budget is spread across accounts with a skewed distribution (Pareto-like): a small share of enterprise accounts produces most product events.

## Account generation

| Attribute | Distribution |
|---|---|
| Segment | `smb` 60%, `mid_market` 30%, `enterprise` 10% |
| Plan tier | Derived from segment with noise (for example, enterprise mostly `business` or `enterprise`) |
| Seats | Log-normal, scaled by segment |
| Contract value | Derived from seats and tier, with discount noise |
| Region | Four regions with fixed weights |
| Latent health `h` | Standard normal per account, adjusted by segment and tenure |
| Source coverage | Each account is assigned a coverage profile (see below) |
| Renewal date | The account's pending renewal date after the anchor, written as `renewal_date` in the account master. Null for accounts with no renewal after the anchor (18 of 50 in the canonical seed). |

### Coverage profiles

| Profile | Share | Sources present |
|---|---|---|
| Full | 30% | All five |
| CRM and billing | 25% | Salesforce, Stripe, product |
| Support heavy | 15% | Zendesk, Gmail, Stripe |
| Product only | 20% | Product, Stripe |
| Sparse | 10% | Stripe and at most one other source |

Sparse accounts have less signal. Metrics are reported by coverage profile so the evaluation shows where the model is weak instead of hiding it in an average.

## Churn outcome model (the ground truth generator)

The generator, not the model, defines churn. Keeping this separate from the modelling code prevents the model from being graded against its own assumptions. Implemented in `app/ingestion/generators/world.py`.

1. Each account has a latent health that evolves per renewal term as an autoregressive drift (drift sd 0.4), adjusted by segment.
2. Churn logit at each renewal: `intercept[term] - 0.9 * health + 0.5 * (10 * seat_decline) + noise(sd 0.8)`. Intercepts: monthly -3.0, quarterly -2.3, annual -2.0, biennial -1.8. Longer terms churn more often per renewal.
3. The renewal outcome is drawn from the resulting probability.
4. Hidden structure is included on purpose:
   - Quiet churners: 15% of churners show their signal late (the decline is scaled by `fraction ** 4` through the term), so early cutoffs see almost nothing.
   - False alarms: 12% of non-churners get a signal drop (-1.5 on health) and then renew anyway.
5. Events are thinned by `exp(k * health)` per event type, so healthy accounts produce more product, email and paid-invoice events. Escalations and failed invoices have negative slopes, so they become more frequent as health falls.

Achieved churn rates are written to the manifest (canonical seed): pooled 12.9%; acme-saas 14.1%, northwind-cloud 12.3%, globex-data 10.5%. By term: monthly 4.2%, quarterly 21.4%, annual 22.5%, biennial 0% (one biennial renewal, so not meaningful). The configured acceptable range is 12% to 22%, so the pooled rate sits close to the lower edge. The original design target of 15 to 20% was not met for acme-saas and northwind-cloud. The rate is a configuration value, not a fact about real customers.

Coefficients and rates are in the generation config and the manifest so the ground truth is auditable.

## Source-native payload shapes

Each source has its own raw schema, mirroring how real exports look. The adapters must parse these shapes, not a pre-cleaned format.

| Source | Native key | Notable shape details |
|---|---|---|
| Salesforce | `Account.Id` (18-char style) plus `Contact.AccountId` | Stage names with spelling variants across time (`Closed Won` / `Closed-Won`). Some account IDs are missing on contacts. |
| Gmail | Thread ID plus participant domains | Metadata only, no bodies. Domain may differ from the account's primary domain (identity mismatch). |
| Zendesk | `organization_id` (numeric) | Organisation name typed by agents, with casing and suffix variants (`Acme Inc.`, `ACME`, `acme inc`). |
| Product events | `account_external_id` and `user_id` | Timestamps in local time with offset; some events have no offset (timezone ambiguity). |
| Stripe | `customer` (`cus_...`) | Amounts in minor units. Some customers are linked to a different account via a stale mapping (identity mismatch). |

## Defect injection

Rates are configuration values, and the actual achieved rate is written to the manifest.

| Defect | Default rate | Description | Expected pipeline behaviour |
|---|---|---|---|
| Exact duplicate | 2.0% of events | Same payload re-sent | Deduplicated by `(org, source, dedupe_key)`. Logged as `duplicate`. |
| Revised record | 0.5% | Same source record ID, changed content (for example, stage moved back) | Stored as new raw revision. Latest revision wins for state. Logged as `revised_record`. |
| Late arrival | 4% | `ingested_at` is 3 to 45 days after `event_time` | Kept. Counted in data quality. Not used for predictions made before its ingestion time. |
| Out-of-order delivery | 6% of batches | Batch rows shuffled within a window | Sorted by `event_time` during feature build. Results do not depend on arrival order (tested). |
| Missing source record | 3% of expected records | A source drops records in a window | Features use what exists. Coverage flags are recorded as features (see ML design). |
| Identity mismatch: domain | 2% of Gmail events | Participant domain belongs to an acquired or personal domain | Resolved by domain map if confident, otherwise `ambiguous_identity`. |
| Identity mismatch: name variant | 5% of Zendesk org names | Casing, suffixes, typos | Normalised. Fuzzy match with threshold. Below threshold, `unmatched_identity`. |
| Stale mapping | 1% of Stripe customers | Maps to a previously owned account | Detected when the CRM reference in the Stripe metadata points to a different account than the customer key; flagged `ambiguous_identity`. |
| Salesforce missing AccountId | 4% of Salesforce events | No `AccountId`; `ContactDomain` remains | Resolved by domain when it is known, otherwise `unmatched_identity`. |
| Timezone-less timestamp | 3% of product events | No offset | Assumed to be the account's configured timezone (UTC if the account is unknown), flagged as `timezone_assumed`. |
| Malformed record | Not injected | Unknown event type, missing key, unparseable time | Not written to `source_events`; logged as `malformed_record`. Covered by adapter unit tests. |

Injected defects are recorded in a side file (`defects.ndjson`) with the original clean record ID. This allows tests to assert that every injected duplicate is removed and every injected identity mismatch is either resolved or flagged. Without this, "passes" would be an unverifiable claim.

## Organisations (demo tenants)

| Org slug | Accounts | Event share | Character | Term mix (monthly / quarterly / annual / biennial) |
|---|---|---|---|---|
| `acme-saas` | 24 | 48% | Mostly mid-market, strong product and Zendesk coverage | 8 / 22 / 70 / 0 |
| `northwind-cloud` | 16 | 32% | SMB-heavy, product-only and sparse profiles dominate | 15 / 25 / 60 / 0 |
| `globex-data` | 10 | 20% | Enterprise-heavy, long terms including biennial | 0 / 10 / 60 / 30 |

Each org's random streams derive from the master seed and the org slug (`SeedSequence([seed, crc32(slug), stage, source])`), so one org's files do not change when another org's size changes. A test asserts this.

## Validation rules (run after generation, fail the build on violation)

1. Total emitted source records (after defect injection) equals exactly `1,000`.
2. Account count equals the configured total.
3. Per-source counts match the budget within the tolerance set in config, which is zero by default.
4. Every non-null `account_id` in the ground-truth file exists in `accounts`.
5. Achieved defect rates are within configured tolerance of target rates.
6. Churn base rate is within the configured range.
7. Re-running with the same seed produces the same SHA-256 for every output file.
8. No payload contains values matching real-looking email patterns outside the reserved `example.test` domain family.

## Reproducibility and storage

- Output: `fixtures/canonical/<org-slug>/` with `accounts.ndjson`, one NDJSON file per source, `ground_truth.ndjson` and `defects.ndjson`, plus `fixtures/canonical/manifest.json`. NDJSON was chosen over Parquet to avoid a new dependency; 1,000 records do not need columnar storage.
- `fixtures/canonical/` is committed. It is regenerated with `python -m app.cli generate-fixtures`, and the generator's output must match the committed files byte for byte (a test checks this).
- `manifest.json` records the generator version, config hash, seed, per-org counts, achieved defect counts, churn rates and a SHA-256 for every file. It does not record a git commit because the generator does not shell out to git.
- `cli seed` loads the canonical fixtures into `raw_source_records` per org and runs the normal pipeline. It is idempotent: re-running adds no rows. It does not write `source_events` directly.

## Ground truth and evaluation separation

`ground_truth.ndjson` (true churn labels and latent health) is never read by the ingestion, feature, or scoring code. It is read only by the offline training and evaluation workflow, to build labels and to report how far the model is from the generator's ceiling. The API, dashboard, and worker never read it, and the dashboard never shows ground truth.

Two different things are called "renewal" in this data. `renewal_date` in the account master is the account's pending renewal date and is the only renewal field runtime code may read, and only to select the 90-day scoring window. The renewal outcome (churned or renewed) lives only in `ground_truth.ndjson` and is the offline label. It is never a feature. See `ml-design.md` section 3.

## Known limitations of the synthetic data

- Causal structure is designed by hand, so good model results show the pipeline can learn the planted signal, not that the model would work on real churn. This is stated in the model card.
- Real data has concept drift, pricing changes, and policy changes not modelled here.
- Gmail is metadata only. Real Gmail signal such as sentiment cannot be represented and is not used.
- Churn rate for two of three orgs is below the original 15 to 20% target (see the churn model section). Raising it means changing the intercepts and re-running the validity check in `ml-design.md` section 12.
- At 50 accounts the labelled test window has 12 accounts and one churned renewal term. The validation window has two. Evaluation on this dataset is a pipeline check, not a performance measurement.
- The renewal-term count for the offline workflow is 149 rows in ground truth: 140 observable and 9 censored. Of the 140, 138 produce training examples. The two that do not are 2023 renewals on one account, which fall before the training window (`ml-design.md` section 2.1).
- The 2% duplicate, 4% late and similar rates are round numbers chosen for the demo, not measured from any real system.
- Identity defects are injected independently. Real systems often have correlated failures (one acquisition renaming many accounts at once), which are easier to resolve in bulk and harder to detect individually.
