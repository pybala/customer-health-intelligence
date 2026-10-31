"""Feature definitions. Each feature reads only normalised source events visible at the cutoff.

The account's `renewal_date` is not a feature. The account record holds only its current value,
so using it at a past cutoff would leak the renewal that the cutoff is meant to predict. It
selects the scoring window and supplies the offline label date instead (docs/ml-design.md
section 3). `identity_ambiguous_flag` is not built: ambiguous events are never attached to an
account, so it has no per-account value.
"""

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.ingestion.pipeline import LATE_AFTER

FEATURE_SET_VERSION = "features-v1"

FeatureValue = int | float | str | None

STAGE_RANK = {
    "Prospecting": 0,
    "Qualification": 1,
    "Proposal": 2,
    "Negotiation": 3,
    "Closed Won": 4,
}

GMAIL_TYPES = frozenset({"thread_sent", "thread_received", "meeting_invite"})
THREAD_TYPES = frozenset({"thread_sent", "thread_received"})


@dataclass(frozen=True, slots=True)
class VisibleEvent:
    id: str
    source: str
    source_record_id: str
    event_type: str
    event_time: datetime
    ingested_at: datetime
    attributes: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AccountProfile:
    account_id: str
    segment: str
    plan_tier: str
    region: str
    created_at: datetime


Aggregate = Callable[[Sequence[VisibleEvent], datetime], FeatureValue]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    source: str | None
    event_types: frozenset[str] | None
    window_days: int | None
    aggregate: Aggregate
    how: str


def _count(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    return len(events)


def _distinct_attr(key: str) -> Aggregate:
    def aggregate(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
        return len({e.attributes[key] for e in events if e.attributes.get(key) is not None})

    return aggregate


def _sum_attr(key: str) -> Aggregate:
    def aggregate(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
        return sum(float(e.attributes[key]) for e in events if e.attributes.get(key) is not None)

    return aggregate


def _median_attr(key: str) -> Aggregate:
    def aggregate(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
        values = [float(e.attributes[key]) for e in events if e.attributes.get(key) is not None]
        return statistics.median(values) if values else None

    return aggregate


def _mean_attr(key: str) -> Aggregate:
    def aggregate(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
        values = [float(e.attributes[key]) for e in events if e.attributes.get(key) is not None]
        return statistics.fmean(values) if values else None

    return aggregate


def _any_true_attr(key: str) -> Aggregate:
    def aggregate(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
        return int(any(bool(e.attributes.get(key)) for e in events))

    return aggregate


def _days_since_latest(events: Sequence[VisibleEvent], cutoff: datetime) -> FeatureValue:
    if not events:
        return None
    return (cutoff - max(e.event_time for e in events)).total_seconds() / 86400


def _outbound_ratio(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    if not events:
        return None
    return sum(1 for e in events if e.event_type == "thread_sent") / len(events)


def _active_users_trend(events: Sequence[VisibleEvent], cutoff: datetime) -> FeatureValue:
    split = cutoff - timedelta(days=60)
    recent = {e.attributes.get("user") for e in events if e.event_time > split}
    prior = {e.attributes.get("user") for e in events if e.event_time <= split}
    recent.discard(None)
    prior.discard(None)
    if not prior:
        return None
    return len(recent) / len(prior)


def _forecast_delta(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    ordered = sorted(events, key=lambda e: (e.event_time, e.id))
    values = [
        float(e.attributes["forecast_pct"]) for e in ordered if "forecast_pct" in e.attributes
    ]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def _stage_regressions(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    ordered = sorted(events, key=lambda e: (e.event_time, e.id))
    ranks = [
        STAGE_RANK[e.attributes["stage"]]
        for e in ordered
        if e.attributes.get("stage") in STAGE_RANK
    ]
    return sum(1 for before, after in zip(ranks, ranks[1:], strict=False) if after < before)


def _late_share(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    if not events:
        return None
    return sum(1 for e in events if e.ingested_at - e.event_time > LATE_AFTER) / len(events)


def _distinct_sources(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    return len({e.source for e in events})


def _has_any(events: Sequence[VisibleEvent], _cutoff: datetime) -> FeatureValue:
    return int(bool(events))


EVENT_FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec("product_events_30d", "product", None, 30, _count, "count of events"),
    FeatureSpec(
        "active_users_30d", "product", None, 30, _distinct_attr("user"), "distinct user_id"
    ),
    FeatureSpec(
        "active_users_trend_60v60",
        "product",
        None,
        120,
        _active_users_trend,
        "distinct users in last 60 days / distinct users in the 60 days before",
    ),
    FeatureSpec(
        "days_since_last_login",
        "product",
        frozenset({"login"}),
        None,
        _days_since_latest,
        "cutoff minus latest visible login, in days",
    ),
    FeatureSpec(
        "api_calls_30d",
        "product",
        frozenset({"api_call_batch"}),
        30,
        _sum_attr("calls"),
        "sum of properties.calls",
    ),
    FeatureSpec(
        "seat_change_90d",
        "product",
        frozenset({"seat_added"}),
        90,
        _sum_attr("seats_delta"),
        "sum of properties.seats_delta",
    ),
    FeatureSpec("tickets_90d", "zendesk", frozenset({"ticket_opened"}), 90, _count, "count"),
    FeatureSpec("escalations_90d", "zendesk", frozenset({"escalation"}), 90, _count, "count"),
    FeatureSpec(
        "median_resolution_hours_90d",
        "zendesk",
        frozenset({"ticket_solved"}),
        90,
        _median_attr("resolution_hours"),
        "median of resolution_hours",
    ),
    FeatureSpec(
        "csat_90d_mean",
        "zendesk",
        frozenset({"csat_response"}),
        90,
        _mean_attr("csat"),
        "mean of csat_score",
    ),
    FeatureSpec(
        "threads_30d", "gmail", THREAD_TYPES, 30, _distinct_attr("thread"), "distinct thread_id"
    ),
    FeatureSpec(
        "outbound_ratio_90d",
        "gmail",
        THREAD_TYPES,
        90,
        _outbound_ratio,
        "thread_sent / (thread_sent + thread_received)",
    ),
    FeatureSpec(
        "days_since_last_contact",
        "gmail",
        GMAIL_TYPES,
        None,
        _days_since_latest,
        "cutoff minus latest visible Gmail event, in days",
    ),
    FeatureSpec(
        "exec_contact_recent_30d",
        "gmail",
        GMAIL_TYPES,
        30,
        _any_true_attr("exec_contact"),
        "1 if any event has an executive contact",
    ),
    FeatureSpec(
        "stage_changes_180d",
        "salesforce",
        frozenset({"opportunity_stage_change"}),
        180,
        _count,
        "count",
    ),
    FeatureSpec(
        "stage_regressions_180d",
        "salesforce",
        frozenset({"opportunity_stage_change"}),
        180,
        _stage_regressions,
        "count of stage moves to a lower rank, ordered by event_time then id",
    ),
    FeatureSpec(
        "renewal_forecast_delta_90d",
        "salesforce",
        frozenset({"renewal_forecast_update"}),
        90,
        _forecast_delta,
        "last ForecastPct minus first, ordered by event_time then id",
    ),
    FeatureSpec(
        "failed_payments_60d", "stripe", frozenset({"invoice_failed"}), 60, _count, "count"
    ),
    FeatureSpec(
        "days_since_last_payment",
        "stripe",
        frozenset({"invoice_paid"}),
        None,
        _days_since_latest,
        "cutoff minus latest visible paid invoice, in days",
    ),
    FeatureSpec(
        "late_arrival_share_90d",
        None,
        None,
        90,
        _late_share,
        "share of events whose ingested_at is more than LATE_AFTER after event_time",
    ),
    FeatureSpec(
        "coverage_source_count_90d",
        None,
        None,
        90,
        _distinct_sources,
        "number of distinct sources with a visible event",
    ),
    FeatureSpec("has_salesforce_90d", "salesforce", None, 90, _has_any, "1 if any event"),
    FeatureSpec("has_gmail_90d", "gmail", None, 90, _has_any, "1 if any event"),
    FeatureSpec("has_zendesk_90d", "zendesk", None, 90, _has_any, "1 if any event"),
    FeatureSpec("has_product_90d", "product", None, 90, _has_any, "1 if any event"),
    FeatureSpec("has_stripe_90d", "stripe", None, 90, _has_any, "1 if any event"),
)

PROFILE_FEATURES: tuple[str, ...] = ("segment", "plan_tier", "region", "tenure_months")


def feature_names() -> tuple[str, ...]:
    return (*(spec.name for spec in EVENT_FEATURES), *PROFILE_FEATURES)
