# ML Design

This document defines the supervised problem, labels, features, leakage controls, and evaluation. The model requirements from the business are to be supplied later; the choices here are demonstration defaults and are recorded as assumptions.

## 1. Problem definition

| Item | Decision |
|---|---|
| Task | Binary classification: will the account churn at its next scheduled renewal within the prediction horizon? |
| Unit of prediction | One account at one scoring cutoff. |
| Scoring cutoff | Daily, at 00:00 UTC of the run date. Only data with `event_time` and `ingested_at` at or before the cutoff is visible. |
| Prediction horizon | 90 days (approved). The target is the probability that an account fails to renew within the next 90 days. An account is in scope if it has a scheduled renewal date within `(cutoff, cutoff + 90d]`. |
| Label | `churn = 1` if the account does not renew at that renewal, or cancels effective within 30 days after the renewal date. Otherwise `0`. |
| Observation window | Features use only events visible at the cutoff: `event_time <= cutoff` and `ingested_at <= cutoff`. Lookbacks never reach past the cutoff. |
| Label observable | The label is known at `renewal_date + 30 days`. An example is labelled only if that date is on or before the data end (the anchor date). |
| Censored accounts | A renewal whose `renewal_date + 30 days` falls after the data end is censored. It is not labelled, not used for training, validation or test, and not treated as a non-churn. Such accounts are still scored at serving time; they just have no label yet. Accounts that churned and left earlier produce no later examples. |
| Cutoffs for training | Weekly. Daily cutoffs are for serving only. Each `(account, weekly cutoff)` with a renewal inside the horizon and an observable label is one example. |
| Model scope | One shared model across all three organisations (approved). Feature computation, storage, scoring and API access stay per organisation. No organisation identifier is used as a feature. |
| Output | Probability `risk_score` in [0, 1], a `risk_band` (`low`, `medium`, `high`) from thresholds fixed in config, and contributions per feature. |
| Business use | Rank accounts for CSM attention. The threshold for "high" is a default pending business input (see solution brief question 5). |

Why this framing: churn is a decision the CSM can still act on before the renewal, so the cutoff must be before the renewal, not after it. Predicting "will churn ever" would mix accounts that churn in five years with accounts due next month, and would be less actionable.

## 2. Training set construction

Training examples are created by replaying history at many past cutoffs, not by taking one snapshot per account.

- Cutoffs: weekly, from month 1 to the end of the training window.
- For each `(account, cutoff)` where a renewal falls within the horizon and its outcome is observable by the data end, create one example.
- An account can contribute several examples across cutoffs, so the same account appears in training many times. Splits are therefore done by time, never randomly by row, and grouped by account for any cross-validation fold.

| Window | Period (relative to 36-month history) | Use |
|---|---|---|
| Feature warm-up | Months 1 to 3 | Only used to compute lookbacks. No labels. |
| Train | Months 4 to 24 | Fit candidates. Cross-validation by time blocks inside this window. |
| Validation | Months 25 to 30 | Hyperparameter choice and model selection. |
| Temporal test | Months 31 to 36 | Touched once, for the final registered model. |

The dates are relative and computed from the generation anchor date. Implemented boundaries (`app/ml/model.py`): train renewals before 2025-09-30, validation renewals from 2025-09-30 up to 2026-04-01, test renewals from 2026-04-01. The split is by renewal date. The boundaries are stored in the model metadata (`splits`) and the selection hash. They are not written to a `training_cutoff_at` column, which does not exist.

### 2.1 How the 1,747 examples are built

The canonical dataset has 149 renewal-term rows in `ground_truth.ndjson`. 140 of them are observable (the label is known by the data end). The other 9 are censored and are not used.

Each example is one `(account, weekly cutoff)` pair where the renewal falls inside `(cutoff, cutoff + 90 days]`. Cutoffs run weekly from 2024-01-01 to 2026-09-30 (144 cutoffs). A single renewal term therefore produces several examples, one per weekly cutoff in its 90-day look-back. Observed counts per term range from 3 to 13.

| | Count |
|---|---|
| Renewal-term rows in ground truth | 149 |
| Observable (labelled) terms | 140 |
| Terms that produce at least one example | 138 |
| Examples | 1,747 |
| Examples per term (min / max) | 3 / 13 |

**Why 2 observable terms produce no examples.** Both are 2023 renewals on the same northwind account (2023-11-17 and 2023-12-17). Their 90-day look-back ends before the training window starts on 2024-01-01, so no weekly cutoff falls in their horizon. Both renewed. Excluding them does not change any label or any split.

**Labels.** Each example carries the term's outcome. The label is the same for every cutoff of a term. The examples of one term are therefore not independent: they share a label, an account, and most of their events.

### 2.2 Splits, overlap and leakage risk

| Split | Examples | Accounts | Renewal terms | Churned terms | Churned example rows |
|---|---|---|---|---|---|
| Train | 1,026 | 34 | 82 | 15 | 195 |
| Validation | 386 | 17 | 30 | 2 | 26 |
| Test | 335 | 12 | 26 | 1 | 13 |

- **Term overlap across splits: 0.** No renewal term appears in more than one split, because the split is by renewal date.
- **Account overlap across splits: 11 (train and validation), 8 (train and test), 5 (validation and test).** The same account appears in several splits, with different renewal terms. Its features and behaviour are correlated across them.
- **Horizon straddling.** 108 examples have a cutoff before the train boundary and a renewal after it. Their look-back covers the boundary, so their features and labels can refer to the same period as the training examples. The split is by renewal date, not by cutoff date, so this is expected. It is one reason the validation and test windows are not fully independent of training.

What this means for independence: the effective sample size is closer to the number of renewal terms and churned terms than to the number of examples. Within a split, rows that share a term share a label, so the row count overstates the information. Across splits, shared accounts mean a model can learn account-specific patterns that look like generalisation. The temporal split reduces, but does not remove, this leakage risk. It was not checked by a separate test in Part 2.

### 2.3 Realism of the split

The split is chronological, so the validation and test windows are later than the training window. That is the right shape for a "predict the next renewal" task. It is not a realistic simulation of production, for three reasons:

- The windows have 2 and 1 churned terms. A metric on them describes one or two outcomes.
- The base rate changes sharply between windows (train 195 / 1,026 = 19.0% of example rows, validation 6.7%, test 3.9%). A model fitted on train faces a different base rate at evaluation time, so the prior's score and any calibration would not transfer.
- The dataset is synthetic, and the generator, not real customer behaviour, defines the churn outcome.

## 3. Feature plan

Features are declared in `features/definitions.py`. Each feature has: name, source(s), window, aggregation, missing-value rule, and as-of rule. Every feature is computed by one function that takes `(account_id, cutoff_at)`.

### Feature families

| Family | Example features | Window | Source |
|---|---|---|---|
| Product engagement | `active_users_30d`, `active_users_trend_60v60` (ratio of last 60 days to previous 60), `days_since_last_login`, `api_calls_30d_log`, `seat_utilisation` | 30, 60, 90 days | Product events |
| Seat dynamics | `seat_change_90d`, `seat_peak_ratio` | 90 days, all time | Product, Salesforce |
| Support | `tickets_90d`, `escalations_90d`, `open_tickets`, `median_resolution_hours_90d`, `csat_90d_mean` | 90 days | Zendesk |
| Communication | `threads_30d`, `outbound_ratio_90d`, `days_since_last_contact`, `exec_contact_recent` | 30, 90 days | Gmail metadata |
| CRM | `stage_regressions_180d`, `contract_value_log`, `renewal_forecast_delta_90d` | 180 days | Salesforce |
| Billing | `failed_payments_60d`, `days_since_last_payment`, `payment_method_expired_flag`, `invoice_amount_trend` | 60 days | Stripe |
| Account profile | `segment`, `plan_tier`, `region`, `tenure_months` | as of cutoff | Accounts |
| Coverage | `has_salesforce`, `has_gmail`, `has_zendesk`, `has_product`, `has_stripe`, `coverage_count` | 90 days | Derived |
| Data quality | `late_arrival_share_90d`, `identity_ambiguous_flag` | 90 days | Data quality issues |

Coverage and data quality features are included on purpose. They let the model express "I have less information here" and make sparse-coverage behaviour visible. They are also a source of potential bias; see section 8.

### Renewal date and renewal outcome

Two different things share the word "renewal" and must not be confused:

- **`renewal_date` (account attribute).** The account's next renewal date, stored as an explicit field on the account contract (`accounts.renewal_date`, populated in the canonical account master). Runtime scoring uses it only to decide whether an account is in the 90-day scoring window (`renewal_date` within the next 90 days of the scoring time). It is not a model feature: the account record holds only its current value, so using it at a past cutoff would expose the renewal date that the cutoff is meant to predict. `days_to_renewal` is therefore not a feature.
- **Renewal outcome (offline label only).** Whether the account churned at the relevant renewal. It exists only in `ground_truth.ndjson`, is read only by the offline training and evaluation workflow, and is never a feature, a runtime input, or a column the API, dashboard, or worker reads.

Label 1 means the account churned at the relevant renewal (or cancelled within 30 days of it). Label 0 means it renewed. Accounts without an observable outcome are excluded, not labelled 0.

### Encoding and missing values

- Numeric features: missing is imputed with the training-set median, and an indicator column `<feature>__missing` is added when the missing rate in training exceeds 1%.
- Categorical features: one-hot with an explicit `unknown` level.
- Count features: `log1p`.
- Ratios: computed with a small epsilon and clipped to the training 1st and 99th percentiles.
- Imputation statistics and clipping bounds are fitted on training data only and stored with the model artifact.

## 4. Leakage prevention

Leakage is the most likely way to produce a convincing but wrong model, so it is controlled in several places.

| Risk | Control |
|---|---|
| Future events in features | Feature builder filters `event_time <= cutoff AND ingested_at <= cutoff`. Both conditions are needed: a late-arriving event that happened before the cutoff but was ingested after it is not visible at that cutoff. |
| Renewal outcome in features | The renewal outcome label and any cancellation or Stripe status after the cutoff are never features. `renewal_date` is not a feature either (see section 3). A denylist check at feature build time is planned for Phase 3. |
| Post-cutoff status | Salesforce stage is taken from the event history as of the cutoff, not from the current record. Stage changes after cutoff are not visible. |
| Label leakage through the split | Split is by time. Cross-validation folds are time-blocked and grouped by account, so an account's future is never in the training fold for its past. |
| Preprocessing leakage | Imputers, scalers, clipping bounds, and encoders are fit inside the training pipeline only. Validation and test use `transform`. |
| Ground-truth leakage | Option A: `ground_truth.ndjson` is readable only by the offline training and evaluation workflow (`app/cli.py`, `app/ml/train.py`, `app/ml/evaluate.py`). Runtime scoring, the API, the dashboard, the worker, and feature engineering never read it. Enforced by `tests/ml/test_ground_truth_isolation.py`. |
| Feature-set drift | `feature_set_version` is stored in `feature_snapshots` and `model_versions`. Scoring refuses a model whose feature set version differs from the built features. |
| Re-scoring with late data | Re-running a past cutoff rebuilds features with the same visibility rule and produces the same snapshot. A test asserts that scores for a past cutoff do not change when events ingested after that cutoff are added. |

### Leakage tests (tests/ml)

Built (Part 1):

1. **Future-event injection** (`test_future_event_does_not_change_the_snapshot`): add an event after the cutoff; the snapshot is unchanged.
2. **Late-arrival visibility** (`test_late_arrival_of_a_pre_cutoff_event_is_invisible_at_that_cutoff`, `test_late_arrival_becomes_visible_at_a_later_cutoff`): an event dated before the cutoff but ingested after it is excluded at that cutoff and included at a later one.
3. **Shuffle invariance** (`test_shuffled_event_order_gives_identical_features`): shuffling insertion order gives identical features.
4. **Ground-truth isolation** (`tests/ml/test_ground_truth_isolation.py`): runtime packages never reference ground truth or generators; only the offline modules mention it; the seed path never hashes ground-truth or defect files.
5. **Tenant scope** (`test_a_snapshot_for_another_organisations_account_is_not_found`): another organisation's account yields no snapshot.

Built in Part 2:

6. **Preprocessing fit scope** (`test_imputation_statistics_come_from_training_rows_only`): the imputer medians equal the training-row medians, not the full-set medians, for every numeric column where the two differ. Scope: the logistic imputer only. The boosting pipeline has no imputer (it accepts missing values natively), and the scaler and one-hot encoder are not separately tested for training-only fitting.
7. **Scoring path has no ground-truth reference** (`test_scoring_path_has_no_ground_truth_reference`): the runtime scoring modules contain no reference to `ground_truth`, `load_outcomes`, `app.ml.train`, or `defects`.

Still planned, not built:

8. **Denylist scan:** assert no feature name or source column references a banned field. Not built. The current check is the narrower test in item 7.

## 5. Models and selection

Candidates are fixed before training, so selection is not tuned on the test set.

| Candidate | Status | Notes |
|---|---|---|
| Prior (`DummyClassifier`, constant score) | Implemented; floor | Constant score equal to the training base rate. Every example gets the same score. |
| Logistic regression (L2, `C=1.0`, balanced class weights) | Implemented | Median imputation with missing indicators, standardised numerics, one-hot categoricals with infrequent-level grouping. Coefficients give direct contributions. |
| Gradient boosting (`HistGradientBoostingClassifier`, `max_depth=3`, `max_iter=100`, `learning_rate=0.05`, balanced class weights) | Implemented | A single fixed configuration, not tuned on any split. Added because the design allowed it; it was not expected to win on this data. |
| Rules baseline (weighted sum of three hand-chosen signals) | Not implemented | The weights are not defined anywhere in the repository, and the design requires them to be fixed before results are seen. Not added after the fact. |
| Oracle ceiling (generator's true logit as the score) | Not implemented | Needs ground truth in an evaluation path. Not added in Part 2. |

Selection rule (implemented in `app/ml/train.py`, recorded as `SELECTION_RULE` in the metadata):

1. A learned candidate (logistic, boosting) is eligible only if its validation PR-AUC difference from the prior is reliable (at least 30 churned terms in validation) **and** its 95% account-bootstrap confidence interval for that difference lies above zero.
2. Among eligible candidates, keep those whose validation PR-AUC is within 0.01 of the best eligible value.
3. Among those, choose the lowest validation Brier score. Ties go to logistic regression.
4. If no learned candidate is eligible, retain the prior. The result is then reported as "ranking not demonstrated".

The Brier step is uncalibrated. Calibration is deferred (section 5.1), so the Brier score ranks uncalibrated scores and is not a calibration measure. The original version of this section required calibration before the Brier step; that is not implemented.

Hyperparameter search: none. Each candidate has one fixed configuration, so no grid is searched on validation or test.

### 5.1 Calibration: deferred

Isotonic or Platt calibration was planned on a time-blocked slice inside training. It is deferred. Calibrating needs a held-out slice with enough churned terms to fit the calibrator and to check it. The training window has 15 churned terms, and validation has 2. A calibrator fitted on either would be unstable, and it would be fitted on the same data used to judge it. Model outputs are therefore uncalibrated scores, not probabilities. Documents and the dashboard must not describe `risk_score` as a probability until calibration is implemented and checked on enough data.

## 5.2 Selection audit trail

- The selection hash is computed after validation selection. It covers the candidate list, the rule text, the split boundaries, the validation metrics, the validation comparisons, and the chosen model name.
- Test metrics are computed after the hash is fixed, and each test row in `model_evaluations` stores the same hash.
- **Process disclosure.** In the first offline run, which used a selection rule without the validation-CI gate, the test metrics for all candidates were printed to the terminal before the gate was added. The gate was then added for a reason that came from validation alone: boosting led prior on validation PR-AUC by 0.03 with overlapping intervals (validation PR-AUC CIs: prior 0.025 to 0.193, boosting 0.044 to 0.289). The test numbers were visible at that point, and the gate was added after seeing them. The stated justification is validation-only, but the test numbers may have influenced the decision to add the gate, and this cannot be ruled out. The final rule is validation-based, but the test set was not completely untouched. The metrics in this document come from the final run. Treat the test-set results as an exploratory read, not as a clean held-out result.
- The original section 5 rule was described as fixed before training. It was replaced by the rule above after the first run, and that replacement is part of this disclosure.

## 6. Evaluation

### Metrics

| Metric | Why |
|---|---|
| PR-AUC | Primary ranking metric. Base churn rate is low, so ROC-AUC alone overstates performance. |
| ROC-AUC | Reported for comparability. |
| Brier score and reliability curve | Calibration quality. |
| Precision and recall at the top-k (k = 5%, 10%, 20% of scored accounts) | Matches CSM capacity: what fraction of churners are in the top list. |
| Lift at top 10% | Shown against random ordering. |
| Log loss | Secondary. |

Every metric is reported with a bootstrap 95% confidence interval, resampling accounts (not rows) to respect grouping. Interval width is part of the result; a point estimate without it is not reported.

### Implemented metrics

PR-AUC, ROC-AUC, Brier score, and top-5/10/20% precision and recall are implemented in `app/ml/evaluate.py`. Each has a 95% percentile bootstrap interval that resamples accounts. Reliability reporting is implemented; the reliability curve is not.

### Comparison set

- Prior (constant score) as the floor: implemented.
- Logistic regression and boosting: implemented, both compared with the prior on the same examples.
- Rules baseline: not implemented (section 5).
- Oracle ceiling: not implemented.

### Reliability threshold

A metric is reliable only when the evaluation set has at least 30 churned renewal terms (`MIN_CHURN_TERMS`). Counting rows is not enough, because rows from one term share a label. Below the threshold, PR-AUC, Brier, and top-10% are reported as suppressed, with a note. Every current validation and test set is below the threshold (validation 2 terms, test 1 term), so no metric or comparison from them is reliable.

### Model comparison

The difference between a learned candidate and the prior is computed on the same examples, with accounts resampled jointly for both models. The criterion for "beats the prior" is met only when the difference is reliable and its PR-AUC interval lies above zero. The Brier difference is reported and is not used as a criterion.

### Temporal test protocol

1. Selection uses validation only. The selection hash is computed after selection, and it covers the validation metrics, the validation comparisons, and the chosen model.
2. Test metrics are computed after the hash is fixed. Each test row in `model_evaluations` stores the hash.
3. Test results are written to `model_evaluations`, one row per organisation, and the artifact metadata keeps the full candidate set.
4. The test set was viewed once during the first offline run, before the validation gate was added (section 5.2). The test set is therefore not a clean held-out result.

### Slice analysis

Implemented for: organisation, segment, plan tier, region, and coverage band (0-1, 2-3, 4-5 sources). Each slice reports counts (examples, accounts, churned rows, churned terms), a reliability flag, and PR-AUC, Brier, and top-10% precision. Calibration error is not reported, because calibration is deferred. Unreliable slices have their metrics suppressed. In the final run, no slice on test is reliable.

## 7. Explainability

- **Logistic regression:** contribution of feature `j` = `coef_j * (x_j - x_ref_j)`, where `x_ref_j` is the training median (or mean for standardised features). Sum plus intercept equals the logit. The explain step stores the top five positive and top three negative contributions.
- **Gradient boosting:** tree-path attribution (SHAP TreeExplainer if the library is added, otherwise a documented approximation). Whichever method is used is stored in the model metadata. The dashboard does not show it yet, because the model metadata page is not built.
- Explanations are the stored contributions and feature values, formatted by templates from feature metadata. They are not generated by an LLM and not hardcoded per account.
- Contributions always show the feature value at cutoff and the reference value, so a CSM can check the claim against source events.

## 8. Fairness and risk of harm

- The model ranks accounts for attention, not for pricing, access, or termination. This limits harm, and the product must not be used for those decisions. Stated in the model card.
- Segment and region are features. Slice metrics are reported to show whether the model is systematically worse for some groups. A material gap is an issue to resolve before business use, not a hidden caveat.
- Coverage features can encode "we have less data about this account" as a churn signal. Sparse accounts may be ranked higher simply because the model has less evidence about them. The dashboard does not show a coverage figure yet. Account detail lists feature values grouped by source, but it does not compute a coverage score.

## 9. Model lifecycle

| Stage | Behaviour |
|---|---|
| Train | Offline, via `cli train`. Writes artifact (`artifacts/models/<version>/model.joblib`), `metrics.json`, and a `model_versions` row with status `candidate`. |
| Evaluate | Writes `model_evaluations`. |
| Register | Operator promotes one candidate to `registered` via CLI. Partial unique index allows one registered version. Previous registered version becomes `retired`. |
| Score | Worker loads only the `registered` version. Its SHA-256 is checked against `artifact_sha256` before use. |
| Retire | Old version is kept. Its predictions remain in `prediction_snapshots` and stay traceable. |

Artifacts are loaded only from `artifacts/`. `joblib` loading is restricted to files whose checksum matches the registry record, because `joblib` (like `pickle`) can execute code on load. This is a security control, not only an integrity check.

## 10. Reproducibility

- Training seed is recorded. Scikit-learn estimators receive an explicit `random_state`.
- Training data hash (over the example table) is recorded.
- Library versions are pinned in `pyproject.toml` and recorded in `metrics.json`.
- Retraining with the same data and seed produces the same coefficients within floating-point tolerance. A test asserts this.

## 11. Assumptions and open items

| Assumption | Impact if wrong | Resolution |
|---|---|---|
| 90-day horizon matches CSM action time | Wrong horizon changes labels and rankings | Approved 2026-10-29 as a working default. Business confirmation of CSM lead time (brief Q1 and Q2) still open |
| One shared model suits every organisation | Per-organisation quality may differ | Approved. Metrics are reported overall and per organisation, with small-sample flags. Equal performance is not assumed |
| 30-day cancellation grace is acceptable | Mislabels early cancellations | Confirm |
| Top 5 to 20% capacity | Changes which metrics matter most | Confirm |
| Synthetic churn drivers are plausible | Metrics reflect the generator, not reality | Stated in model card |
| Model requirements (business) | Specific accuracy or fairness thresholds may apply | To be supplied after this design review |

## 12. Data validity check before any ML code (Phase 2 result, re-run at canonical scale)

The earlier check used 5,000 accounts and 200,000 events. That dataset has been removed, and the canonical dataset is now 50 accounts and 1,000 emitted records (`fixtures/canonical/`, seed 20261001). The check was re-run at this scale with the same rules: weekly cutoffs, only labelled (non-censored) renewals with the 90-day horizon, and the windows in section 2. It used generator ground truth, which ingestion and features never read.

Labelled examples (one row per cutoff and renewal within 90 days), and distinct accounts with a renewal in each window:

| Organisation | Window | Examples | Churn examples | Accounts | Churned accounts |
|---|---|---|---|---|---|
| acme-saas | train | 578 | 113 | 20 | 9 |
| acme-saas | validation | 153 | 3 | 10 | 1 |
| acme-saas | test | 81 | 0 | 5 | 0 |
| northwind-cloud | train | 429 | 65 | 12 | 5 |
| northwind-cloud | validation | 166 | 12 | 7 | 1 |
| northwind-cloud | test | 84 | 13 | 2 | 1 |
| globex-data | train | 117 | 26 | 6 | 2 |
| globex-data | validation | 84 | 0 | 5 | 0 |
| globex-data | test | 45 | 0 | 5 | 0 |
| **Pooled** | train | 1,124 | 204 (18.1%) | 38 | 16 |
| **Pooled** | validation | 403 | 15 (3.7%) | 22 | 2 |
| **Pooled** | test | 210 | 13 (6.2%) | 12 | 1 |

Findings:

- The data is not large enough for a meaningful temporal evaluation. The test window has 210 labelled examples from 12 accounts, and one churned account. Any AUC, precision or recall on that window is driven by one or two accounts and carries no useful confidence interval.
- The base rate is not stable across windows. It falls from 18.1% in train to 3.7% in validation and 6.2% in test. Section 12 of the earlier version said rates were stable, which was true only at 5,000 accounts. The shift here is partly sampling noise, but a model trained on train would face a very different base rate at evaluation time, so calibration will not transfer without a check.
- Every organisation has churners in train, but globex-data has none in validation or test, and acme-saas has none in test. A per-organisation evaluation is not possible for those windows.
- Only one biennial renewal exists in the dataset, so per-term evaluation is meaningless.
- The pooled churn rate over all observable renewals is 12.9% (manifest `pooled_churn_rate`), near the lower edge of the configured 12% to 22% range. Other seeds can fall below that floor at this size; the generator validator rejects them.
- The latent-health ceiling AUC that the earlier version reported was not recomputed here. It needs the same ground-truth join and should be re-run before any claim about model headroom.

Conclusion: the pipeline is exercised end to end at this scale, but the dataset cannot support statistically meaningful model evaluation or model comparison. Phase 3 must not report strong model performance from it. Use this dataset for integration, schema and pipeline tests, and for demonstrating the workflow. Any performance claim needs a larger canonical dataset, which would be a deliberate config change, not a quiet edit.

Limits of this check: it is computed on synthetic data from the generator's own latent variable, so it describes the generator, not real customers, and it was run as a scratch analysis, not as a committed test.
