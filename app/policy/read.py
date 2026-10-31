"""Read-time RYG for one stored prediction. Nothing is persisted.

The status is recomputed from the prediction's own feature snapshot and renewal distance, so it
always reflects the values that were scored. A prediction with no snapshot (ineligible) has no
RYG status.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.features.definitions import FeatureValue
from app.policy.health import PolicyDecision, evaluate


@dataclass(frozen=True, slots=True)
class RygView:
    status: str
    policy_version: str
    cutoff_at: datetime
    reasons: list[dict[str, Any]]


def ryg_for_prediction(
    feature_values: dict[str, FeatureValue] | None,
    renewal_date: datetime | None,
    cutoff_at: datetime,
    eligible: bool,
) -> RygView | None:
    if not eligible or feature_values is None or renewal_date is None:
        return None
    days = (renewal_date - cutoff_at).days
    decision: PolicyDecision = evaluate(feature_values, days)
    return RygView(
        status=decision.status.value,
        policy_version=decision.policy_version,
        cutoff_at=cutoff_at,
        reasons=[
            {
                "code": r.code,
                "feature": r.feature,
                "observed": r.observed,
                "threshold": r.threshold,
                "severity": r.severity.value,
            }
            for r in decision.reasons
        ],
    )
