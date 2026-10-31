"""Evidence-based RYG status. A deterministic demo policy, not calibrated, not from risk_score.

Inputs are the visible feature values for one eligible prediction plus the account's renewal
distance. Each reason names the feature, the observed value and the threshold, so a status can
be traced to the source events cited in the feature provenance. Thresholds are provisional
demo values. They were set from the feature definitions, not tuned against any test split.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.features.definitions import FeatureValue

POLICY_VERSION = "demo-rag-v1"

MIN_SOURCES = 3
MAX_LATE_SHARE = 0.5


class Status(StrEnum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    feature: str
    observed: FeatureValue
    threshold: float
    severity: Status


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    status: Status
    reasons: tuple[Reason, ...]
    policy_version: str = POLICY_VERSION

    @property
    def critical(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.severity is Status.RED)


def _insufficiency(values: dict[str, FeatureValue]) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    sources = values.get("coverage_source_count_90d")
    if not isinstance(sources, int | float) or sources < MIN_SOURCES:
        reasons.append(
            Reason(
                "too_few_sources",
                "coverage_source_count_90d",
                sources,
                MIN_SOURCES,
                Status.INSUFFICIENT_DATA,
            )
        )
    if values.get("has_product_90d") != 1:
        reasons.append(
            Reason(
                "no_product_usage",
                "has_product_90d",
                values.get("has_product_90d"),
                1,
                Status.INSUFFICIENT_DATA,
            )
        )
    late = values.get("late_arrival_share_90d")
    if isinstance(late, int | float) and late > MAX_LATE_SHARE:
        reasons.append(
            Reason(
                "data_arriving_late",
                "late_arrival_share_90d",
                late,
                MAX_LATE_SHARE,
                Status.INSUFFICIENT_DATA,
            )
        )
    return tuple(reasons)


def _critical(values: dict[str, FeatureValue], days_to_renewal: int) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    failed = values.get("failed_payments_60d")
    if isinstance(failed, int | float) and failed >= 2:
        reasons.append(
            Reason("repeated_payment_failure", "failed_payments_60d", failed, 2, Status.RED)
        )
    escalations = values.get("escalations_90d")
    if isinstance(escalations, int | float) and escalations >= 2 and days_to_renewal <= 60:
        reasons.append(
            Reason("escalations_near_renewal", "escalations_90d", escalations, 2, Status.RED)
        )
    trend = values.get("active_users_trend_60v60")
    silence = values.get("days_since_last_login")
    if (
        isinstance(trend, int | float)
        and trend < 0.5
        and isinstance(silence, int | float)
        and silence >= 30
    ):
        reasons.append(Reason("usage_collapse", "active_users_trend_60v60", trend, 0.5, Status.RED))
    return tuple(reasons)


def _warning(values: dict[str, FeatureValue], days_to_renewal: int) -> tuple[Reason, ...]:
    reasons: list[Reason] = []
    failed = values.get("failed_payments_60d")
    if isinstance(failed, int | float) and failed == 1:
        reasons.append(Reason("payment_failure", "failed_payments_60d", failed, 1, Status.YELLOW))
    escalations = values.get("escalations_90d")
    if isinstance(escalations, int | float) and escalations >= 1:
        reasons.append(Reason("escalation", "escalations_90d", escalations, 1, Status.YELLOW))
    trend = values.get("active_users_trend_60v60")
    if isinstance(trend, int | float) and trend < 0.8:
        reasons.append(
            Reason("usage_decline", "active_users_trend_60v60", trend, 0.8, Status.YELLOW)
        )
    regressions = values.get("stage_regressions_180d")
    if isinstance(regressions, int | float) and regressions >= 1:
        reasons.append(
            Reason("pipeline_regression", "stage_regressions_180d", regressions, 1, Status.YELLOW)
        )
    forecast = values.get("renewal_forecast_delta_90d")
    if isinstance(forecast, int | float) and forecast <= -10:
        reasons.append(
            Reason("forecast_drop", "renewal_forecast_delta_90d", forecast, -10, Status.YELLOW)
        )
    csat = values.get("csat_90d_mean")
    if isinstance(csat, int | float) and csat < 3.0:
        reasons.append(Reason("low_csat", "csat_90d_mean", csat, 3.0, Status.YELLOW))
    contact = values.get("days_since_last_contact")
    if isinstance(contact, int | float) and contact > 45:
        reasons.append(
            Reason("no_recent_contact", "days_since_last_contact", contact, 45, Status.YELLOW)
        )
    return tuple(reasons)


def evaluate(values: dict[str, FeatureValue], days_to_renewal: int) -> PolicyDecision:
    insufficient = _insufficiency(values)
    if insufficient:
        return PolicyDecision(Status.INSUFFICIENT_DATA, insufficient)
    critical = _critical(values, days_to_renewal)
    if critical:
        return PolicyDecision(Status.RED, critical)
    warning = _warning(values, days_to_renewal)
    if warning:
        return PolicyDecision(Status.YELLOW, warning)
    return PolicyDecision(Status.GREEN, ())
