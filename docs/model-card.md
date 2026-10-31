# Model Card: Churn Risk Prior (logreg-v1 run, selected candidate: prior)

Written for: engineers and reviewers evaluating this demo. Not for business decisions.

## Status

**The model that was selected is the constant prior.** Every account receives the same score, the training base rate. It ranks no accounts. The learned candidates (logistic regression and gradient boosting) were trained and evaluated, but neither passed the validation gate, so neither was selected.

This card does not describe a working churn-risk model. No result here shows that accounts can be ranked by risk. This is a demonstration of the training, evaluation, and persistence workflow on a synthetic dataset of 50 accounts.

## Intended use

- Demonstrating an offline ML workflow: temporal split, leakage controls, bootstrap evaluation, selection with a pre-registered rule, artifact integrity, registry persistence, and tenant-isolated prediction storage.
- Pipeline and integration testing.

## Out of scope and inappropriate uses

- Ranking accounts for CSM attention. The selected prior cannot rank. A learned model would need the evidence described under limitations before any ranking claim.
- Any decision about pricing, access, termination, or individual customers.
- Reporting `risk_score` as a probability. Calibration is deferred. The prior's score is the base rate; no output is calibrated.
- Reporting any metric from this dataset as evidence of production performance.

## Training data and label

- Source: canonical synthetic dataset, `fixtures/canonical/` (50 accounts, 1,000 emitted events, seed 20261001, three organisations).
- Label: `churn = 1` if the account does not renew at a scheduled renewal, or cancels effective within 30 days after it. Only renewals whose 30-day label date falls on or before the data end are used. Nine of 149 renewal terms are censored and excluded. The label comes from `ground_truth.ndjson`, which only the offline workflow reads.
- Examples: one per `(account, weekly cutoff)` where the renewal falls in the 90-day horizon. 1,747 examples come from 138 renewal terms. Each term yields 3 to 13 examples, one per weekly cutoff in its horizon. Full detail is in `ml-design.md` section 2.1.

## Features

Features come from the point-in-time builder (`features-v1`), using only events visible at each cutoff (`event_time <= cutoff` and `ingested_at <= cutoff`). Families: product engagement, seat dynamics, support, communication, CRM, billing, account profile, coverage, and data quality. The renewal outcome and the renewal date are not features. Organisation identity is not a feature.

## Temporal split

| Split | Renewals | Examples | Accounts | Renewal terms | Churned terms |
|---|---|---|---|---|---|
| Train | before 2025-09-30 | 1,026 | 34 | 82 | 15 |
| Validation | 2025-09-30 to 2026-04-01 | 386 | 17 | 30 | 2 |
| Test | from 2026-04-01 | 335 | 12 | 26 | 1 |

No renewal term crosses splits. 11 accounts appear in both train and validation, 8 in train and test, and 5 in validation and test. Horizons of 108 examples straddle the training boundary. Implications for independence and leakage are in `ml-design.md` section 2.2.

## Candidates and selection

| Candidate | Configuration |
|---|---|
| Prior | constant score (`DummyClassifier`, `strategy="prior"`) |
| Logistic regression | L2, `C=1.0`, balanced class weights, median imputation with indicators, standardised numerics, one-hot categoricals |
| Gradient boosting | `HistGradientBoostingClassifier`, `max_depth=3`, `max_iter=100`, `learning_rate=0.05`, balanced class weights |

Selection rule (validation only, recorded in metadata as `SELECTION_RULE`):

1. A learned candidate is eligible only if its validation PR-AUC difference from the prior is reliable (at least 30 churned terms) and its 95% account-bootstrap interval lies above zero.
2. Among eligible candidates, keep those within 0.01 PR-AUC of the best.
3. Choose the lowest validation Brier score among them. Ties go to logistic regression.
4. If no learned candidate is eligible, keep the prior.

Outcome in the final run: **no learned candidate was eligible, so the prior was retained.**

| Candidate | Validation PR-AUC | 95% CI | Difference from prior | Difference CI | Criterion |
|---|---|---|---|---|---|
| Prior | 0.067 | 0.025 to 0.193 | | | |
| Logistic | 0.067 | 0.027 to 0.190 | -0.001 | -0.056 to 0.069 | unmet |
| Boosting | 0.097 | 0.044 to 0.289 | +0.029 | -0.022 to 0.173 | unmet |

Boosting has a higher point estimate, but its interval includes zero and the validation set has 2 churned terms. Choosing it would be a choice on noise.

## Evaluation results

The test set had 1 churned term. Every metric on it is unreliable, and the reliability flag is `false` for every row and slice. The values are reported for transparency, not as evidence.

| Candidate | Test PR-AUC (95% CI) | Test Brier (95% CI) | Test ROC-AUC (95% CI) |
|---|---|---|---|
| Prior | 0.039 (0.028 to 0.109) | 0.060 (0.036 to 0.101) | 0.500 (0.500 to 0.500; constant score, so 0.5 by construction) |
| Logistic | 0.276 (0.173 to 0.473) | 0.276 (0.094 to 0.449) | 0.944 (0.905 to 0.977) |
| Boosting | 0.187 (0.127 to 0.388) | 0.115 (0.045 to 0.212) | 0.902 (0.777 to 0.960) |

Test PR-AUC differences from prior: logistic +0.237 (interval 0.144 to 0.431), boosting +0.148 (0.079 to 0.323). These intervals exclude zero, but the test set has 1 churned term, so the comparison is unreliable and the criterion is reported as unmet. The result was seen after the selection was fixed, so it cannot change the selected model. It also could not be trusted on its own (section below).

Top-10% precision and recall on test are shown in the artifact metadata. They rest on 13 positive example rows from one churned term and are not interpretable.

Slices: organisation, segment, plan tier, region, and coverage band are computed for each candidate. On test, no slice meets the reliability threshold, so no slice metric is shown.

## Calibration

Deferred. Fitting a calibrator needs more churned terms than the training window provides for a held-out slice (15 in train, 2 in validation). The outputs are uncalibrated scores. The Brier score in this card is therefore not a calibration measure. Do not read `risk_score` as a probability.

## Ranking

**Account ranking is not demonstrated.** The selected prior gives every account the same score, so it has no ranking ability, and its identical scores must not be presented as differentiation. The learned candidates did not pass the validation gate, so no ranking claim is made for them either. Their test results show a large point estimate, but that is one churned term.

## Limitations and known biases

- **Sample size.** 50 accounts, 1,000 events, and 15, 2, and 1 churned terms across the three windows. No window supports a reliable metric or model comparison.
- **Validation windows.** Validation has 2 churned terms, so the gate is seldom able to pass. The gate is a safeguard. It does not solve the small-sample problem.
- **Process disclosure.** In the first offline run, test metrics for all candidates were printed before the validation gate was added. The gate's justification is validation-based, but the test numbers were visible when it was added. The test set was therefore not completely untouched. The results above are an exploratory read.
- **Synthetic outcome.** The generator defines churn from a latent health variable, so the model learns the generator's structure, not real churn drivers.
- **Base rate drift.** The churned share of examples falls from 19.0% in train to 6.7% in validation and 3.9% in test. A model's scores would not transfer across windows without a check.
- **Dependence.** Examples from one renewal term share a label, and accounts recur across splits. Effective sample size is closer to the number of terms than the number of rows.
- **Coverage.** Coverage features can make sparse accounts look riskier because the model has less evidence about them. The slice report exists to expose this, but it cannot test it on this data.
- **Segment and region.** These are features, and their slice metrics cannot be assessed on this data.
- **Missing rules baseline and oracle ceiling.** Neither is implemented, so the value of the learned candidates over a simple rule, and their headroom against the generator's own ceiling, are unknown.
- **Two excluded 2023 renewals.** Both are on one northwind account. Their look-back falls before the training window, so they produce no examples.

## Artifact and reproducibility

- Artifact: no artifact is committed to the repository, because `artifacts/models/` is git-ignored. A local artifact exists at `artifacts/models/logreg-v1/` (`model.joblib` and `metadata.json`), written on 2026-10-30, and its `selected` field is `prior`. Its file digest matches `artifact_sha256` in `metadata.json`. Whether it is registered in the database has not been checked. An earlier dev-workflow artifact at the same path was deleted because it was stale (it predated the validation gate). Commands to train, register, and score are in `docs/dev-services.md`. Loading verifies the SHA-256 against the registry record before `joblib.load`. A tampered file is refused (`test_tampered_artifact_is_refused_before_loading`).
- The selected model in the artifact is the prior. The model version name `logreg-v1` is historical and does not describe the selected model.
- Dataset fixture hash is recorded as `dataset_fixture_sha256` (SHA-256 of `manifest.json`).
- Library versions are recorded in the metadata. The training seed is fixed (`random_state=0` for learned candidates; the prior is deterministic).
- The metrics in this card come from `metadata.json`, written by one offline run. They were transcribed into this card by hand, not generated from the file. The model card is therefore not generated, and the regenerate-and-diff check does not exist.

## Caveats for future readers

- Do not use this card to justify a model claim. The honest result is that the workflow works and the data cannot show whether any model ranks accounts.
- To make a ranking claim, the dataset needs many more churned terms in validation and test, or an external evaluation. Both are outside this demo's scope.
