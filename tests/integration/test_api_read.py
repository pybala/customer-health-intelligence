"""Functional tests for the Group 1 read endpoints: tenancy, roles, auth, responses, no writes."""

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.main import create_app
from app.config import Settings
from app.db.session import tenant_session
from app.features.definitions import FEATURE_SET_VERSION
from app.outbox.store import claim_due, enqueue_approved, mark_failed, mark_sent
from app.policy.health import POLICY_VERSION
from app.security.api_keys import issue_key

pytestmark = pytest.mark.integration

CUTOFF = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PROBLEM = "application/problem+json"
GREEN_VALUES: dict[str, Any] = {
    "coverage_source_count_90d": 3,
    "has_product_90d": 1,
    "late_arrival_share_90d": 0.0,
    "failed_payments_60d": 0,
    "escalations_90d": 0,
    "active_users_trend_60v60": 1.0,
    "days_since_last_login": 1,
    "stage_regressions_180d": 0,
    "renewal_forecast_delta_90d": 0,
    "csat_90d_mean": 4.5,
    "days_since_last_contact": 5,
}
RED_VALUES = {**GREEN_VALUES, "failed_payments_60d": 2}
INSUFFICIENT_VALUES = {**GREEN_VALUES, "coverage_source_count_90d": 1}


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    model_version_id: uuid.UUID
    model_version: str
    csm: str
    readonly: str
    account_green: uuid.UUID
    account_red: uuid.UUID
    account_insufficient: uuid.UUID
    account_ineligible: uuid.UUID
    account_empty: uuid.UUID
    prediction_red: uuid.UUID
    prediction_green: uuid.UUID


def _write(engine: Engine, org_id: uuid.UUID, sql: str, params: dict[str, Any]) -> None:
    with tenant_session(engine, org_id) as session:
        session.execute(text(sql), params)


def _org(owner: Engine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    with owner.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
            {"i": org_id, "s": f"{slug}-{org_id.hex[:8]}", "n": slug},
        )
    return org_id


def _key(owner: Engine, org_id: uuid.UUID, pepper: str, role: str) -> str:
    issued = issue_key(pepper)
    _write(
        owner,
        org_id,
        "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) VALUES (:i, :o, :p, :h, :r)",
        {"i": uuid.uuid4(), "o": org_id, "p": issued.prefix, "h": issued.key_hash, "r": role},
    )
    return issued.plaintext


def _account(owner: Engine, org_id: uuid.UUID, name: str, renewal: datetime | None) -> uuid.UUID:
    account_id = uuid.uuid4()
    _write(
        owner,
        org_id,
        "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, plan_tier, "
        "region, timezone, renewal_date, created_at) VALUES (:i, :o, :n, :d, 'mid_market', "
        "'growth', 'na', 'UTC', :r, now())",
        {
            "i": account_id,
            "o": org_id,
            "n": name,
            "d": f"{account_id.hex}.example.test",
            "r": renewal,
        },
    )
    return account_id


def _prediction(
    owner: Engine,
    org_id: uuid.UUID,
    model_version_id: uuid.UUID,
    account_id: uuid.UUID,
    values: dict[str, Any] | None,
    renewal: datetime | None,
    cutoff: datetime = CUTOFF,
    risk: float | None = 0.5,
    provenance: dict[str, Any] | None = None,
) -> uuid.UUID:
    prediction_id = uuid.uuid4()
    eligible = values is not None
    _write(
        owner,
        org_id,
        "INSERT INTO predictions (id, org_id, account_id, model_version_id, feature_set_version, "
        "cutoff_at, predicted_at, eligible, ineligible_reason, renewal_date, risk_score, "
        "feature_values, feature_provenance) VALUES (:p, :o, :a, :m, :f, :c, :c, :e, :why, :r, "
        ":s, CAST(:v AS jsonb), CAST(:pr AS jsonb))",
        {
            "p": prediction_id,
            "o": org_id,
            "a": account_id,
            "m": model_version_id,
            "f": FEATURE_SET_VERSION,
            "c": cutoff,
            "e": eligible,
            "why": None if eligible else "no renewal date on record",
            "r": renewal,
            "s": risk if eligible else None,
            "v": json.dumps(values) if eligible else None,
            "pr": json.dumps(provenance or {}) if eligible else None,
        },
    )
    return prediction_id


@pytest.fixture(scope="module")
def tenant(owner_engine: Engine, pepper: str) -> Tenant:
    org_id = _org(owner_engine, "read-a")
    model_version_id = uuid.uuid4()
    model_version = f"read-{model_version_id.hex[:8]}"
    _write(
        owner_engine,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', :f, "
        ":h, '{}'::jsonb, '{}'::jsonb)",
        {
            "i": model_version_id,
            "o": org_id,
            "v": model_version,
            "f": FEATURE_SET_VERSION,
            "h": "0" * 64,
        },
    )
    soon = CUTOFF + timedelta(days=30)
    later = CUTOFF + timedelta(days=80)
    account_green = _account(owner_engine, org_id, "Green Co", later)
    account_red = _account(owner_engine, org_id, "Red Co", soon)
    account_insufficient = _account(owner_engine, org_id, "Thin Co", later)
    account_ineligible = _account(owner_engine, org_id, "No Renewal Co", None)
    account_empty = _account(owner_engine, org_id, "Unscored Co", later)
    prediction_red = _prediction(
        owner_engine,
        org_id,
        model_version_id,
        account_red,
        RED_VALUES,
        soon,
        risk=0.9,
        provenance={"failed_payments_60d": {"event_ids": []}},
    )
    prediction_green = _prediction(
        owner_engine, org_id, model_version_id, account_green, GREEN_VALUES, later, risk=0.2
    )
    _prediction(
        owner_engine,
        org_id,
        model_version_id,
        account_insufficient,
        INSUFFICIENT_VALUES,
        later,
        risk=0.4,
    )
    _prediction(owner_engine, org_id, model_version_id, account_ineligible, None, None, risk=None)
    return Tenant(
        org_id=org_id,
        model_version_id=model_version_id,
        model_version=model_version,
        csm=_key(owner_engine, org_id, pepper, "csm"),
        readonly=_key(owner_engine, org_id, pepper, "readonly"),
        account_green=account_green,
        account_red=account_red,
        account_insufficient=account_insufficient,
        account_ineligible=account_ineligible,
        account_empty=account_empty,
        prediction_red=prediction_red,
        prediction_green=prediction_green,
    )


@pytest.fixture(scope="module")
def other_tenant(owner_engine: Engine, pepper: str) -> Tenant:
    org_id = _org(owner_engine, "read-b")
    model_version_id = uuid.uuid4()
    _write(
        owner_engine,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', :f, "
        ":h, '{}'::jsonb, '{}'::jsonb)",
        {
            "i": model_version_id,
            "o": org_id,
            "v": f"other-{model_version_id.hex[:8]}",
            "f": FEATURE_SET_VERSION,
            "h": "0" * 64,
        },
    )
    account = _account(owner_engine, org_id, "Other Co", CUTOFF + timedelta(days=80))
    prediction = _prediction(
        owner_engine, org_id, model_version_id, account, GREEN_VALUES, CUTOFF + timedelta(days=80)
    )
    return Tenant(
        org_id=org_id,
        model_version_id=model_version_id,
        model_version=f"other-{model_version_id.hex[:8]}",
        csm=_key(owner_engine, org_id, pepper, "csm"),
        readonly=_key(owner_engine, org_id, pepper, "readonly"),
        account_green=account,
        account_red=account,
        account_insufficient=account,
        account_ineligible=account,
        account_empty=account,
        prediction_red=prediction,
        prediction_green=prediction,
    )


@pytest.fixture(scope="module")
def client(test_database, pepper: str) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        database_url=test_database.app_url,
        api_key_pepper=pepper,
        _env_file=None,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _row_counts(owner: Engine, org_id: uuid.UUID) -> dict[str, int]:
    tables = (
        "accounts",
        "predictions",
        "recommended_actions",
        "action_approvals",
        "outbox_messages",
        "audit_events",
        "model_versions",
        "data_quality_issues",
    )
    with tenant_session(owner, org_id) as session:
        return {
            table: session.execute(
                text(f"SELECT count(*) FROM {table} WHERE org_id = :o"),  # noqa: S608
                {"o": org_id},
            ).scalar_one()
            for table in tables
        }


def test_list_accounts_orders_by_risk_and_returns_ryg(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/api/v1/accounts", headers=_auth(tenant.csm))

    assert response.status_code == 200
    items = response.json()["items"]
    by_id = {item["account_id"]: item for item in items}
    assert items[0]["account_id"] == str(tenant.account_red)
    assert by_id[str(tenant.account_red)]["latest_prediction"]["ryg"]["status"] == "red"
    assert by_id[str(tenant.account_green)]["latest_prediction"]["ryg"]["status"] == "green"
    assert (
        by_id[str(tenant.account_insufficient)]["latest_prediction"]["ryg"]["status"]
        == "insufficient_data"
    )
    assert by_id[str(tenant.account_empty)]["latest_prediction"] is None


def test_ryg_carries_policy_version_cutoff_and_reasons(client: TestClient, tenant: Tenant) -> None:
    response = client.get(f"/api/v1/accounts/{tenant.account_red}", headers=_auth(tenant.csm))

    assert response.status_code == 200
    ryg = response.json()["latest_prediction"]["ryg"]
    assert ryg["policy_version"] == POLICY_VERSION
    assert datetime.fromisoformat(ryg["feature_cutoff_at"]) == CUTOFF
    assert [r["code"] for r in ryg["reasons"]] == ["repeated_payment_failure"]
    assert ryg["reasons"][0]["severity"] == "red"


def test_insufficient_data_reports_its_reasons(client: TestClient, tenant: Tenant) -> None:
    response = client.get(
        f"/api/v1/accounts/{tenant.account_insufficient}", headers=_auth(tenant.csm)
    )

    ryg = response.json()["latest_prediction"]["ryg"]
    assert ryg["status"] == "insufficient_data"
    assert [r["code"] for r in ryg["reasons"]] == ["too_few_sources"]


def test_ineligible_prediction_has_no_ryg(client: TestClient, tenant: Tenant) -> None:
    response = client.get(
        f"/api/v1/accounts/{tenant.account_ineligible}", headers=_auth(tenant.csm)
    )

    latest = response.json()["latest_prediction"]
    assert latest["eligible"] is False
    assert latest["ryg_state"] == "not_eligible"
    assert latest["ryg"] is None
    assert latest["risk_score"] is None
    assert latest["ineligible_reason"] == "no renewal date on record"


def test_account_profile_fields(client: TestClient, tenant: Tenant) -> None:
    body = client.get(f"/api/v1/accounts/{tenant.account_green}", headers=_auth(tenant.csm)).json()

    assert body["canonical_name"] == "Green Co"
    assert body["segment"] == "mid_market"
    assert body["plan_tier"] == "growth"
    assert body["renewal_date"] is not None


def test_account_list_pages_without_duplicates(client: TestClient, tenant: Tenant) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/api/v1/accounts", params=params, headers=_auth(tenant.csm)).json()
        seen.extend(item["account_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert str(tenant.account_empty) in seen
    assert str(tenant.account_red) in seen


def test_prediction_history_is_newest_first(client: TestClient, tenant: Tenant) -> None:
    body = client.get(
        f"/api/v1/accounts/{tenant.account_red}/predictions", headers=_auth(tenant.csm)
    ).json()

    assert [item["prediction_id"] for item in body["items"]] == [str(tenant.prediction_red)]
    assert body["items"][0]["scored_at"] is not None
    assert body["items"][0]["ryg_state"] == "evaluated"
    assert body["items"][0]["ryg"]["status"] == "red"


def test_prediction_detail_includes_contributions_and_model(
    client: TestClient, tenant: Tenant
) -> None:
    body = client.get(
        f"/api/v1/predictions/{tenant.prediction_red}", headers=_auth(tenant.csm)
    ).json()

    assert body["account_id"] == str(tenant.account_red)
    assert body["model_version"] == tenant.model_version
    assert body["feature_set_version"] == FEATURE_SET_VERSION
    assert body["contributions"] == {"failed_payments_60d": {"event_ids": []}}
    assert body["feature_values"]["failed_payments_60d"] == 2
    assert body["ryg"]["status"] == "red"


def test_evidence_for_prediction_without_events_is_empty(
    client: TestClient, tenant: Tenant
) -> None:
    response = client.get(
        f"/api/v1/predictions/{tenant.prediction_red}/evidence", headers=_auth(tenant.csm)
    )

    assert response.status_code == 200
    assert response.json() == {"prediction_id": str(tenant.prediction_red), "events": []}


def test_actions_list_is_empty_for_a_scored_account(client: TestClient, tenant: Tenant) -> None:
    response = client.get(
        f"/api/v1/accounts/{tenant.account_red}/actions", headers=_auth(tenant.csm)
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_outbox_is_empty_for_a_new_tenant(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/api/v1/outbox", headers=_auth(tenant.csm))

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_data_quality_for_a_new_tenant_is_zero(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/api/v1/data-quality", headers=_auth(tenant.csm))

    assert response.status_code == 200
    assert response.json() == {"issues": [], "total": 0}


def test_models_lists_the_tenants_registered_versions(client: TestClient, tenant: Tenant) -> None:
    body = client.get("/api/v1/models", headers=_auth(tenant.csm)).json()

    assert [item["model_version"] for item in body["items"]] == [tenant.model_version]


def test_model_evaluation_for_unknown_version_is_404(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/api/v1/models/does-not-exist/evaluation", headers=_auth(tenant.csm))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)


def test_model_evaluation_for_own_version_returns_metadata(
    client: TestClient, tenant: Tenant
) -> None:
    response = client.get(
        f"/api/v1/models/{tenant.model_version}/evaluation", headers=_auth(tenant.csm)
    )

    assert response.status_code == 200
    assert response.json()["model_version"] == tenant.model_version
    assert response.json()["evaluations"] == []


def test_other_tenant_account_is_not_found(
    client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    response = client.get(f"/api/v1/accounts/{other_tenant.account_red}", headers=_auth(tenant.csm))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)


def test_other_tenant_prediction_is_not_found(
    client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    for path in (
        f"/api/v1/predictions/{other_tenant.prediction_red}",
        f"/api/v1/predictions/{other_tenant.prediction_red}/evidence",
    ):
        assert client.get(path, headers=_auth(tenant.csm)).status_code == 404, path


def test_other_tenant_collections_are_isolated(
    client: TestClient, tenant: Tenant, other_tenant: Tenant
) -> None:
    accounts = client.get("/api/v1/accounts", headers=_auth(tenant.csm)).json()["items"]
    assert str(other_tenant.account_red) not in {item["account_id"] for item in accounts}
    assert (
        client.get(
            f"/api/v1/accounts/{other_tenant.account_red}/actions", headers=_auth(tenant.csm)
        ).status_code
        == 404
    )
    models = client.get("/api/v1/models", headers=_auth(tenant.csm)).json()["items"]
    assert other_tenant.model_version not in {item["model_version"] for item in models}


def test_readonly_token_can_read(client: TestClient, tenant: Tenant) -> None:
    for path in (
        "/api/v1/accounts",
        f"/api/v1/accounts/{tenant.account_red}",
        "/api/v1/outbox",
        "/api/v1/data-quality",
        "/api/v1/models",
    ):
        assert client.get(path, headers=_auth(tenant.readonly)).status_code == 200, path


def test_readonly_token_cannot_write(client: TestClient, tenant: Tenant) -> None:
    response = client.post(
        f"/api/v1/actions/{uuid.uuid4()}/approvals", headers=_auth(tenant.readonly)
    )

    assert response.status_code == 403


def test_unauthenticated_reads_are_rejected(client: TestClient, tenant: Tenant) -> None:
    for path in ("/api/v1/accounts", f"/api/v1/accounts/{tenant.account_red}", "/api/v1/outbox"):
        assert client.get(path).status_code == 401, path


def test_malformed_cursor_is_a_client_error(client: TestClient, tenant: Tenant) -> None:
    for cursor in ("!!!", "bm90LWpzb24"):
        response = client.get(
            "/api/v1/accounts", params={"cursor": cursor}, headers=_auth(tenant.csm)
        )
        assert response.status_code == 400, cursor
        assert response.headers["content-type"].startswith(PROBLEM)


def test_limit_above_maximum_is_rejected(client: TestClient, tenant: Tenant) -> None:
    response = client.get("/api/v1/accounts", params={"limit": 500}, headers=_auth(tenant.csm))

    assert response.status_code == 422


def test_approval_without_idempotency_key_is_rejected(client: TestClient, tenant: Tenant) -> None:
    response = client.post(
        f"/api/v1/actions/{uuid.uuid4()}/approvals",
        json={"decision": "approved"},
        headers=_auth(tenant.csm),
    )

    assert response.status_code == 422


def test_reads_do_not_write(client: TestClient, owner_engine: Engine, tenant: Tenant) -> None:
    before = _row_counts(owner_engine, tenant.org_id)

    for path in (
        "/api/v1/accounts",
        f"/api/v1/accounts/{tenant.account_red}",
        f"/api/v1/accounts/{tenant.account_red}/predictions",
        f"/api/v1/predictions/{tenant.prediction_red}",
        f"/api/v1/predictions/{tenant.prediction_red}/evidence",
        f"/api/v1/accounts/{tenant.account_red}/actions",
        "/api/v1/outbox",
        "/api/v1/data-quality",
        "/api/v1/models",
        f"/api/v1/models/{tenant.model_version}/evaluation",
    ):
        assert client.get(path, headers=_auth(tenant.readonly)).status_code == 200, path

    assert _row_counts(owner_engine, tenant.org_id) == before


def test_ryg_states_are_distinct_and_none_is_green(client: TestClient, tenant: Tenant) -> None:
    red = client.get(f"/api/v1/accounts/{tenant.account_red}", headers=_auth(tenant.csm)).json()
    thin = client.get(
        f"/api/v1/accounts/{tenant.account_insufficient}", headers=_auth(tenant.csm)
    ).json()
    ineligible = client.get(
        f"/api/v1/accounts/{tenant.account_ineligible}", headers=_auth(tenant.csm)
    ).json()

    assert red["latest_prediction"]["ryg_state"] == "evaluated"
    assert thin["latest_prediction"]["ryg_state"] == "evaluated"
    assert thin["latest_prediction"]["ryg"]["status"] == "insufficient_data"
    assert ineligible["latest_prediction"]["ryg_state"] == "not_eligible"
    assert ineligible["latest_prediction"]["ryg"] is None


def test_sort_defaults_to_risk_desc_and_accepts_it_explicitly(
    client: TestClient, tenant: Tenant
) -> None:
    default = client.get("/api/v1/accounts", headers=_auth(tenant.csm)).json()
    explicit = client.get(
        "/api/v1/accounts", params={"sort": "risk_desc"}, headers=_auth(tenant.csm)
    ).json()

    assert [i["account_id"] for i in default["items"]] == [
        i["account_id"] for i in explicit["items"]
    ]


@pytest.mark.parametrize("sort", ["risk_asc", "name", "", "RISK_DESC"])
def test_unsupported_sort_is_400(client: TestClient, tenant: Tenant, sort: str) -> None:
    response = client.get("/api/v1/accounts", params={"sort": sort}, headers=_auth(tenant.csm))

    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM)
    assert response.json()["type"].endswith("unsupported-sort")


OUTBOX_STATUSES = ("pending", "sending", "sent", "dry_run_recorded", "suppressed", "dead_letter")
OUTBOX_LOCK_FOR = timedelta(minutes=10)
OUTBOX_WORKER = "api-read-test"


@dataclass(frozen=True)
class OutboxTenant:
    org_id: uuid.UUID
    model_version_id: uuid.UUID
    csm: str


@pytest.fixture(scope="module")
def outbox_tenant(owner_engine: Engine, pepper: str) -> OutboxTenant:
    org_id = _org(owner_engine, "read-outbox")
    model_version_id = uuid.uuid4()
    _write(
        owner_engine,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', :f, "
        ":h, '{}'::jsonb, '{}'::jsonb)",
        {
            "i": model_version_id,
            "o": org_id,
            "v": f"outbox-{model_version_id.hex[:8]}",
            "f": FEATURE_SET_VERSION,
            "h": "0" * 64,
        },
    )
    return OutboxTenant(
        org_id=org_id,
        model_version_id=model_version_id,
        csm=_key(owner_engine, org_id, pepper, "csm"),
    )


def _seed_outbox_message(
    app_engine: Engine, owner: Engine, tenant: OutboxTenant, status: str
) -> uuid.UUID:
    """One approved action driven through the real outbox functions until it reaches status.

    Each action is enqueued right after it is approved, so each enqueue creates exactly one
    message. A pending message is due a year out, so the claims for other statuses skip it.
    """
    renewal = CUTOFF + timedelta(days=60)
    account = _account(owner, tenant.org_id, f"Outbox {status}", renewal)
    prediction = _prediction(
        owner, tenant.org_id, tenant.model_version_id, account, GREEN_VALUES, renewal
    )
    action_id = uuid.uuid4()
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, action_type, "
        "reason_code, evidence) VALUES (:i, :o, :a, :p, 'csm_outreach_email', 'risk_threshold', "
        "'{}'::jsonb)",
        {"i": action_id, "o": tenant.org_id, "a": account, "p": prediction},
    )
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, 'approved', :actor)",
        {"i": uuid.uuid4(), "o": tenant.org_id, "a": action_id, "actor": uuid.uuid4()},
    )
    _write(
        app_engine,
        tenant.org_id,
        "UPDATE recommended_actions SET status = 'approved' WHERE org_id = :o AND id = :a",
        {"o": tenant.org_id, "a": action_id},
    )
    if status == "suppressed":
        _write(
            app_engine,
            tenant.org_id,
            "INSERT INTO suppressions (id, org_id, account_id, reason, starts_at, created_by) "
            "VALUES (:i, :o, :a, 'legal_hold', :s, :c)",
            {
                "i": uuid.uuid4(),
                "o": tenant.org_id,
                "a": account,
                "s": CUTOFF - timedelta(days=1),
                "c": uuid.uuid4(),
            },
        )

    pending_only = status == "pending"
    enqueue_at = CUTOFF + timedelta(days=365) if pending_only else CUTOFF
    enqueue_approved(
        app_engine,
        tenant.org_id,
        "dry_run" if status == "dry_run_recorded" else "simulated",
        enqueue_at,
    )
    with tenant_session(owner, tenant.org_id) as session:
        message_id = session.execute(
            text("SELECT id FROM outbox_messages WHERE org_id = :o AND action_id = :a"),
            {"o": tenant.org_id, "a": action_id},
        ).scalar_one()

    if status in ("dry_run_recorded", "suppressed") or status == "sending":
        claim_due(
            app_engine, tenant.org_id, OUTBOX_WORKER, CUTOFF, limit=10, lock_for=OUTBOX_LOCK_FOR
        )
    elif status == "sent":
        [claimed], _ = claim_due(
            app_engine, tenant.org_id, OUTBOX_WORKER, CUTOFF, limit=10, lock_for=OUTBOX_LOCK_FOR
        )
        assert mark_sent(app_engine, tenant.org_id, claimed.id, OUTBOX_WORKER, CUTOFF) is True
    elif status == "dead_letter":
        now = CUTOFF
        for _ in range(5):
            [claimed], _ = claim_due(
                app_engine, tenant.org_id, OUTBOX_WORKER, now, limit=10, lock_for=OUTBOX_LOCK_FOR
            )
            mark_failed(app_engine, tenant.org_id, claimed, OUTBOX_WORKER, "smtp 451", now)
            now += timedelta(hours=2)
    return message_id


def _outbox_items(client: TestClient, tenant: OutboxTenant, status: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, str] = {"status": status, "limit": "2"}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/api/v1/outbox", params=params, headers=_auth(tenant.csm)).json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("outbox pagination did not terminate")


def test_outbox_status_filter_returns_only_that_status(
    client: TestClient, app_engine: Engine, owner_engine: Engine, outbox_tenant: OutboxTenant
) -> None:
    seeded = {
        status: _seed_outbox_message(app_engine, owner_engine, outbox_tenant, status)
        for status in OUTBOX_STATUSES
    }

    for status, message_id in seeded.items():
        items = _outbox_items(client, outbox_tenant, status)
        assert {item["status"] for item in items} == {status}, status
        assert str(message_id) in {item["outbox_id"] for item in items}, status


def test_outbox_without_filter_returns_every_status(
    client: TestClient, outbox_tenant: OutboxTenant
) -> None:
    body = client.get(
        "/api/v1/outbox", params={"limit": "200"}, headers=_auth(outbox_tenant.csm)
    ).json()

    assert {item["status"] for item in body["items"]} == set(OUTBOX_STATUSES)


@pytest.mark.parametrize("status", ["Pending", "dead-letter", "", "done"])
def test_unknown_outbox_status_is_rejected(
    client: TestClient, outbox_tenant: OutboxTenant, status: str
) -> None:
    response = client.get(
        "/api/v1/outbox", params={"status": status}, headers=_auth(outbox_tenant.csm)
    )

    assert response.status_code == 422


def test_outbox_filter_is_tenant_scoped(
    client: TestClient,
    app_engine: Engine,
    owner_engine: Engine,
    outbox_tenant: OutboxTenant,
    other_tenant: Tenant,
) -> None:
    foreign = _seed_outbox_message(
        app_engine,
        owner_engine,
        OutboxTenant(other_tenant.org_id, other_tenant.model_version_id, other_tenant.csm),
        "pending",
    )

    assert str(foreign) not in {
        item["outbox_id"] for item in _outbox_items(client, outbox_tenant, "pending")
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"outbox_id": "not-an-account"},
        [1, 2],
        {"account_id": "00000000-0000-0000-0000-000000000000", "risk_score": "high"},
    ],
)
def test_cursor_from_another_shape_is_400(client: TestClient, tenant: Tenant, payload: Any) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    response = client.get("/api/v1/accounts", params={"cursor": cursor}, headers=_auth(tenant.csm))

    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM)
