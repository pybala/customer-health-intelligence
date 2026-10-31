"""The four CSM screens. They render API data through view.py and compute no policy or scores."""

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import altair as alt
import httpx
import pandas as pd
import streamlit as st

from app.dashboard import view
from app.dashboard.api_client import ApiClient, ApiError, Decision

PORTFOLIO = "Portfolio"
ACCOUNTS = "Accounts"
DETAIL = "Account detail"
ACTIONS = "Action review"
DATA_QUALITY = "Data quality"
SCREENS = (PORTFOLIO, ACCOUNTS, DETAIL, ACTIONS, DATA_QUALITY)

SIMULATED_NOTE = (
    "Delivery is simulated. Approving records an outbox row in simulated mode; nothing is sent."
)
RISK_LABEL = "Model risk score (not a calibrated probability)"
OUTBOX_PAGE_NOTE = "Outbox status reflects the first 200 rows returned by the API."
SUBTITLES = {
    PORTFOLIO: "Health states, renewals and approvals across your accounts.",
    ACCOUNTS: "Every account in your organisation, with its latest verdict.",
    DETAIL: "Why an account is in its health state, and the evidence behind it.",
    ACTIONS: "Proposed outreach awaiting a decision. Delivery is simulated.",
    DATA_QUALITY: "Ingestion issues detected for your organisation, by source.",
}
THEME_CSS = """<style>
.chi-header {
  background: linear-gradient(120deg, #1e1b4b 0%, #4338ca 60%, #0f766e 100%);
  border-radius: 10px;
  color: #ffffff;
  margin-bottom: 1rem;
  padding: 1.1rem 1.4rem;
}
.chi-header h2 { color: #ffffff; font-size: 1.5rem; margin: 0; }
.chi-header p { color: #e0e7ff; margin: 0.25rem 0 0; }
.chi-banner {
  border-left: 6px solid;
  border-radius: 8px;
  padding: 0.8rem 1rem;
  margin: 0.4rem 0 0.8rem;
}
.chi-banner, .chi-banner strong { color: #0f172a; }
.chi-banner strong { font-size: 1.05rem; }
.chi-kpi {
  background: #ffffff;
  border-radius: 10px;
  border-top: 4px solid;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
  padding: 0.9rem 1rem;
}
.chi-kpi-count { color: #0f172a; font-size: 2rem; font-weight: 700; line-height: 1.1; }
.chi-kpi-label {
  color: #475569;
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.chi-kpi-note { color: #64748b; font-size: 0.8rem; margin-top: 0.25rem; }
.chi-profile {
  background: #ffffff;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
  padding: 1rem 1.2rem;
}
.chi-profile h3 { color: #0f172a; margin: 0 0 0.3rem; }
.chi-profile p { color: #475569; margin: 0; }
[data-testid="stSidebar"] { background: linear-gradient(180deg, #1e1b4b 0%, #312e81 100%); }
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color: #e0e7ff; }
[data-testid="stAppViewContainer"] { background: #f1f5f9; }
</style>"""
SORT_LABELS = {
    "priority": "Priority (red first)",
    "risk": "Risk score (API order)",
    "name": "Name",
    "renewal": "Renewal date",
}
ACTION_FILTERS = ("proposed", "approved", "rejected", "all")
FILTER_LABELS = {**view.ACTION_STATUS_LABELS, "all": "All statuses"}

_DECISION_ERRORS = {
    401: "The token was rejected. Check the API token in the sidebar.",
    403: "Your role cannot decide actions, or this token is readonly.",
    404: "This action is not in your organisation.",
    409: "This action already has a different decision. Refresh to see the current state.",
}


@dataclass(frozen=True)
class Snapshot:
    accounts: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    outbox: list[dict[str, Any]]
    loaded_at: datetime


def load_snapshot(client: ApiClient) -> Snapshot:
    accounts = client.accounts()
    actions = [
        {**action, "account_id": account["account_id"], "account_name": account["canonical_name"]}
        for account in accounts
        for action in client.actions(account["account_id"])
    ]
    return Snapshot(accounts, actions, client.outbox(), datetime.now(UTC))


def render_header(screen: str) -> None:
    st.markdown(THEME_CSS, unsafe_allow_html=True)
    st.markdown(
        f'<div class="chi-header"><h2>{screen}</h2><p>{SUBTITLES[screen]}</p></div>',
        unsafe_allow_html=True,
    )


def show_notice() -> None:
    notice = st.session_state.pop("notice", None)
    if notice is not None:
        level, text = notice
        getattr(st, level)(text)


def render_portfolio(snapshot: Snapshot, now: datetime) -> None:
    summary = view.portfolio(snapshot.accounts, snapshot.actions, now)
    shared = view.shared_score(snapshot.accounts)
    if shared is not None:
        st.warning(
            f"Every scored account has the same risk score ({shared:.2f}). This is a constant "
            "prior, not a ranking or a probability, so the risk column does not order accounts."
        )
    figures = view.kpis(summary)
    cols = st.columns(4)
    _kpi(
        cols[0],
        "Total accounts",
        figures.total,
        "#4338ca",
        f"{summary.upcoming_renewals} renew in 90 days",
    )
    _kpi(cols[1], "Healthy", figures.healthy, "#16a34a", "Evaluated green verdicts")
    _kpi(cols[2], "At-risk", figures.at_risk, "#ca8a04", "Evaluated yellow verdicts")
    _kpi(cols[3], "Critical", figures.critical, "#dc2626", "Evaluated red verdicts")
    st.caption(
        f"Healthy {figures.healthy} + At-risk {figures.at_risk} + Critical {figures.critical} "
        f"+ Other {figures.other} = {figures.total} accounts. Other covers insufficient data, "
        "not eligible, not scored and no snapshot; none of those is counted as healthy."
    )

    left, right = st.columns([2, 3])
    with left:
        st.subheader("Health distribution")
        _donut(summary.counts)
    with right:
        st.subheader("Attention summary")
        st.write(
            f"{figures.critical} critical and {figures.at_risk} at-risk accounts need review. "
            f"Actions awaiting a decision: {summary.awaiting_approval}. "
            f"{figures.other} accounts are not yet verdict-ready."
        )
        st.caption("Scores are model risk scores, not calibrated renewal probabilities.")

    st.subheader("Accounts requiring attention")
    attention = view.attention_accounts(snapshot.accounts)
    if attention:
        _show_table(
            [
                {
                    "Account": account["canonical_name"],
                    "Health": view.label(view.health_key(account)),
                    "Renewal": view.renewal_text(account["renewal_date"], now),
                    "Evidence (reason codes)": _reason_codes(account),
                }
                for account in attention
            ],
            [view.health_key(account) for account in attention],
            view.HEALTH_TINTS,
        )
    else:
        st.write("No accounts are red or yellow.")

    st.subheader("Upcoming renewals")
    renewals = view.renewals_within(snapshot.accounts, now)
    if renewals:
        _show_table(
            [
                {
                    "Account": account["canonical_name"],
                    "Renewal": view.format_date(account["renewal_date"]),
                    "Health": view.label(view.health_key(account)),
                }
                for account in renewals
            ],
            [view.health_key(account) for account in renewals],
            view.HEALTH_TINTS,
        )
    else:
        st.write("No renewals in the next 90 days.")

    st.subheader("Proposed actions awaiting approval")
    proposed = [action for action in snapshot.actions if action["status"] == "proposed"]
    if proposed:
        _show_table(
            [
                {
                    "Account": action["account_name"],
                    "Action": action["action_type"],
                    "Reason code": action["reason_code"],
                    "Proposed": view.format_time(action["proposed_at"]),
                }
                for action in proposed
            ],
            [action["status"] for action in proposed],
            view.ACTION_TINTS,
        )
        st.caption("Approve or reject these on the Action review screen.")
    else:
        st.write("Nothing is waiting for approval.")


def render_accounts(snapshot: Snapshot, now: datetime) -> None:
    search, health, by = st.columns([3, 2, 2])
    query = search.text_input("Search accounts", key="accounts_search", placeholder="Account name")
    health_key = health.selectbox(
        "Health state",
        [None, *view.RYG_ORDER],
        format_func=lambda key: "All states" if key is None else view.label(key),
        key="accounts_health",
    )
    sort_key = by.selectbox(
        "Sort by", list(SORT_LABELS), format_func=SORT_LABELS.__getitem__, key="accounts_sort"
    )
    shared = view.shared_score(snapshot.accounts)
    matches = view.search_accounts(snapshot.accounts, query)
    rows = view.sort_accounts(view.filter_accounts(matches, health_key), sort_key)
    st.caption(f"Showing {len(rows)} of {len(snapshot.accounts)} accounts.")
    st.caption(f"{RISK_LABEL}. {view.shared_score_note(shared)}")
    _show_table(
        [_account_row(account, shared, now) for account in rows],
        [view.health_key(account) for account in rows],
        view.HEALTH_TINTS,
    )
    st.caption("To open an account's profile, choose it on the Account detail screen.")


def render_detail(client: ApiClient, snapshot: Snapshot, now: datetime) -> None:
    ordered = view.sort_accounts(snapshot.accounts, "name")
    names = {account["account_id"]: account["canonical_name"] for account in ordered}
    if not names:
        st.write("No accounts are visible to this token.")
        return
    account_id = st.selectbox(
        "Account", list(names), format_func=names.__getitem__, key="detail_account"
    )
    account = client.account(account_id)
    pred = account["latest_prediction"]
    shared = view.shared_score(snapshot.accounts)
    key = view.health_key(account)

    st.markdown(
        f'<div class="chi-profile"><h3>{html.escape(account["canonical_name"])}</h3>'
        f"<p>{html.escape(account['segment'])} · {html.escape(account['plan_tier'])} · "
        f"{html.escape(account['region'])} · {html.escape(account['timezone'])} · "
        f"{html.escape(account['primary_domain'])}</p></div>",
        unsafe_allow_html=True,
    )

    st.subheader("Health")
    st.markdown(
        _banner(
            view.tint(view.HEALTH_TINTS, key),
            view.label(key),
            view.health_explanation(pred),
        ),
        unsafe_allow_html=True,
    )

    st.subheader("Renewal")
    st.write(view.renewal_text(account["renewal_date"], now))
    if account["renewal_date"] is not None:
        st.caption(f"Renewal date {view.format_date(account['renewal_date'])}")

    st.subheader("Prediction")
    if pred is None:
        st.write("No prediction is stored for this account.")
    else:
        st.write(f"Model risk score: {view.risk_display(pred, shared)}")
        st.caption(f"{RISK_LABEL}. Ranks no accounts when the score is shared across them.")
        st.caption(
            f"Scored {view.format_time(pred.get('scored_at'))} · feature cutoff "
            f"{view.format_time(pred['feature_cutoff_at'])}"
        )
        ryg = pred.get("ryg")
        if ryg and ryg["reasons"]:
            st.write("Reasons behind the verdict (the only negative signals shown)")
            _show_table(
                [
                    {
                        "Code": reason["code"],
                        "Feature": reason["feature"],
                        "Observed": reason["observed"],
                        "Threshold": reason["threshold"],
                        "Severity": view.label(reason["severity"]),
                    }
                    for reason in ryg["reasons"]
                ],
                [reason["severity"] for reason in ryg["reasons"]],
                view.HEALTH_TINTS,
            )
        st.subheader("Evidence by source")
        _render_evidence_sources(client, pred)

    st.subheader("Recommended actions")
    actions = [action for action in snapshot.actions if action["account_id"] == account_id]
    if actions:
        _show_table(
            [
                {
                    "Action": action["action_type"],
                    "Reason code": action["reason_code"],
                    "Status": view.ACTION_STATUS_LABELS.get(action["status"], action["status"]),
                    "Proposed": view.format_time(action["proposed_at"]),
                }
                for action in actions
            ],
            [action["status"] for action in actions],
            view.ACTION_TINTS,
        )
    else:
        st.write("No recommended actions for this account.")

    st.subheader("Recent activity")
    activity = view.action_history(actions, snapshot.outbox)
    if activity:
        st.dataframe(activity, hide_index=True)
        st.caption(OUTBOX_PAGE_NOTE)
    else:
        st.write("No activity is recorded for this account.")


def render_actions(snapshot: Snapshot) -> None:
    st.info(SIMULATED_NOTE)
    counts = {status: 0 for status in view.ACTION_STATUS_LABELS}
    for action in snapshot.actions:
        counts[action["status"]] = counts.get(action["status"], 0) + 1
    cols = st.columns(3)
    _kpi(cols[0], "Awaiting approval", counts["proposed"], "#ca8a04", "No decision recorded")
    _kpi(cols[1], "Approved", counts["approved"], "#16a34a", "Outbox is simulated; nothing sent")
    _kpi(cols[2], "Rejected", counts["rejected"], "#64748b", "No outbox message created")

    st.subheader("Awaiting approval")
    proposed = [action for action in snapshot.actions if action["status"] == "proposed"]
    if not proposed:
        st.write("No actions are waiting for a decision.")
    for action in proposed:
        _action_card(action, snapshot)

    st.subheader("All actions")
    status = st.selectbox(
        "Status",
        list(ACTION_FILTERS),
        format_func=FILTER_LABELS.__getitem__,
        key="actions_status",
    )
    rows = [a for a in snapshot.actions if status == "all" or a["status"] == status]
    if not rows:
        st.write("No actions match this status.")
        return
    _show_table(
        [
            {
                "Account": action["account_name"],
                "Action": action["action_type"],
                "Reason code": action["reason_code"],
                "Status": view.ACTION_STATUS_LABELS.get(action["status"], action["status"]),
                "Delivery": _delivery_text(action["action_id"], snapshot),
            }
            for action in rows
        ],
        [action["status"] for action in rows],
        view.ACTION_TINTS,
    )
    st.caption(OUTBOX_PAGE_NOTE)


def render_data_quality(client: ApiClient) -> None:
    report = client.data_quality()
    issues = report["issues"]
    totals = view.data_quality_totals(issues)
    cols = st.columns(len(view.DQ_CATEGORY_ORDER))
    for col, category in zip(cols, view.DQ_CATEGORY_ORDER, strict=True):
        col.metric(category, totals[category])
    st.caption(f"Total issues detected: {report['total']}. Counts are by kind and source.")
    if not issues:
        st.write("No data-quality issues are recorded for your organisation.")
        return
    st.subheader("Issues by source")
    st.dataframe(view.data_quality_rows(issues), hide_index=True)


def _decide(action_id: str, decision: Decision, reason_key: str) -> None:
    client = st.session_state.get("client")
    if client is None:
        st.session_state["notice"] = ("error", "No API token is set.")
        return
    reason = str(st.session_state.get(reason_key, "")).strip() or None
    attempts: dict[str, dict[str, Any]] = st.session_state.setdefault("attempts", {})
    attempt = attempts.get(action_id)
    if attempt is None or attempt["decision"] != decision or attempt["reason"] != reason:
        attempt = {"key": client.new_idempotency_key(), "decision": decision, "reason": reason}
        attempts[action_id] = attempt
    st.session_state["snapshot"] = None
    try:
        client.decide(action_id, decision, attempt["key"], reason)
    except httpx.TransportError:
        st.session_state["notice"] = (
            "error",
            "The API is unreachable. Press again to retry; the same key is reused, so the "
            "decision cannot apply twice.",
        )
        return
    except ApiError as error:
        if error.status >= 500:
            st.session_state["notice"] = (
                "error",
                f"The API failed ({error.status}). Press again to retry with the same key.",
            )
            return
        attempts.pop(action_id, None)
        message = _DECISION_ERRORS.get(error.status, f"Rejected ({error.status}): {error.detail}")
        st.session_state["notice"] = ("error", message)
        return
    attempts.pop(action_id, None)
    if decision == "approved":
        text = "Approved. A simulated outbox row was recorded; nothing was sent."
    else:
        text = "Rejected. No outbox message is created."
    st.session_state["notice"] = ("success", text)


def _render_evidence_sources(client: ApiClient, pred: dict[str, Any]) -> None:
    detail = client.prediction(pred["prediction_id"])
    features = view.feature_rows_by_source(
        detail["feature_values"], detail["contributions"], detail.get("feature_cutoff_at")
    )
    events = view.evidence_by_source(client.evidence(pred["prediction_id"]))
    for label, rows in features.items():
        if not rows and not events[label]:
            continue
        st.markdown(f"**{label}**")
        if rows:
            st.dataframe(rows, hide_index=True)
        else:
            st.caption("No feature values are stored for this source.")
        if events[label]:
            st.dataframe(
                [
                    {
                        "Source": event["source"],
                        "Event": event["event_type"],
                        "Event time": view.format_time(event["event_time"]),
                        "Source record": event["source_record_id"],
                    }
                    for event in events[label]
                ],
                hide_index=True,
            )
    if not any(features.values()) and not any(events.values()):
        st.write("No evidence is stored for this prediction.")


def _action_card(action: dict[str, Any], snapshot: Snapshot) -> None:
    action_id = action["action_id"]
    with st.container(border=True):
        st.markdown(
            f"**{html.escape(action['account_name'])}** · {html.escape(action['action_type'])}  \n"
            f"Reason {html.escape(action['reason_code'])} · proposed "
            f"{view.format_time(action['proposed_at'])}"
        )
        evidence = action["evidence"]
        cited = sum(len(ids) for ids in evidence.get("event_ids", {}).values())
        st.caption(
            f"Policy {evidence.get('policy_version', '-')} · cutoff "
            f"{view.format_time(evidence.get('cutoff_at'))} · {cited} cited events"
        )
        reasons = evidence.get("reasons", [])
        if reasons:
            st.dataframe(
                [
                    {
                        "Code": reason["code"],
                        "Feature": reason["feature"],
                        "Observed": reason["observed"],
                        "Threshold": reason["threshold"],
                    }
                    for reason in reasons
                ],
                hide_index=True,
            )
        reason_key = f"reason_{action_id}"
        st.text_input("Reason (optional)", max_chars=500, key=reason_key)
        approve, reject = st.columns(2)
        approve.button(
            "Approve",
            key=f"approve_{action_id}",
            type="primary",
            on_click=_decide,
            args=(action_id, "approved", reason_key),
        )
        reject.button(
            "Reject",
            key=f"reject_{action_id}",
            on_click=_decide,
            args=(action_id, "rejected", reason_key),
        )
        st.caption(_delivery_text(action_id, snapshot))


def _kpi(col: Any, label: str, count: int, accent: str, note: str) -> None:
    col.markdown(
        f'<div class="chi-kpi" style="border-top-color:{accent}">'
        f'<div class="chi-kpi-label">{html.escape(label)}</div>'
        f'<div class="chi-kpi-count">{count}</div>'
        f'<div class="chi-kpi-note">{html.escape(note)}</div></div>',
        unsafe_allow_html=True,
    )


def _donut(counts: dict[str, int]) -> None:
    keys = [view.GREEN, view.YELLOW, view.RED]
    frame = pd.DataFrame(
        [
            {"State": view.label(key), "Accounts": counts[key], "Order": i}
            for i, key in enumerate(keys)
        ]
    )
    colours = [view.tint(view.HEALTH_TINTS, key)[1] for key in keys]
    chart = (
        alt.Chart(frame)
        .mark_arc(innerRadius=60, outerRadius=110)
        .encode(
            theta=alt.Theta("Accounts:Q"),
            color=alt.Color(
                "State:N",
                scale=alt.Scale(domain=list(frame["State"]), range=colours),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
            order=alt.Order("Order:Q"),
            tooltip=["State", "Accounts"],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, width="stretch")
    st.caption("Green, yellow and red verdicts only. Other states are excluded from this chart.")


def _delivery_text(action_id: str, snapshot: Snapshot) -> str:
    action = next(a for a in snapshot.actions if a["action_id"] == action_id)
    row = view.outbox_for(action_id, snapshot.outbox)
    if action["status"] == "rejected":
        return "Rejected. No outbox message is created."
    if action["status"] == "proposed":
        return "No decision yet. Approving creates one simulated outbox row."
    if row is None:
        return (
            "Approved. No outbox row appears in the loaded data. This is not confirmation "
            "of delivery; use Reload data to check again."
        )
    status = view.OUTBOX_LABELS.get(row["status"], row["status"])
    return f"{status}. Attempt {row['attempts']} of {row['max_attempts']}."


def _reason_codes(account: dict[str, Any]) -> str:
    pred = account["latest_prediction"]
    ryg = pred.get("ryg") if pred else None
    codes = [reason["code"] for reason in ryg["reasons"]] if ryg else []
    return ", ".join(codes) or "-"


def _account_row(account: dict[str, Any], shared: float | None, now: datetime) -> dict[str, Any]:
    pred = account["latest_prediction"]
    return {
        "Account": account["canonical_name"],
        "Health": view.label(view.health_key(account)),
        "Renewal": view.renewal_text(account["renewal_date"], now),
        "Latest prediction": view.format_time(pred.get("scored_at")) if pred else "-",
        "Model risk score": view.risk_display(pred, shared),
        "Evidence (reason codes)": _reason_codes(account),
    }


def _show_table(records: list[dict[str, Any]], keys: list[str], tints: dict[str, Any]) -> None:
    if not records:
        st.dataframe(records, hide_index=True)
        return

    def style_row(row: pd.Series) -> list[str]:
        background, _ = view.tint(tints, keys[int(row.name)])
        return [f"background-color: {background}"] * len(row)

    st.dataframe(pd.DataFrame(records).style.apply(style_row, axis=1), hide_index=True)


def _banner(tint_pair: tuple[str, str], title: str, body: str) -> str:
    background, accent = tint_pair
    return (
        f'<div class="chi-banner" style="background:{background};border-left-color:{accent}">'
        f"<strong>{html.escape(title)}</strong><br>"
        f"{html.escape(body)}</div>"
    )
