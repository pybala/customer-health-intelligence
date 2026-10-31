"""Display rules for API responses. Pure functions, no I/O.

Every RYG state that is not green is labelled as such, so an account without a verdict never
reads as healthy. The API already reports why a RYG is absent; this module only words it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

GREEN = "green"
YELLOW = "yellow"
RED = "red"
INSUFFICIENT = "insufficient_data"
NOT_ELIGIBLE = "not_eligible"
NO_SNAPSHOT = "no_snapshot"
NO_PREDICTION = "no_prediction"

RYG_ORDER = (RED, YELLOW, GREEN, INSUFFICIENT, NOT_ELIGIBLE, NO_SNAPSHOT, NO_PREDICTION)

LABELS = {
    RED: "Red",
    YELLOW: "Yellow",
    GREEN: "Green",
    INSUFFICIENT: "Insufficient data",
    NOT_ELIGIBLE: "Not eligible",
    NO_SNAPSHOT: "No snapshot",
    NO_PREDICTION: "Not scored",
}

NEUTRAL_TINT = ("rgba(100, 116, 139, 0.16)", "#64748b")

HEALTH_TINTS = {
    RED: ("rgba(220, 38, 38, 0.16)", "#dc2626"),
    YELLOW: ("rgba(202, 138, 4, 0.18)", "#ca8a04"),
    GREEN: ("rgba(22, 163, 74, 0.16)", "#16a34a"),
    INSUFFICIENT: ("rgba(124, 58, 237, 0.15)", "#7c3aed"),
    NOT_ELIGIBLE: NEUTRAL_TINT,
    NO_SNAPSHOT: NEUTRAL_TINT,
    NO_PREDICTION: NEUTRAL_TINT,
}

ACTION_STATUS_LABELS = {
    "proposed": "Awaiting approval",
    "approved": "Approved",
    "rejected": "Rejected",
}

ACTION_TINTS = {
    "proposed": ("rgba(202, 138, 4, 0.18)", "#ca8a04"),
    "approved": ("rgba(22, 163, 74, 0.16)", "#16a34a"),
    "rejected": NEUTRAL_TINT,
}

ATTENTION_STATES = (RED, YELLOW)

OUTBOX_LABELS = {
    "pending": "Queued in the simulated outbox, not yet sent",
    "sending": "Being processed by the simulated outbox",
    "sent": "Recorded as sent in simulated mode. Nothing was emailed.",
    "dry_run_recorded": "Dry run recorded. Nothing was sent.",
    "suppressed": "Suppressed. Nothing was sent.",
    "dead_letter": "Failed after all attempts. Nothing was sent.",
}

DQ_CATEGORY_ORDER = (
    "Duplicate records",
    "Unresolved identities",
    "Missing or incomplete source data",
    "Late or out-of-order data",
    "Other data-quality issues",
)

DQ_CATEGORIES = {
    "duplicate": DQ_CATEGORY_ORDER[0],
    "unmatched_identity": DQ_CATEGORY_ORDER[1],
    "ambiguous_identity": DQ_CATEGORY_ORDER[1],
    "missing_source": DQ_CATEGORY_ORDER[2],
    "malformed_record": DQ_CATEGORY_ORDER[2],
    "late_arrival": DQ_CATEGORY_ORDER[3],
    "out_of_order": DQ_CATEGORY_ORDER[3],
    "out_of_order_batch": DQ_CATEGORY_ORDER[3],
}

EVIDENCE_SOURCES = {
    "salesforce": "Salesforce",
    "gmail": "Gmail",
    "zendesk": "Zendesk",
    "product": "Product",
    "stripe": "Stripe",
}
OTHER_SOURCE = "Other sources"

RENEWAL_WINDOW = timedelta(days=90)


def health_key(account: dict[str, Any]) -> str:
    """The single state an account is shown under. Non-green states keep their own key."""
    prediction = account.get("latest_prediction")
    if prediction is None:
        return NO_PREDICTION
    ryg = prediction.get("ryg")
    if prediction.get("ryg_state") == "evaluated" and ryg is not None:
        return str(ryg["status"])
    return str(prediction.get("ryg_state") or NO_PREDICTION)


def label(key: str) -> str:
    return LABELS.get(key, key)


def tint(mapping: dict[str, tuple[str, str]], key: str) -> tuple[str, str]:
    return mapping.get(key, NEUTRAL_TINT)


def priority_rank(key: str) -> int:
    return RYG_ORDER.index(key) if key in RYG_ORDER else len(RYG_ORDER)


def priority_key(account: dict[str, Any]) -> tuple[int, bool, datetime]:
    value = account.get("renewal_date")
    when = _as_utc(_parse(value)) if value is not None else datetime.max.replace(tzinfo=UTC)
    return (priority_rank(health_key(account)), value is None, when)


def attention_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Red and yellow accounts, most severe first, then by soonest renewal."""
    flagged = [account for account in accounts if health_key(account) in ATTENTION_STATES]
    return sorted(flagged, key=priority_key)


def days_until(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    return (_as_utc(_parse(value)) - now).days


def renewal_text(value: str | None, now: datetime) -> str:
    days = days_until(value, now)
    if days is None:
        return "No renewal date recorded"
    if days < 0:
        return f"Renewal date passed {-days} days ago"
    return f"Renews in {days} days"


def action_history(
    actions: list[dict[str, Any]],
    outbox: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Newest first. The API stores proposal and outbox times, but no approval decision time."""
    accounts = {action["action_id"]: action.get("account_name", "-") for action in actions}
    events: list[tuple[datetime, dict[str, str]]] = []
    for action in actions:
        when = _as_utc(_parse(action["proposed_at"]))
        account = accounts[action["action_id"]]
        events.append((when, _event(when, account, "Action proposed", action["action_type"])))
    for row in outbox:
        if row["action_id"] not in accounts:
            continue
        account = accounts[row["action_id"]]
        status = OUTBOX_LABELS.get(row["status"], row["status"])
        created = _as_utc(_parse(row["created_at"]))
        events.append((created, _event(created, account, "Simulated outbox row created", status)))
        if row["finished_at"] is not None:
            finished = _as_utc(_parse(row["finished_at"]))
            events.append((finished, _event(finished, account, "Simulated outbox updated", status)))
    events.sort(key=lambda event: event[0], reverse=True)
    return [row for _, row in events]


def _event(when: datetime, account: str, event: str, detail: str) -> dict[str, str]:
    return {"When": format_time(when), "Account": account, "Event": event, "Detail": detail}


def dq_category(kind: str) -> str:
    return DQ_CATEGORIES.get(kind, DQ_CATEGORY_ORDER[-1])


def data_quality_totals(issues: list[dict[str, Any]]) -> dict[str, int]:
    totals = dict.fromkeys(DQ_CATEGORY_ORDER, 0)
    for issue in issues:
        totals[dq_category(issue["kind"])] += int(issue["count"])
    return totals


def data_quality_rows(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        {
            "Category": dq_category(issue["kind"]),
            "Source": issue["source"],
            "Issue": issue["kind"],
            "Count": int(issue["count"]),
        }
        for issue in issues
    ]
    return sorted(
        rows,
        key=lambda row: (DQ_CATEGORY_ORDER.index(row["Category"]), row["Source"], row["Issue"]),
    )


def health_explanation(prediction: dict[str, Any] | None) -> str:
    if prediction is None:
        return "No prediction is stored for this account, so it has not been scored. Not green."
    state = prediction.get("ryg_state")
    ryg = prediction.get("ryg")
    if state == "evaluated" and ryg is not None:
        if ryg["status"] == INSUFFICIENT:
            return (
                "Insufficient data for a verdict; the reasons below say what is missing. Not green."
            )
        return f"{label(str(ryg['status']))} under policy {ryg['policy_version']}."
    if state == NOT_ELIGIBLE:
        reason = prediction.get("ineligible_reason") or "outside the scoring window"
        return f"Not eligible for scoring: {reason}. No verdict. Not green."
    if state == NO_SNAPSHOT:
        return "Eligible, but no feature snapshot is stored, so there is no verdict. Not green."
    return f"Unrecognised state {state}. Not green."


def format_date(value: str | None) -> str:
    return "No renewal date" if value is None else _parse(value).date().isoformat()


def format_time(value: str | datetime | None) -> str:
    if value is None:
        return "-"
    return _as_utc(_parse(value)).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def shared_score(accounts: list[dict[str, Any]]) -> float | None:
    """Return the risk score if every scored account shares it, else None."""
    scores = [s for s in (_score(account) for account in accounts) if s is not None]
    if len(scores) >= 2 and len(set(scores)) == 1:
        return scores[0]
    return None


def shared_score_note(shared: float | None) -> str:
    if shared is None:
        return "Scores differ across accounts, so the risk column can be compared."
    return f"All scored accounts share one score ({shared:.2f}), so the column does not rank them."


def risk_display(prediction: dict[str, Any] | None, shared: float | None) -> str:
    if prediction is None or prediction.get("risk_score") is None:
        return "-"
    score = float(prediction["risk_score"])
    if shared is not None and score == shared:
        return "constant prior (not a ranking)"
    return f"{score:.2f}"


def outbox_for(action_id: str, outbox: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((row for row in outbox if row["action_id"] == action_id), None)


def source_groups() -> dict[str, list[Any]]:
    return {label: [] for label in (*EVIDENCE_SOURCES.values(), OTHER_SOURCE)}


def feature_rows_by_source(
    feature_values: dict[str, Any] | None,
    contributions: dict[str, Any],
    as_of: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Feature values by source. The only timestamp stored is the prediction's feature cutoff."""
    groups = source_groups()
    for name, value in sorted((feature_values or {}).items()):
        source = contributions.get(name, {}).get("source")
        groups[EVIDENCE_SOURCES.get(str(source), OTHER_SOURCE)].append(
            {
                "Source": "-" if source is None else str(source),
                "Feature": name,
                "Value": value,
                "As of": format_time(as_of),
            }
        )
    return groups


def evidence_by_source(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = source_groups()
    for event in events:
        groups[EVIDENCE_SOURCES.get(str(event["source"]), OTHER_SOURCE)].append(event)
    return groups


def search_accounts(accounts: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return accounts
    return [account for account in accounts if needle in str(account["canonical_name"]).lower()]


@dataclass(frozen=True)
class Portfolio:
    total: int
    counts: dict[str, int]
    upcoming_renewals: int
    awaiting_approval: int


def portfolio(
    accounts: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    now: datetime,
) -> Portfolio:
    counts = dict.fromkeys(RYG_ORDER, 0)
    for account in accounts:
        counts[health_key(account)] += 1
    awaiting = sum(1 for action in actions if action["status"] == "proposed")
    return Portfolio(
        total=len(accounts),
        counts=counts,
        upcoming_renewals=len(renewals_within(accounts, now)),
        awaiting_approval=awaiting,
    )


@dataclass(frozen=True)
class Kpis:
    total: int
    healthy: int
    at_risk: int
    critical: int
    other: int


def kpis(summary: Portfolio) -> Kpis:
    """Healthy, at-risk and critical cover the RYG verdicts; every other state lands in other."""
    counts = summary.counts
    other = sum(count for key, count in counts.items() if key not in (GREEN, YELLOW, RED))
    return Kpis(summary.total, counts[GREEN], counts[YELLOW], counts[RED], other)


def renewals_within(accounts: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    horizon = now + RENEWAL_WINDOW
    due = [
        account
        for account in accounts
        if account.get("renewal_date") is not None
        and now <= _as_utc(_parse(account["renewal_date"])) <= horizon
    ]
    return sorted(due, key=lambda a: _as_utc(_parse(a["renewal_date"])))


def filter_accounts(accounts: list[dict[str, Any]], health: str | None) -> list[dict[str, Any]]:
    if health is None:
        return accounts
    return [account for account in accounts if health_key(account) == health]


def sort_accounts(accounts: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    """Sort for display. 'risk' keeps the API order (risk, highest first, then unscored)."""
    if by == "priority":
        return sorted(accounts, key=priority_key)
    if by == "name":
        return sorted(accounts, key=lambda a: str(a["canonical_name"]).lower())
    if by == "renewal":
        return sorted(
            accounts,
            key=lambda a: (
                a.get("renewal_date") is None,
                _as_utc(_parse(a["renewal_date"])) if a.get("renewal_date") else datetime.min,
            ),
        )
    return accounts


def _score(account: dict[str, Any]) -> float | None:
    prediction = account.get("latest_prediction")
    if prediction is None or prediction.get("risk_score") is None:
        return None
    return float(prediction["risk_score"])


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
