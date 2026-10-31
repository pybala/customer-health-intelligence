"""Group 2 approval endpoint: idempotency, roles, tenancy, state rules and the database gate."""

import json
import threading
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
from app.security.api_keys import issue_key

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PROBLEM = "application/problem+json"
VALUES: dict[str, Any] = {
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


@dataclass(frozen=True)
class Org:
    org_id: uuid.UUID
    model_version_id: uuid.UUID
    csm: str
    admin: str
    readonly: str


def _write(engine: Engine, org_id: uuid.UUID, sql: str, params: dict[str, Any]) -> None:
    with tenant_session(engine, org_id) as session:
        session.execute(text(sql), params)


def _key(owner: Engine, org_id: uuid.UUID, pepper: str, role: str) -> str:
    issued = issue_key(pepper)
    _write(
        owner,
        org_id,
        "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) VALUES (:i, :o, :p, :h, :r)",
        {"i": uuid.uuid4(), "o": org_id, "p": issued.prefix, "h": issued.key_hash, "r": role},
    )
    return issued.plaintext


def _org(owner: Engine, pepper: str, slug: str) -> Org:
    org_id = uuid.uuid4()
    with owner.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
            {"i": org_id, "s": f"{slug}-{org_id.hex[:8]}", "n": slug},
        )
    model_version_id = uuid.uuid4()
    _write(
        owner,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', :f, "
        ":h, '{}'::jsonb, '{}'::jsonb)",
        {
            "i": model_version_id,
            "o": org_id,
            "v": f"approve-{model_version_id.hex[:8]}",
            "f": FEATURE_SET_VERSION,
            "h": "0" * 64,
        },
    )
    return Org(
        org_id=org_id,
        model_version_id=model_version_id,
        csm=_key(owner, org_id, pepper, "csm"),
        admin=_key(owner, org_id, pepper, "admin"),
        readonly=_key(owner, org_id, pepper, "readonly"),
    )


def _proposed_action(app: Engine, owner: Engine, org: Org) -> uuid.UUID:
    account_id = uuid.uuid4()
    renewal = NOW + timedelta(days=60)
    _write(
        owner,
        org.org_id,
        "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, plan_tier, "
        "region, timezone, renewal_date, created_at) VALUES (:i, :o, 'Acct', :d, 'mid_market', "
        "'growth', 'na', 'UTC', :r, now())",
        {"i": account_id, "o": org.org_id, "d": f"{account_id.hex}.example.test", "r": renewal},
    )
    prediction_id = uuid.uuid4()
    _write(
        owner,
        org.org_id,
        "INSERT INTO predictions (id, org_id, account_id, model_version_id, feature_set_version, "
        "cutoff_at, predicted_at, eligible, renewal_date, risk_score, feature_values, "
        "feature_provenance) VALUES (:p, :o, :a, :m, :f, :c, :c, true, :r, 0.5, "
        "CAST(:v AS jsonb), '{}'::jsonb)",
        {
            "p": prediction_id,
            "o": org.org_id,
            "a": account_id,
            "m": org.model_version_id,
            "f": FEATURE_SET_VERSION,
            "c": NOW,
            "r": renewal,
            "v": json.dumps(VALUES),
        },
    )
    action_id = uuid.uuid4()
    _write(
        app,
        org.org_id,
        "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, action_type, "
        "reason_code, evidence) VALUES (:i, :o, :a, :p, 'csm_outreach_email', 'risk_threshold', "
        "'{}'::jsonb)",
        {"i": action_id, "o": org.org_id, "a": account_id, "p": prediction_id},
    )
    return action_id


@pytest.fixture(scope="module")
def tenant(owner_engine: Engine, pepper: str) -> Org:
    return _org(owner_engine, pepper, "approve-a")


@pytest.fixture(scope="module")
def other_tenant(owner_engine: Engine, pepper: str) -> Org:
    return _org(owner_engine, pepper, "approve-b")


@pytest.fixture(scope="module")
def settings(test_database, pepper: str) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(  # type: ignore[call-arg]
        database_url=test_database.app_url,
        api_key_pepper=pepper,
        _env_file=None,
    )


@pytest.fixture(scope="module")
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _decide(
    client: TestClient,
    action_id: uuid.UUID,
    token: str,
    key: str | None,
    decision: str = "approved",
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {"decision": decision}
    if reason is not None:
        body["reason"] = reason
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"/api/v1/actions/{action_id}/approvals", json=body, headers=headers)


def _state(owner: Engine, org: Org, action_id: uuid.UUID) -> dict[str, Any]:
    with tenant_session(owner, org.org_id) as session:
        status = session.execute(
            text("SELECT status FROM recommended_actions WHERE org_id = :o AND id = :a"),
            {"o": org.org_id, "a": action_id},
        ).scalar_one()
        approvals = (
            session.execute(
                text("SELECT decision FROM action_approvals WHERE org_id = :o AND action_id = :a"),
                {"o": org.org_id, "a": action_id},
            )
            .scalars()
            .all()
        )
        messages = (
            session.execute(
                text("SELECT status FROM outbox_messages WHERE org_id = :o AND action_id = :a"),
                {"o": org.org_id, "a": action_id},
            )
            .scalars()
            .all()
        )
        audits = session.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE org_id = :o AND entity_id = :a "
                "AND event_type LIKE 'action_%'"
            ),
            {"o": org.org_id, "a": str(action_id)},
        ).scalar_one()
    return {
        "status": status,
        "approvals": list(approvals),
        "messages": list(messages),
        "audits": audits,
    }


def test_csm_approval_records_decision_audit_and_one_simulated_message(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(client, action, tenant.csm, "approve-success")

    assert response.status_code == 201
    body = response.json()
    assert body["decision"] == "approved"
    assert body["action_id"] == str(action)
    state = _state(owner_engine, tenant, action)
    assert state["status"] == "approved"
    assert state["approvals"] == ["approved"]
    assert state["audits"] == 1
    assert state["messages"] == ["pending"]
    with tenant_session(owner_engine, tenant.org_id) as session:
        mode = session.execute(
            text("SELECT delivery_mode FROM outbox_messages WHERE org_id = :o AND action_id = :a"),
            {"o": tenant.org_id, "a": action},
        ).scalar_one()
    assert mode == "simulated"


def test_rejection_records_decision_and_creates_no_outbox_message(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(
        client, action, tenant.csm, "reject-success", decision="rejected", reason="not now"
    )

    assert response.status_code == 201
    state = _state(owner_engine, tenant, action)
    assert state["status"] == "rejected"
    assert state["approvals"] == ["rejected"]
    assert state["messages"] == []
    assert state["audits"] == 1


def test_same_key_and_same_request_replays_the_original_result(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    first = _decide(client, action, tenant.csm, "replay-same")
    second = _decide(client, action, tenant.csm, "replay-same")

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    state = _state(owner_engine, tenant, action)
    assert state["approvals"] == ["approved"]
    assert state["messages"] == ["pending"]
    assert state["audits"] == 1


def test_same_key_with_a_different_decision_is_422(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "mismatch-decision").status_code == 201

    response = _decide(client, action, tenant.csm, "mismatch-decision", decision="rejected")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM)
    assert response.json()["type"].endswith("idempotency-key-reused")
    assert _state(owner_engine, tenant, action)["status"] == "approved"


def test_same_key_with_a_different_reason_is_422(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "mismatch-reason", reason="a").status_code == 201

    response = _decide(client, action, tenant.csm, "mismatch-reason", reason="b")

    assert response.status_code == 422


def test_same_key_on_a_different_action_is_422_and_touches_nothing(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    first = _proposed_action(app_engine, owner_engine, tenant)
    second = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, first, tenant.csm, "mismatch-action").status_code == 201

    response = _decide(client, second, tenant.csm, "mismatch-action")

    assert response.status_code == 422
    state = _state(owner_engine, tenant, second)
    assert state["status"] == "proposed"
    assert state["approvals"] == []


def test_expired_key_is_ignored_and_the_request_is_processed(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "expiry-key").status_code == 201
    _write(
        owner_engine,
        tenant.org_id,
        "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' "
        "WHERE org_id = :o AND key = :k",
        {"o": tenant.org_id, "k": "expiry-key"},
    )
    action_two = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(client, action_two, tenant.csm, "expiry-key", decision="rejected")

    assert response.status_code == 201
    assert _state(owner_engine, tenant, action_two)["status"] == "rejected"


def test_idempotency_keys_expire_after_twenty_four_hours(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "ttl-key").status_code == 201

    with tenant_session(owner_engine, tenant.org_id) as session:
        ttl = session.execute(
            text(
                "SELECT expires_at - created_at FROM idempotency_keys "
                "WHERE org_id = :o AND key = :k"
            ),
            {"o": tenant.org_id, "k": "ttl-key"},
        ).scalar_one()
    assert ttl == timedelta(hours=24)


def test_second_decision_with_a_new_key_is_409(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "first-decision").status_code == 201

    response = _decide(client, action, tenant.csm, "second-decision", decision="rejected")

    assert response.status_code == 409
    assert response.json()["type"].endswith("action-already-decided")
    state = _state(owner_engine, tenant, action)
    assert state["status"] == "approved"
    assert state["approvals"] == ["approved"]
    assert state["messages"] == ["pending"]


def test_reject_after_approve_with_the_same_key_is_422_not_409(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    assert _decide(client, action, tenant.csm, "approve-then-reject").status_code == 201

    response = _decide(client, action, tenant.csm, "approve-then-reject", decision="rejected")

    assert response.status_code == 422


def test_concurrent_same_key_requests_record_one_decision(
    settings: Settings, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    barrier = threading.Barrier(2)
    results: list[tuple[int, Any]] = []

    def send() -> None:
        with TestClient(create_app(settings)) as client:
            barrier.wait(timeout=30)
            response = _decide(client, action, tenant.csm, "race-same")
            results.append((response.status_code, response.json()))

    _run_together(send, send)

    assert len(results) == 2
    assert {status for status, _ in results} == {201}
    assert results[0][1] == results[1][1]
    state = _state(owner_engine, tenant, action)
    assert state["approvals"] == ["approved"]
    assert state["messages"] == ["pending"]
    assert state["audits"] == 1


def test_concurrent_different_decisions_record_exactly_one(
    settings: Settings, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int]] = []

    def send(key: str, decision: str) -> None:
        with TestClient(create_app(settings)) as client:
            barrier.wait(timeout=30)
            response = _decide(client, action, tenant.csm, key, decision)
            outcomes.append((decision, response.status_code))

    _run_together(
        lambda: send("race-a", "approved"),
        lambda: send("race-b", "rejected"),
    )

    assert sorted(status for _, status in outcomes) == [201, 409]
    winner = next(decision for decision, status in outcomes if status == 201)
    state = _state(owner_engine, tenant, action)
    assert state["approvals"] == [winner]
    assert state["status"] == winner
    assert len(state["messages"]) == (1 if winner == "approved" else 0)


def _run_together(*targets: Any) -> None:
    threads = [threading.Thread(target=target) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()


def test_readonly_role_is_forbidden_and_writes_nothing(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(client, action, tenant.readonly, "readonly-try")

    assert response.status_code == 403
    state = _state(owner_engine, tenant, action)
    assert state["status"] == "proposed"
    assert state["approvals"] == []


def test_admin_role_can_decide(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(client, action, tenant.admin, "admin-decides")

    assert response.status_code == 201


def test_action_in_another_tenant_is_404_and_untouched(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org, other_tenant: Org
) -> None:
    foreign = _proposed_action(app_engine, owner_engine, other_tenant)

    response = _decide(client, foreign, tenant.csm, "cross-tenant")

    assert response.status_code == 404
    state = _state(owner_engine, other_tenant, foreign)
    assert state["status"] == "proposed"
    assert state["approvals"] == []


def test_key_from_another_tenant_does_not_replay_or_block(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org, other_tenant: Org
) -> None:
    own = _proposed_action(app_engine, owner_engine, tenant)
    foreign = _proposed_action(app_engine, owner_engine, other_tenant)
    assert _decide(client, own, other_tenant.csm, "shared-key-name").status_code == 404
    assert _decide(client, own, tenant.csm, "shared-key-name").status_code == 201

    response = _decide(client, foreign, other_tenant.csm, "shared-key-name")

    assert response.status_code == 201
    assert _state(owner_engine, tenant, own)["status"] == "approved"
    assert _state(owner_engine, other_tenant, foreign)["status"] == "approved"


def test_unknown_action_is_404_with_problem_body(client: TestClient, tenant: Org) -> None:
    response = _decide(client, uuid.uuid4(), tenant.csm, "unknown-action")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)


def test_missing_key_is_422_and_writes_nothing(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = _decide(client, action, tenant.csm, None)

    assert response.status_code == 422
    assert _state(owner_engine, tenant, action)["approvals"] == []


def test_unknown_decision_value_is_422_and_writes_nothing(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = client.post(
        f"/api/v1/actions/{action}/approvals",
        json={"decision": "maybe"},
        headers={"Authorization": f"Bearer {tenant.csm}", "Idempotency-Key": "bad-decision"},
    )

    assert response.status_code == 422
    assert _state(owner_engine, tenant, action)["approvals"] == []


def test_unknown_body_field_is_422(
    client: TestClient, app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    response = client.post(
        f"/api/v1/actions/{action}/approvals",
        json={"decision": "approved", "status": "approved"},
        headers={"Authorization": f"Bearer {tenant.csm}", "Idempotency-Key": "extra-field"},
    )

    assert response.status_code == 422
    assert _state(owner_engine, tenant, action)["status"] == "proposed"


def test_database_refuses_an_outbox_message_for_a_rejected_action(
    app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    approval_id = uuid.uuid4()
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, 'rejected', :actor)",
        {"i": approval_id, "o": tenant.org_id, "a": action, "actor": uuid.uuid4()},
    )
    _write(
        app_engine,
        tenant.org_id,
        "UPDATE recommended_actions SET status = 'rejected' WHERE org_id = :o AND id = :a",
        {"o": tenant.org_id, "a": action},
    )

    with pytest.raises(Exception, match="outbox message requires an approved action"):
        _write(
            app_engine,
            tenant.org_id,
            "INSERT INTO outbox_messages (id, org_id, action_id, approval_id, channel, "
            "delivery_mode, payload, payload_sha256) VALUES (:i, :o, :a, :ap, 'email', "
            "'simulated', '{}'::jsonb, :h)",
            {
                "i": uuid.uuid4(),
                "o": tenant.org_id,
                "a": action,
                "ap": approval_id,
                "h": "0" * 64,
            },
        )


def test_database_refuses_an_outbox_message_for_a_proposed_action(
    app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    approval_id = uuid.uuid4()
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, 'approved', :actor)",
        {"i": approval_id, "o": tenant.org_id, "a": action, "actor": uuid.uuid4()},
    )

    with pytest.raises(Exception, match="outbox message requires an approved action"):
        _write(
            app_engine,
            tenant.org_id,
            "INSERT INTO outbox_messages (id, org_id, action_id, approval_id, channel, "
            "delivery_mode, payload, payload_sha256) VALUES (:i, :o, :a, :ap, 'email', "
            "'simulated', '{}'::jsonb, :h)",
            {
                "i": uuid.uuid4(),
                "o": tenant.org_id,
                "a": action,
                "ap": approval_id,
                "h": "0" * 64,
            },
        )


def test_database_refuses_a_status_change_without_a_matching_decision(
    app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)

    with pytest.raises(Exception, match="needs a matching approval decision"):
        _write(
            app_engine,
            tenant.org_id,
            "UPDATE recommended_actions SET status = 'approved' WHERE org_id = :o AND id = :a",
            {"o": tenant.org_id, "a": action},
        )
    assert _state(owner_engine, tenant, action)["status"] == "proposed"


def test_database_refuses_a_second_decision_on_a_decided_action(
    app_engine: Engine, owner_engine: Engine, tenant: Org
) -> None:
    action = _proposed_action(app_engine, owner_engine, tenant)
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, 'approved', :actor)",
        {"i": uuid.uuid4(), "o": tenant.org_id, "a": action, "actor": uuid.uuid4()},
    )
    _write(
        app_engine,
        tenant.org_id,
        "UPDATE recommended_actions SET status = 'approved' WHERE org_id = :o AND id = :a",
        {"o": tenant.org_id, "a": action},
    )

    with pytest.raises(Exception, match="needs a proposed action"):
        _write(
            app_engine,
            tenant.org_id,
            "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
            "VALUES (:i, :o, :a, 'rejected', :actor)",
            {"i": uuid.uuid4(), "o": tenant.org_id, "a": action, "actor": uuid.uuid4()},
        )


def test_failed_decision_leaves_no_partial_rows(
    settings: Settings,
    app_engine: Engine,
    owner_engine: Engine,
    tenant: Org,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import approvals

    action = _proposed_action(app_engine, owner_engine, tenant)

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("simulated failure after the approval row was written")

    monkeypatch.setattr(approvals, "insert_outbox_message", explode)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = _decide(client, action, tenant.csm, "partial-write")

    assert response.status_code == 500
    assert _state(owner_engine, tenant, action) == {
        "status": "proposed",
        "approvals": [],
        "messages": [],
        "audits": 0,
    }
    with tenant_session(owner_engine, tenant.org_id) as session:
        stored = session.execute(
            text(
                "SELECT count(*) FROM idempotency_keys WHERE org_id = :o AND key = 'partial-write'"
            ),
            {"o": tenant.org_id},
        ).scalar_one()
    assert stored == 0
