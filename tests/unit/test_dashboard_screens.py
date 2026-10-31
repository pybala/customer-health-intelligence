import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from app.dashboard import api_client
from app.dashboard.api_client import ApiClient

MAIN = Path(__file__).resolve().parents[2] / "app" / "dashboard" / "main.py"
TOKEN = "dev_" + "t" * 30

ACME, BETA, GAMMA, DELTA = (f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 5))
ACTION_ACME = "20000000-0000-0000-0000-000000000001"
NOW = datetime.now(UTC)


def _iso(delta: timedelta | None = None) -> str:
    return (NOW + (delta or timedelta(0))).isoformat()


def _prediction(
    prediction_id: str, state: str, status: str | None, score: float | None
) -> dict[str, Any]:
    ryg = None
    if state == "evaluated":
        ryg = {
            "status": status,
            "policy_version": "p1",
            "feature_cutoff_at": _iso(),
            "reasons": [],
        }
    return {
        "prediction_id": prediction_id,
        "model_version_id": "m",
        "feature_cutoff_at": _iso(),
        "scored_at": _iso(),
        "eligible": state != "not_eligible",
        "ineligible_reason": "outside_window" if state == "not_eligible" else None,
        "risk_score": score,
        "ryg_state": state,
        "ryg": ryg,
    }


def _account(
    account_id: str,
    name: str,
    renewal: timedelta | None,
    prediction: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "canonical_name": name,
        "segment": "smb",
        "renewal_date": None if renewal is None else _iso(renewal),
        "latest_prediction": prediction,
    }


PRED_IDS = {
    ACME: "10000000-0000-0000-0000-000000000001",
    BETA: "10000000-0000-0000-0000-000000000002",
    GAMMA: "10000000-0000-0000-0000-000000000003",
}

ACCOUNTS = [
    _account(
        ACME,
        "Acme",
        timedelta(days=30),
        _prediction(PRED_IDS[ACME], "evaluated", "green", 0.19),
    ),
    _account(
        BETA,
        "Beta",
        None,
        _prediction(PRED_IDS[BETA], "evaluated", "insufficient_data", 0.19),
    ),
    _account(
        GAMMA,
        "Gamma",
        timedelta(days=200),
        _prediction(PRED_IDS[GAMMA], "not_eligible", None, None),
    ),
    _account(DELTA, "Delta", None, None),
]


def _action(status: str = "proposed") -> dict[str, Any]:
    return {
        "action_id": ACTION_ACME,
        "prediction_id": PRED_IDS[ACME],
        "action_type": "outreach_email",
        "status": status,
        "reason_code": "usage_decline",
        "evidence": {
            "policy_version": "p1",
            "status": "red",
            "cutoff_at": _iso(),
            "reasons": [
                {
                    "code": "usage_decline",
                    "feature": "active_users_30d",
                    "observed": 3,
                    "threshold": 10,
                }
            ],
            "event_ids": {"active_users_30d": ["e1", "e2"]},
        },
        "proposed_at": _iso(),
    }


def _prediction_detail(prediction_id: str) -> dict[str, Any]:
    return {
        "prediction_id": prediction_id,
        "feature_cutoff_at": _iso(),
        "scored_at": _iso(),
        "risk_score": 0.19,
        "ryg_state": "evaluated",
        "ryg": None,
        "feature_values": {"api_calls_30d": 120, "tickets_90d": 4},
        "contributions": {
            "api_calls_30d": {"source": "product", "how": "sum", "window_days": 30},
            "tickets_90d": {"source": "zendesk", "how": "count", "window_days": 90},
        },
    }


@dataclass
class FakeApi:
    decide_replies: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        parts = path.strip("/").split("/")
        if request.method == "POST" and path.endswith("/approvals"):
            status, body = self.decide_replies.pop(0)
            return httpx.Response(status, json=body)
        if path == "/api/v1/accounts":
            return httpx.Response(200, json={"items": ACCOUNTS, "next_cursor": None})
        if path == "/api/v1/outbox":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if path == "/api/v1/data-quality":
            return httpx.Response(200, json={"issues": [], "total": 0})
        if len(parts) == 4 and parts[2] == "accounts":
            return self._account_route(parts[3])
        if len(parts) == 5 and parts[4] == "actions":
            items = [_action()] if parts[3] == ACME else []
            return httpx.Response(200, json={"items": items})
        if len(parts) == 4 and parts[2] == "predictions":
            return httpx.Response(200, json=_prediction_detail(parts[3]))
        if len(parts) == 5 and parts[2] == "predictions" and parts[4] == "evidence":
            events = [
                {
                    "event_id": "e1",
                    "source": "product",
                    "event_type": "api_call_batch",
                    "event_time": _iso(),
                    "ingested_at": _iso(),
                    "source_record_id": "rec-1",
                }
            ]
            return httpx.Response(200, json={"prediction_id": parts[3], "events": events})
        return httpx.Response(404, json={"detail": f"no fake for {path}"})

    def _account_route(self, account_id: str) -> httpx.Response:
        account = next((a for a in ACCOUNTS if a["account_id"] == account_id), None)
        if account is None:
            return httpx.Response(404, json={"detail": "Account not found"})
        detail = {
            **account,
            "primary_domain": "example.test",
            "plan_tier": "pro",
            "region": "emea",
            "timezone": "Europe/London",
        }
        return httpx.Response(200, json=detail)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()

    class Mocked(ApiClient):
        def __init__(self, base_url: str, token: str, *, transport: Any = None) -> None:
            super().__init__(base_url, token, transport=httpx.MockTransport(api.handle))

    monkeypatch.setattr(api_client, "ApiClient", Mocked)
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    return api


def _app(fake_api: FakeApi) -> AppTest:
    at = AppTest.from_file(str(MAIN), default_timeout=20)
    at.run()
    at.sidebar.text_input[0].input(TOKEN).run()
    return at


def _screen(at: AppTest, name: str) -> AppTest:
    at.sidebar.radio[0].set_value(name).run()
    return at


def _button(at: AppTest, key: str) -> Any:
    matches = [b for b in at.button if b.key == key]
    assert matches, f"no button {key}"
    return matches[0]


def _kpis(at: AppTest) -> dict[str, int]:
    pattern = re.compile(
        r'<div class="chi-kpi-label">([^<]+)</div><div class="chi-kpi-count">(\d+)</div>'
    )
    markdown = "\n".join(str(m.value) for m in at.markdown)
    return {label: int(count) for label, count in pattern.findall(markdown)}


def _rendered_text(at: AppTest) -> str:
    parts: list[str] = []
    for group in (at.markdown, at.caption, at.subheader, at.warning, at.error, at.success, at.info):
        parts.extend(str(element.value) for element in group)
    parts.extend(str(metric.label) for metric in at.metric)
    return "\n".join(parts)


def test_no_token_shows_prompt_and_no_api_call(fake_api: FakeApi) -> None:
    at = AppTest.from_file(str(MAIN), default_timeout=20)
    at.run()

    assert any("Enter an API token" in str(i.value) for i in at.info)
    assert fake_api.requests == []


def test_token_is_not_rendered_and_no_org_id_is_sent(fake_api: FakeApi) -> None:
    at = _app(fake_api)

    assert TOKEN not in _rendered_text(at)
    assert dict(at.query_params) == {}
    for request in fake_api.requests:
        assert "org_id" not in str(request.url)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_portfolio_kpis_reconcile_to_total_without_double_counting(fake_api: FakeApi) -> None:
    at = _app(fake_api)

    kpis = _kpis(at)
    assert kpis["Total accounts"] == 4
    assert kpis["Healthy"] == 1
    assert kpis["At-risk"] == 0
    assert kpis["Critical"] == 0
    assert kpis["Total accounts"] == kpis["Healthy"] + kpis["At-risk"] + kpis["Critical"] + 3
    assert any("= 4 accounts" in str(c.value) for c in at.caption)


def test_portfolio_warns_about_constant_score_and_labels_it_a_model_score(
    fake_api: FakeApi,
) -> None:
    at = _app(fake_api)

    assert any("constant prior" in str(w.value) for w in at.warning)
    assert any("not calibrated renewal probabilities" in str(c.value) for c in at.caption)
    assert not any("probability of" in str(m.value).lower() for m in at.markdown)


def test_portfolio_lists_attention_empty_state_and_renewal_count(fake_api: FakeApi) -> None:
    at = _app(fake_api)

    assert any(str(s.value) == "Accounts requiring attention" for s in at.subheader)
    assert any("No accounts are red or yellow" in str(m.value) for m in at.markdown)
    assert any("Actions awaiting a decision: 1" in str(m.value) for m in at.markdown)


def test_data_quality_screen_reports_issues_by_category(fake_api: FakeApi) -> None:
    at = _screen(_app(fake_api), "Data quality")

    labels = {metric.label: metric.value for metric in at.metric}
    assert labels["Duplicate records"] == "0"
    assert any("No data-quality issues" in str(m.value) for m in at.markdown)


def test_account_list_keeps_non_green_states_distinct(fake_api: FakeApi) -> None:
    at = _screen(_app(fake_api), "Accounts")

    frame = at.dataframe[0].value
    health = dict(zip(frame["Account"], frame["Health"], strict=True))
    assert health == {
        "Acme": "Green",
        "Beta": "Insufficient data",
        "Gamma": "Not eligible",
        "Delta": "Not scored",
    }


def test_account_search_filters_by_name(fake_api: FakeApi) -> None:
    at = _screen(_app(fake_api), "Accounts")
    at.text_input(key="accounts_search").input("bet").run()

    frame = at.dataframe[0].value
    assert list(frame["Account"]) == ["Beta"]
    assert any("Showing 1 of 4 accounts" in str(c.value) for c in at.caption)


def test_account_detail_explains_not_eligible_without_green(fake_api: FakeApi) -> None:
    at = _screen(_app(fake_api), "Account detail")
    at.selectbox(key="detail_account").set_value(GAMMA).run()

    text = _rendered_text(at)
    assert "Not eligible for scoring: outside_window" in text
    assert "No verdict. Not green." in text
    assert not any("Green" in str(m.value) for m in at.markdown)


def test_account_detail_groups_evidence_by_source_with_timestamps(fake_api: FakeApi) -> None:
    at = _screen(_app(fake_api), "Account detail")
    at.selectbox(key="detail_account").set_value(ACME).run()

    bold = [str(m.value) for m in at.markdown if str(m.value).startswith("**")]
    assert "**Product**" in bold
    assert "**Zendesk**" in bold
    assert "**Salesforce**" not in bold
    assert "**Other sources**" not in bold
    assert any("feature cutoff" in str(c.value) for c in at.caption)
    features = [
        row
        for frame in at.dataframe
        if "Feature" in frame.value.columns
        for _, row in frame.value.iterrows()
    ]
    api_calls = next(row for row in features if row["Feature"] == "api_calls_30d")
    assert api_calls["Source"] == "product"
    assert api_calls["Value"] == 120
    assert api_calls["As of"].endswith("UTC")
    for frame in at.dataframe:
        assert not {"Direction", "Signal", "Impact"} & set(frame.value.columns)


def test_approve_retry_reuses_the_idempotency_key(fake_api: FakeApi) -> None:
    approved = {
        "approval_id": "x",
        "action_id": ACTION_ACME,
        "decision": "approved",
        "decided_at": _iso(),
    }
    fake_api.decide_replies = [(503, {"detail": "try later"}), (201, approved)]
    at = _screen(_app(fake_api), "Action review")

    _button(at, f"approve_{ACTION_ACME}").click().run()
    assert any("The API failed (503)" in str(e.value) for e in at.error)
    _button(at, f"approve_{ACTION_ACME}").click().run()

    posts = [r for r in fake_api.requests if r.method == "POST"]
    assert len(posts) == 2
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]
    assert any("simulated outbox row" in str(s.value) for s in at.success)


def test_conflicting_decision_is_reported(fake_api: FakeApi) -> None:
    conflict = {"type": "x", "title": "t", "status": 409, "detail": "already decided"}
    fake_api.decide_replies = [(409, conflict)]
    at = _screen(_app(fake_api), "Action review")

    _button(at, f"reject_{ACTION_ACME}").click().run()

    assert any("already has a different decision" in str(e.value) for e in at.error)


def test_rejection_sends_rejected_and_states_no_outbox(fake_api: FakeApi) -> None:
    rejected = {
        "approval_id": "x",
        "action_id": ACTION_ACME,
        "decision": "rejected",
        "decided_at": _iso(),
    }
    fake_api.decide_replies = [(201, rejected)]
    at = _screen(_app(fake_api), "Action review")

    _button(at, f"reject_{ACTION_ACME}").click().run()

    posts = [r for r in fake_api.requests if r.method == "POST"]
    assert json.loads(posts[0].content) == {"decision": "rejected"}
    assert any("No outbox message is created" in str(s.value) for s in at.success)
