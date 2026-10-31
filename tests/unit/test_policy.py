from datetime import UTC, datetime, timedelta

import pytest

from app.policy.capacity import review_capacity
from app.policy.health import Status, evaluate
from app.policy.suppression import CONTACT_COOLDOWN, SuppressionFacts, suppression_reason

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _healthy() -> dict[str, object]:
    return {
        "coverage_source_count_90d": 5,
        "has_product_90d": 1,
        "late_arrival_share_90d": 0.0,
        "failed_payments_60d": 0,
        "escalations_90d": 0,
        "active_users_trend_60v60": 1.0,
        "days_since_last_login": 1.0,
        "stage_regressions_180d": 0,
        "renewal_forecast_delta_90d": 0.0,
        "csat_90d_mean": 4.5,
        "days_since_last_contact": 5.0,
    }


def test_healthy_evidence_with_full_coverage_is_green() -> None:
    decision = evaluate(_healthy(), days_to_renewal=60)

    assert decision.status is Status.GREEN
    assert decision.reasons == ()


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"coverage_source_count_90d": 2}, "too_few_sources"),
        ({"has_product_90d": 0}, "no_product_usage"),
        ({"late_arrival_share_90d": 0.9}, "data_arriving_late"),
        ({"coverage_source_count_90d": None}, "too_few_sources"),
    ],
)
def test_insufficient_coverage_is_never_green(override: dict[str, object], code: str) -> None:
    values = {**_healthy(), **override}

    decision = evaluate(values, days_to_renewal=60)

    assert decision.status is Status.INSUFFICIENT_DATA
    assert code in {r.code for r in decision.reasons}


def test_insufficient_data_outranks_critical_signals() -> None:
    values = {**_healthy(), "coverage_source_count_90d": 1, "failed_payments_60d": 4}

    assert evaluate(values, days_to_renewal=60).status is Status.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    "override",
    [
        {"failed_payments_60d": 2},
        {"escalations_90d": 2, "days_to_renewal": 30},
        {"active_users_trend_60v60": 0.4, "days_since_last_login": 45.0},
    ],
)
def test_critical_evidence_is_red(override: dict[str, object]) -> None:
    days = int(override.pop("days_to_renewal", 60))  # type: ignore[call-overload]
    values = {**_healthy(), **override}

    assert evaluate(values, days_to_renewal=days).status is Status.RED


def test_two_escalations_far_from_renewal_are_not_red() -> None:
    values = {**_healthy(), "escalations_90d": 2}

    assert evaluate(values, days_to_renewal=85).status is Status.YELLOW


def test_single_warning_signal_is_yellow() -> None:
    values = {**_healthy(), "failed_payments_60d": 1}

    decision = evaluate(values, days_to_renewal=60)

    assert decision.status is Status.YELLOW
    assert [r.code for r in decision.reasons] == ["payment_failure"]


def test_every_reason_names_its_feature_and_threshold() -> None:
    values = {**_healthy(), "failed_payments_60d": 3, "csat_90d_mean": 2.0}

    decision = evaluate(values, days_to_renewal=60)

    assert decision.reasons
    for reason in decision.reasons:
        assert reason.feature in values
        assert reason.observed == values[reason.feature]


def test_risk_score_is_not_an_input() -> None:
    assert "risk_score" not in evaluate.__code__.co_varnames


def test_legal_hold_always_suppresses() -> None:
    facts = SuppressionFacts(
        legal_hold=True, critical_escalation_open=False, last_successful_contact_at=None
    )

    assert suppression_reason(facts, NOW) == "legal_hold"


def test_open_critical_escalation_suppresses_automated_outreach() -> None:
    facts = SuppressionFacts(
        legal_hold=False, critical_escalation_open=True, last_successful_contact_at=None
    )

    assert suppression_reason(facts, NOW) == "critical_escalation"


def test_contact_inside_cooldown_suppresses_and_outside_does_not() -> None:
    inside = SuppressionFacts(False, False, NOW - CONTACT_COOLDOWN + timedelta(seconds=1))
    outside = SuppressionFacts(False, False, NOW - CONTACT_COOLDOWN)

    assert suppression_reason(inside, NOW) == "recent_contact"
    assert suppression_reason(outside, NOW) is None


def test_cooldown_is_seven_days() -> None:
    assert timedelta(days=7) == CONTACT_COOLDOWN


def test_clear_account_is_not_suppressed() -> None:
    facts = SuppressionFacts(False, False, None)

    assert suppression_reason(facts, NOW) is None


def test_legal_hold_takes_precedence_over_other_reasons() -> None:
    facts = SuppressionFacts(True, True, NOW - timedelta(days=1))

    assert suppression_reason(facts, NOW) == "legal_hold"


@pytest.mark.parametrize(
    ("eligible", "fraction", "expected"),
    [(0, 0.10, 0), (1, 0.10, 1), (10, 0.10, 1), (11, 0.10, 2), (50, 0.10, 5), (50, 0.0, 0)],
)
def test_review_capacity_rounds_up_and_respects_fraction(
    eligible: int, fraction: float, expected: int
) -> None:
    assert review_capacity(eligible, fraction) == expected
