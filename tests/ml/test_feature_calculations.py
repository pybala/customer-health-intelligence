"""Pure feature calculations on hand-built visible events. No database."""

from datetime import UTC, datetime, timedelta

import pytest

from app.features.builder import compute_features, latest_visible_version
from app.features.definitions import AccountProfile, VisibleEvent

CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)
PROFILE = AccountProfile(
    account_id="a1",
    segment="mid_market",
    plan_tier="business",
    region="emea",
    created_at=CUTOFF - timedelta(days=365),
)


def _event(
    eid: str,
    source: str,
    event_type: str,
    days_before: float,
    attributes: dict[str, object] | None = None,
    ingested_days_before: float | None = None,
    record: str | None = None,
) -> VisibleEvent:
    event_time = CUTOFF - timedelta(days=days_before)
    ingested = (
        event_time
        if ingested_days_before is None
        else CUTOFF - timedelta(days=ingested_days_before)
    )
    return VisibleEvent(
        id=eid,
        source=source,
        source_record_id=record or eid,
        event_type=event_type,
        event_time=event_time,
        ingested_at=ingested,
        attributes=attributes or {},
    )


def _value(events: list[VisibleEvent], name: str) -> object:
    values, _ = compute_features(events, PROFILE, CUTOFF)
    return values[name]


def test_count_respects_the_30_day_window() -> None:
    events = [
        _event("in1", "product", "login", 5, {"user": "u1"}),
        _event("in2", "product", "login", 29, {"user": "u2"}),
        _event("out", "product", "login", 31, {"user": "u3"}),
    ]

    assert _value(events, "product_events_30d") == 2


def test_window_boundary_is_exclusive_at_the_start() -> None:
    events = [_event("edge", "product", "login", 30, {"user": "u1"})]

    assert _value(events, "product_events_30d") == 0


def test_days_since_last_login_uses_the_latest_visible_login() -> None:
    events = [
        _event("old", "product", "login", 40, {"user": "u1"}),
        _event("new", "product", "login", 3, {"user": "u1"}),
        _event("not_login", "product", "feature_used", 1, {"user": "u1"}),
    ]

    assert _value(events, "days_since_last_login") == pytest.approx(3.0)


def test_missing_data_gives_none_not_zero_for_undefined_ratios() -> None:
    assert _value([], "days_since_last_login") is None
    assert _value([], "outbound_ratio_90d") is None
    assert _value([], "csat_90d_mean") is None
    assert _value([], "median_resolution_hours_90d") is None
    assert _value([], "active_users_trend_60v60") is None


def test_missing_data_gives_zero_for_counts_and_presence_flags() -> None:
    assert _value([], "tickets_90d") == 0
    assert _value([], "has_zendesk_90d") == 0
    assert _value([], "coverage_source_count_90d") == 0


def test_attribute_missing_from_an_event_is_skipped_not_zeroed() -> None:
    events = [
        _event("a", "zendesk", "csat_response", 10, {"csat": 4}),
        _event("b", "zendesk", "csat_response", 11, {}),
    ]

    assert _value(events, "csat_90d_mean") == pytest.approx(4.0)


def test_outbound_ratio_counts_sent_over_all_threads() -> None:
    events = [
        _event("s1", "gmail", "thread_sent", 1),
        _event("s2", "gmail", "thread_sent", 2),
        _event("r1", "gmail", "thread_received", 3),
        _event("m1", "gmail", "meeting_invite", 4),
    ]

    assert _value(events, "outbound_ratio_90d") == pytest.approx(2 / 3)


def test_stage_regressions_count_backward_moves_in_time_order() -> None:
    stages = ["Qualification", "Proposal", "Negotiation", "Proposal"]
    events = [
        _event(f"s{i}", "salesforce", "opportunity_stage_change", 100 - i * 10, {"stage": stage})
        for i, stage in enumerate(stages)
    ]

    assert _value(events, "stage_regressions_180d") == 1


def test_forecast_delta_is_last_minus_first_by_time() -> None:
    events = [
        _event("late", "salesforce", "renewal_forecast_update", 1, {"forecast_pct": 40}),
        _event("early", "salesforce", "renewal_forecast_update", 50, {"forecast_pct": 70}),
    ]

    assert _value(events, "renewal_forecast_delta_90d") == pytest.approx(-30.0)


def test_forecast_delta_needs_two_points() -> None:
    events = [_event("only", "salesforce", "renewal_forecast_update", 5, {"forecast_pct": 60})]

    assert _value(events, "renewal_forecast_delta_90d") is None


def test_late_share_uses_ingestion_lag_over_three_days() -> None:
    events = [
        _event("on_time", "product", "login", 10, {"user": "u"}, ingested_days_before=10),
        _event("late", "product", "login", 10, {"user": "u"}, ingested_days_before=5),
    ]

    assert _value(events, "late_arrival_share_90d") == pytest.approx(0.5)


def test_profile_tenure_is_months_since_creation_at_cutoff() -> None:
    values, _ = compute_features([], PROFILE, CUTOFF)

    assert values["tenure_months"] == pytest.approx(365 / 30.4375, abs=0.01)
    assert values["segment"] == "mid_market"


def test_every_feature_has_provenance_that_names_its_supporting_events() -> None:
    events = [_event("p1", "product", "login", 5, {"user": "u1"})]

    _, provenance = compute_features(events, PROFILE, CUTOFF)

    assert provenance["days_since_last_login"].event_ids == ("p1",)
    assert provenance["days_since_last_login"].source == "product"
    assert provenance["tickets_90d"].event_ids == ()
    assert provenance["segment"].source == "accounts"


def test_revised_record_keeps_only_its_latest_visible_version() -> None:
    first = _event("v1", "zendesk", "ticket_opened", 10, record="ticket-7")
    revised = VisibleEvent(
        id="v2",
        source="zendesk",
        source_record_id="ticket-7",
        event_type="ticket_opened",
        event_time=first.event_time,
        ingested_at=CUTOFF - timedelta(days=2),
        attributes={},
    )

    kept = latest_visible_version([first, revised])

    assert [e.id for e in kept] == ["v2"]


def test_revised_record_does_not_double_count() -> None:
    first = _event("v1", "zendesk", "ticket_opened", 10, record="ticket-7")
    revised = _event("v2", "zendesk", "ticket_opened", 9, record="ticket-7")

    assert _value(latest_visible_version([first, revised]), "tickets_90d") == 1
