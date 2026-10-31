from datetime import UTC, datetime, timedelta
from typing import Any

from app.dashboard.view import (
    GREEN,
    INSUFFICIENT,
    NO_PREDICTION,
    NO_SNAPSHOT,
    NOT_ELIGIBLE,
    RED,
    YELLOW,
    action_history,
    attention_accounts,
    data_quality_rows,
    data_quality_totals,
    evidence_by_source,
    feature_rows_by_source,
    filter_accounts,
    health_key,
    kpis,
    portfolio,
    renewal_text,
    search_accounts,
    sort_accounts,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _account(
    name: str,
    *,
    ryg_state: str = "evaluated",
    status: str | None = GREEN,
    renewal: datetime | None = None,
) -> dict[str, Any]:
    ryg = None
    if ryg_state == "evaluated" and status is not None:
        ryg = {"status": status, "policy_version": "p", "feature_cutoff_at": "x", "reasons": []}
    return {
        "account_id": name,
        "canonical_name": name,
        "renewal_date": renewal.isoformat() if renewal else None,
        "latest_prediction": {
            "ryg_state": ryg_state,
            "ryg": ryg,
            "risk_score": 0.2 if ryg_state == "evaluated" else None,
        },
    }


def test_insufficient_data_is_its_own_state_not_green() -> None:
    account = _account("a", status=INSUFFICIENT)
    assert health_key(account) == INSUFFICIENT
    assert health_key(account) != GREEN


def test_not_eligible_and_no_snapshot_are_never_green() -> None:
    assert health_key(_account("a", ryg_state=NOT_ELIGIBLE)) == NOT_ELIGIBLE
    assert health_key(_account("b", ryg_state=NO_SNAPSHOT)) == NO_SNAPSHOT


def test_account_without_any_prediction_is_not_scored() -> None:
    assert health_key({"canonical_name": "c", "latest_prediction": None}) == NO_PREDICTION


def test_evaluated_statuses_pass_through() -> None:
    assert health_key(_account("r", status=RED)) == RED
    assert health_key(_account("y", status=YELLOW)) == YELLOW
    assert health_key(_account("g", status=GREEN)) == GREEN


def test_portfolio_counts_every_state_and_sums_to_total() -> None:
    accounts = [
        _account("g", status=GREEN),
        _account("r", status=RED),
        _account("i", status=INSUFFICIENT),
        _account("n", ryg_state=NOT_ELIGIBLE),
        {"canonical_name": "u", "latest_prediction": None},
    ]
    result = portfolio(accounts, [], NOW)
    assert result.total == 5
    assert sum(result.counts.values()) == 5
    assert result.counts[GREEN] == 1
    assert result.counts[INSUFFICIENT] == 1
    assert result.counts[NOT_ELIGIBLE] == 1
    assert result.counts[NO_PREDICTION] == 1


def test_upcoming_renewals_are_counted_inside_ninety_days_only() -> None:
    accounts = [
        _account("soon", renewal=NOW + timedelta(days=30)),
        _account("edge", renewal=NOW + timedelta(days=90)),
        _account("later", renewal=NOW + timedelta(days=120)),
        _account("past", renewal=NOW - timedelta(days=1)),
        _account("none", renewal=None),
    ]
    assert portfolio(accounts, [], NOW).upcoming_renewals == 2


def test_awaiting_approval_counts_only_proposed_actions() -> None:
    actions = [{"status": "proposed"}, {"status": "approved"}, {"status": "proposed"}]
    assert portfolio([], actions, NOW).awaiting_approval == 2


def test_filter_by_health_keeps_non_green_separate() -> None:
    accounts = [_account("g", status=GREEN), _account("i", status=INSUFFICIENT)]
    assert [a["canonical_name"] for a in filter_accounts(accounts, INSUFFICIENT)] == ["i"]
    assert [a["canonical_name"] for a in filter_accounts(accounts, GREEN)] == ["g"]
    assert len(filter_accounts(accounts, None)) == 2


def test_sort_by_name_is_case_insensitive() -> None:
    accounts = [_account("beta"), _account("Alpha"), _account("gamma")]
    assert [a["canonical_name"] for a in sort_accounts(accounts, "name")] == [
        "Alpha",
        "beta",
        "gamma",
    ]


def test_sort_by_renewal_puts_missing_dates_last() -> None:
    accounts = [
        _account("none"),
        _account("late", renewal=NOW + timedelta(days=200)),
        _account("early", renewal=NOW + timedelta(days=10)),
    ]
    assert [a["canonical_name"] for a in sort_accounts(accounts, "renewal")] == [
        "early",
        "late",
        "none",
    ]


def test_risk_sort_keeps_api_order() -> None:
    accounts = [_account("first"), _account("second")]
    assert sort_accounts(accounts, "risk") is accounts


def test_attention_lists_red_then_yellow_then_soonest_renewal_only() -> None:
    accounts = [
        _account("yellow-late", status=YELLOW, renewal=NOW + timedelta(days=200)),
        _account("green", status=GREEN, renewal=NOW + timedelta(days=5)),
        _account("red-none", status=RED, renewal=None),
        _account("insufficient", status=INSUFFICIENT, renewal=NOW + timedelta(days=1)),
        _account("red-soon", status=RED, renewal=NOW + timedelta(days=10)),
        _account("yellow-soon", status=YELLOW, renewal=NOW + timedelta(days=20)),
    ]
    names = [a["canonical_name"] for a in attention_accounts(accounts)]
    assert names == ["red-soon", "red-none", "yellow-soon", "yellow-late"]


def test_priority_sort_puts_red_before_green() -> None:
    accounts = [_account("g", status=GREEN), _account("r", status=RED)]
    assert [a["canonical_name"] for a in sort_accounts(accounts, "priority")] == ["r", "g"]


def test_renewal_text_states_passed_and_missing_dates_plainly() -> None:
    assert renewal_text(None, NOW) == "No renewal date recorded"
    assert renewal_text((NOW + timedelta(days=12)).isoformat(), NOW) == "Renews in 12 days"
    assert renewal_text((NOW - timedelta(days=3)).isoformat(), NOW) == (
        "Renewal date passed 3 days ago"
    )


def test_data_quality_groups_kinds_into_categories() -> None:
    issues = [
        {"kind": "duplicate", "source": "crm", "count": 2},
        {"kind": "unmatched_identity", "source": "crm", "count": 3},
        {"kind": "ambiguous_identity", "source": "product", "count": 1},
        {"kind": "missing_source", "source": "zendesk", "count": 4},
        {"kind": "late_arrival", "source": "product", "count": 5},
        {"kind": "timezone_assumed", "source": "crm", "count": 6},
    ]
    totals = data_quality_totals(issues)
    assert totals["Duplicate records"] == 2
    assert totals["Unresolved identities"] == 4
    assert totals["Missing or incomplete source data"] == 4
    assert totals["Late or out-of-order data"] == 5
    assert totals["Other data-quality issues"] == 6
    rows = data_quality_rows(issues)
    assert [row["Category"] for row in rows][0] == "Duplicate records"
    assert sum(row["Count"] for row in rows) == sum(i["count"] for i in issues)


def test_action_history_is_newest_first_and_never_claims_delivery() -> None:
    actions = [
        {
            "action_id": "a1",
            "action_type": "csm_outreach_email",
            "proposed_at": "2026-10-01T10:00:00+00:00",
            "account_name": "Acme",
        }
    ]
    outbox = [
        {
            "action_id": "a1",
            "status": "sent",
            "created_at": "2026-10-02T10:00:00+00:00",
            "finished_at": "2026-10-02T10:01:00+00:00",
        }
    ]
    history = action_history(actions, outbox)
    assert [row["Event"] for row in history] == [
        "Simulated outbox updated",
        "Simulated outbox row created",
        "Action proposed",
    ]
    assert all(row["Account"] == "Acme" for row in history)
    assert history[0]["Detail"] == ("Recorded as sent in simulated mode. Nothing was emailed.")
    assert not any("delivered" in row["Detail"].lower() for row in history)


def test_kpis_map_healthy_at_risk_critical_and_reconcile_to_total() -> None:
    accounts = [
        _account("g", status=GREEN),
        _account("y", status=YELLOW),
        _account("r", status=RED),
        _account("i", status=INSUFFICIENT),
        _account("n", ryg_state=NOT_ELIGIBLE),
        {"canonical_name": "u", "latest_prediction": None},
    ]
    figures = kpis(portfolio(accounts, [], NOW))
    assert (figures.total, figures.healthy, figures.at_risk, figures.critical) == (6, 1, 1, 1)
    assert figures.other == 3
    assert figures.healthy + figures.at_risk + figures.critical + figures.other == figures.total


def test_search_matches_name_case_insensitively_and_ignores_blank_query() -> None:
    accounts = [_account("Acme Corp"), _account("Beta Ltd")]
    assert [a["canonical_name"] for a in search_accounts(accounts, " acme ")] == ["Acme Corp"]
    assert search_accounts(accounts, "  ") is accounts


def test_feature_rows_group_by_source_and_carry_cutoff_without_direction() -> None:
    features = {"api_calls_30d": 120, "tickets_90d": 4, "seats": 9}
    contributions = {
        "api_calls_30d": {"source": "product"},
        "tickets_90d": {"source": "zendesk"},
    }
    groups = feature_rows_by_source(features, contributions, "2026-10-29T00:00:00+00:00")
    assert groups["Product"] == [
        {
            "Source": "product",
            "Feature": "api_calls_30d",
            "Value": 120,
            "As of": "2026-10-29 00:00 UTC",
        }
    ]
    assert groups["Zendesk"][0]["Feature"] == "tickets_90d"
    assert groups["Other sources"][0]["Source"] == "-"
    assert all("Direction" not in row for rows in groups.values() for row in rows)


def test_evidence_groups_events_by_source() -> None:
    events = [
        {"source": "stripe", "event_type": "invoice"},
        {"source": "gmail", "event_type": "thread"},
        {"source": "unknown", "event_type": "x"},
    ]
    groups = evidence_by_source(events)
    assert [e["event_type"] for e in groups["Stripe"]] == ["invoice"]
    assert [e["event_type"] for e in groups["Gmail"]] == ["thread"]
    assert [e["event_type"] for e in groups["Other sources"]] == ["x"]
