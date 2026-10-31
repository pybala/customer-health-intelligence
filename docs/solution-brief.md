# Solution Brief: Customer Health Intelligence Platform

## Business problem

Customer Success Managers (CSMs) decide where to spend renewal effort based on signals that live in five disconnected systems: CRM (Salesforce), email (Gmail), support tickets (Zendesk), product usage events, and billing (Stripe). No single view ranks accounts by likelihood of churn, and no view explains why. Accounts are usually discovered as at-risk after a missed renewal, when recovery is hardest.

## Proposed solution

A multi-tenant platform that:

1. Ingests events from the five sources idempotently, normalises them, and resolves identities to a canonical account.
2. Builds point-in-time features for each account as of a scoring cutoff, so no feature uses information from after that cutoff.
3. Scores each account with a supervised churn model (scikit-learn, versioned, evaluated on a temporally held-out test set).
4. Persists every prediction as an immutable snapshot linked to model version, feature values, contributing signals, and source-event references.
5. Shows CSMs a ranked dashboard with per-prediction evidence.
6. Runs a deterministic scheduled worker that ingests, computes features, scores, and produces recommended actions.
7. Routes outreach through a policy gate (suppression, deduplication, dry-run default, retry limits) into a mock email adapter. Nothing is sent to real customers.

## What we deliberately reject

| Original-request element | Decision | Reason |
|---|---|---|
| Autonomous LLM agent for scheduling or outreach | Reject | Non-deterministic, hard to audit, and unnecessary. A deterministic worker meets the requirement. |
| LLM-generated explanations | Reject | Explanations come from model contributions and rules, so they are reproducible and never hardcoded. |
| Real email sending by default | Reject | Safety requirement. Mock adapter only in this build. |
| Real-time streaming (Kafka, CDC) | Reject | Daily or hourly batch is enough for CSM workflows. Adds infrastructure with no demonstrable value here. |
| Microservices | Reject | Two containers (worker, API+dashboard) plus PostgreSQL. Module boundaries give the same separation at a fraction of the operational cost. |
| Overwriting predictions in place | Reject | Breaks auditability. Predictions are append-only snapshots. |
| Tenant filtering only in application code | Reject | Insufficient. PostgreSQL RLS is added as defence in depth. |
| Trusting `org_id` from request parameters | Reject | Tenant identity is derived from the authenticated API key. |
| Cloud deployment, managed services | Reject for now | Local Docker Compose only, no cloud credentials required. |
| Hardcoded dashboard metrics or explanations | Reject | All values are computed from stored data. |

## Assumptions (demonstration defaults)

- **Churn definition:** an account churns at a scheduled renewal date if it does not renew, or cancels effective within 30 days after that date. Scores are produced at a cutoff and predict churn at the next renewal falling within the next 90 days (the prediction horizon). Renewals whose outcome is not yet observable at the evaluation date are excluded.
- **Accounts:** exactly 50 synthetic accounts across 3 demo organisations (24 / 16 / 10), with uneven size distribution. One canonical dataset serves demo, seeding, ML, and tests.
- **History:** 3 years (synthetic), with the final 6 months reserved as the temporal test window.
- **Events:** exactly 1,000 emitted source records across the five sources (Salesforce 150, Gmail 150, Zendesk 150, product usage 450, Stripe 100), with a fixed seed for reproducibility.
- **Scoring cadence:** the worker runs daily in the demo (cron-style schedule, configurable).
- **Model:** logistic regression baseline and gradient boosting candidate. The selection rule is decided before training, not after seeing test results.
- **Auth:** API keys per organisation, hashed at rest. No SSO in this build.
- **Email:** a local mock SMTP-style capture service or in-process adapter. No external provider.
- **Data realism:** missing records, duplicates, late and out-of-order arrivals, identity mismatches (e.g. different email domains or account names for the same account), and uneven source coverage are injected at documented rates.

## Questions for the business

1. What is the real renewal cadence (annual, monthly, mixed), and how is "churn" defined for multi-product contracts?
2. What is the acceptable lag between an event happening and it appearing in the score (hours, daily)?
3. Which actions are permitted for CSMs, and who approves outreach? Is human approval required before any customer email?
4. What are the suppression rules (e.g. legal hold, active escalation, recent contact within N days)?
5. Is there a threshold for "high risk" that matches the CSM team's capacity, or should the platform rank only?
6. What data retention and PII rules apply to email bodies and support ticket text? (This build stores metadata and hashes only, not message bodies.)
7. Which fairness or segment-level performance checks does the business require (e.g. by segment, region, or plan tier)?

## Success measures for the demonstration

- Temporal test-set metrics reported with confidence intervals, compared against a simple rules baseline.
- Zero cross-tenant reads in the isolation test suite, including attempts through the API and direct SQL under the application role.
- Re-running ingestion with the same input produces zero duplicate events and zero duplicate actions.
- Every displayed prediction can be traced to its model version, feature values, and source event references.
