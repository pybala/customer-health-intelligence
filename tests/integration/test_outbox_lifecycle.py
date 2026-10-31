"""Outbox lifecycle on isolated tenants: claims, crashes, retries, suppression and lock loss."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.features.definitions import FEATURE_SET_VERSION
from app.outbox.store import (
    claim_due,
    enqueue_approved,
    mark_failed,
    mark_sent,
    recover_stale_locks,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
LOCK_FOR = timedelta(minutes=10)
WORKER = "worker-a"


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    model_version_id: uuid.UUID


@pytest.fixture
def tenant(owner_engine: Engine) -> Tenant:
    org_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Outbox')"),
            {"i": org_id, "s": f"outbox-{org_id.hex[:10]}"},
        )
    _write(
        owner_engine,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', :f, "
        ":h, '{}'::jsonb, '{}'::jsonb)",
        {
            "i": model_version_id,
            "o": org_id,
            "v": f"m-{model_version_id.hex[:8]}",
            "f": FEATURE_SET_VERSION,
            "h": "0" * 64,
        },
    )
    return Tenant(org_id, model_version_id)


def _write(engine: Engine, org_id: uuid.UUID, sql: str, params: dict[str, Any]) -> None:
    with tenant_session(engine, org_id) as session:
        session.execute(text(sql), params)


def _account(owner_engine: Engine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    _write(
        owner_engine,
        org_id,
        "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, plan_tier, "
        "region, timezone, created_at) VALUES (:i, :o, 'Acct', :d, 'smb', 'starter', 'na', 'UTC', "
        "now())",
        {"i": account_id, "o": org_id, "d": f"{account_id.hex}.example.test"},
    )
    return account_id


def _prediction(owner_engine: Engine, tenant: Tenant, account_id: uuid.UUID) -> uuid.UUID:
    prediction_id = uuid.uuid4()
    _write(
        owner_engine,
        tenant.org_id,
        "INSERT INTO predictions (id, org_id, account_id, model_version_id, feature_set_version, "
        "cutoff_at, eligible, risk_score, feature_values, feature_provenance) VALUES (:p, :o, :a, "
        ":m, :f, :c, true, 0.5, '{}'::jsonb, '{}'::jsonb)",
        {
            "p": prediction_id,
            "o": tenant.org_id,
            "a": account_id,
            "m": tenant.model_version_id,
            "f": FEATURE_SET_VERSION,
            "c": T0,
        },
    )
    return prediction_id


def _action(
    app_engine: Engine,
    owner_engine: Engine,
    tenant: Tenant,
    account_id: uuid.UUID,
    decision: str = "approved",
) -> uuid.UUID:
    """A proposed action decided by a human; the approval row is inserted before the status."""
    prediction_id = _prediction(owner_engine, tenant, account_id)
    action_id = uuid.uuid4()
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, action_type, "
        "reason_code, evidence) VALUES (:i, :o, :a, :p, 'csm_outreach_email', 'risk_threshold', "
        "'{}'::jsonb)",
        {"i": action_id, "o": tenant.org_id, "a": account_id, "p": prediction_id},
    )
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, :d, :actor)",
        {
            "i": uuid.uuid4(),
            "o": tenant.org_id,
            "a": action_id,
            "d": decision,
            "actor": uuid.uuid4(),
        },
    )
    _write(
        app_engine,
        tenant.org_id,
        "UPDATE recommended_actions SET status = :d WHERE org_id = :o AND id = :a",
        {"d": decision, "o": tenant.org_id, "a": action_id},
    )
    return action_id


def _message(owner_engine: Engine, tenant: Tenant, action_id: uuid.UUID) -> Any:
    with tenant_session(owner_engine, tenant.org_id) as session:
        return session.execute(
            text(
                "SELECT id, status, attempts, locked_by, next_attempt_at, last_error, finished_at "
                "FROM outbox_messages WHERE org_id = :o AND action_id = :a"
            ),
            {"o": tenant.org_id, "a": action_id},
        ).one()


def _claim(app_engine: Engine, tenant: Tenant, at: datetime, worker: str = WORKER) -> list[Any]:
    claimed, _ = claim_due(app_engine, tenant.org_id, worker, at, limit=10, lock_for=LOCK_FOR)
    return claimed


def test_simulated_message_is_claimed_then_sent(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    claimed, terminal = claim_due(
        app_engine, tenant.org_id, WORKER, T0, limit=10, lock_for=LOCK_FOR
    )

    assert terminal == {"suppressed": 0, "dry_run_recorded": 0}
    [message] = claimed
    during = _message(owner_engine, tenant, action_id)
    assert (during.status, during.attempts, during.locked_by) == ("sending", 1, WORKER)

    assert mark_sent(app_engine, tenant.org_id, message.id, WORKER, T0) is True
    after = _message(owner_engine, tenant, action_id)
    assert (after.status, after.attempts, after.locked_by) == ("sent", 1, None)
    assert after.finished_at == T0


def test_expired_lock_is_requeued_and_keeps_the_attempt_count(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)
    [first] = _claim(app_engine, tenant, T0, worker="crashed")

    almost = T0 + LOCK_FOR - timedelta(seconds=1)
    assert recover_stale_locks(app_engine, tenant.org_id, almost) == {
        "requeued": 0,
        "dead_lettered": 0,
    }
    expired = T0 + LOCK_FOR
    assert recover_stale_locks(app_engine, tenant.org_id, expired) == {
        "requeued": 1,
        "dead_lettered": 0,
    }
    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts, row.locked_by) == ("pending", 1, None)
    assert row.last_error == "stale lock recovered"

    [second] = _claim(app_engine, tenant, expired, worker="recovery")
    assert second.id == first.id
    assert second.attempts == 2


def test_expired_lock_at_the_attempt_limit_is_dead_lettered(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    now = T0
    for attempt in range(1, 6):
        [message] = _claim(app_engine, tenant, now, worker="crashing")
        assert message.attempts == attempt
        now += LOCK_FOR
        expected = {"requeued": 1, "dead_lettered": 0}
        if attempt == 5:
            expected = {"requeued": 0, "dead_lettered": 1}
        assert recover_stale_locks(app_engine, tenant.org_id, now) == expected

    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts) == ("dead_letter", 5)
    assert row.finished_at is not None


def test_failed_delivery_backs_off_then_dead_letters_at_the_limit(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    now = T0
    for attempt in range(1, 6):
        [message] = _claim(app_engine, tenant, now)
        assert message.attempts == attempt
        outcome = mark_failed(app_engine, tenant.org_id, message, WORKER, "smtp 451", now)
        if attempt < 5:
            assert outcome == "pending"
            assert _claim(app_engine, tenant, now) == []
        else:
            assert outcome == "dead_letter"
        now += timedelta(hours=2)

    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts, row.last_error) == ("dead_letter", 5, "smtp 451")
    assert row.finished_at is not None


def test_worker_that_lost_its_lock_cannot_overwrite_the_result(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)
    [stale] = _claim(app_engine, tenant, T0, worker="worker-a")
    later = T0 + LOCK_FOR
    recover_stale_locks(app_engine, tenant.org_id, later)
    [current] = _claim(app_engine, tenant, later, worker="worker-b")

    assert mark_sent(app_engine, tenant.org_id, stale.id, "worker-a", later) is False
    assert mark_failed(app_engine, tenant.org_id, stale, "worker-a", "late", later) is None

    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts, row.locked_by) == ("sending", 2, "worker-b")
    assert mark_sent(app_engine, tenant.org_id, current.id, "worker-b", later) is True


@pytest.mark.parametrize("reason", ["legal_hold", "critical_escalation"])
def test_suppression_makes_a_due_message_terminal_without_delivery(
    reason: str, app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    _write(
        app_engine,
        tenant.org_id,
        "INSERT INTO suppressions (id, org_id, account_id, reason, starts_at, created_by) "
        "VALUES (:i, :o, :a, :r, :s, :c)",
        {
            "i": uuid.uuid4(),
            "o": tenant.org_id,
            "a": account_id,
            "r": reason,
            "s": T0 - timedelta(days=1),
            "c": uuid.uuid4(),
        },
    )
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    claimed, terminal = claim_due(
        app_engine, tenant.org_id, WORKER, T0, limit=10, lock_for=LOCK_FOR
    )

    assert claimed == []
    assert terminal == {"suppressed": 1, "dry_run_recorded": 0}
    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts, row.last_error) == ("suppressed", 0, reason)
    assert row.finished_at == T0


def test_recent_contact_suppresses_the_next_message_for_seven_days(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)
    [sent] = _claim(app_engine, tenant, T0)
    assert mark_sent(app_engine, tenant.org_id, sent.id, WORKER, T0) is True

    next_day = T0 + timedelta(days=1)
    within = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", next_day)
    assert _claim(app_engine, tenant, next_day) == []
    row = _message(owner_engine, tenant, within)
    assert (row.status, row.last_error) == ("suppressed", "recent_contact")

    after_cooldown = T0 + timedelta(days=8)
    outside = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", after_cooldown)
    [due] = _claim(app_engine, tenant, after_cooldown)
    assert due.id == _message(owner_engine, tenant, outside).id


def test_dry_run_message_is_recorded_and_never_claimed(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    account_id = _account(owner_engine, tenant.org_id)
    action_id = _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "dry_run", T0)

    claimed, terminal = claim_due(
        app_engine, tenant.org_id, WORKER, T0, limit=10, lock_for=LOCK_FOR
    )

    assert claimed == []
    assert terminal == {"suppressed": 0, "dry_run_recorded": 1}
    row = _message(owner_engine, tenant, action_id)
    assert (row.status, row.attempts, row.finished_at) == ("dry_run_recorded", 0, T0)


def test_claim_skips_a_message_row_locked_by_another_transaction(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    for _ in range(2):
        account_id = _account(owner_engine, tenant.org_id)
        _action(app_engine, owner_engine, tenant, account_id)
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    with tenant_session(app_engine, tenant.org_id) as session:
        held = session.execute(
            text("SELECT id FROM outbox_messages WHERE org_id = :o ORDER BY id LIMIT 1 FOR UPDATE"),
            {"o": tenant.org_id},
        ).scalar_one()
        claimed = _claim(app_engine, tenant, T0)
        assert held not in [m.id for m in claimed]
        assert len(claimed) == 1

    assert [m.id for m in _claim(app_engine, tenant, T0, worker="later")] == [held]


def test_two_connections_claim_disjoint_messages_under_contention(
    app_engine: Engine, owner_engine: Engine, tenant: Tenant
) -> None:
    action_ids = []
    for _ in range(30):
        account_id = _account(owner_engine, tenant.org_id)
        action_ids.append(_action(app_engine, owner_engine, tenant, account_id))
    enqueue_approved(app_engine, tenant.org_id, "simulated", T0)

    barrier = threading.Barrier(2)
    claims: dict[str, list[uuid.UUID]] = {"a": [], "b": []}

    def drain(name: str) -> None:
        barrier.wait(timeout=30)
        while True:
            batch, _ = claim_due(
                app_engine, tenant.org_id, f"worker-{name}", T0, limit=3, lock_for=LOCK_FOR
            )
            if not batch:
                return
            claims[name].extend(m.id for m in batch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(drain, name) for name in ("a", "b")]
        for future in futures:
            future.result(timeout=60)

    combined = claims["a"] + claims["b"]
    assert len(combined) == len(set(combined)) == 30
    with tenant_session(owner_engine, tenant.org_id) as session:
        rows = session.execute(
            text(
                "SELECT status, attempts, count(*) AS n FROM outbox_messages "
                "WHERE org_id = :o GROUP BY status, attempts"
            ),
            {"o": tenant.org_id},
        ).all()
    assert [(r.status, r.attempts, r.n) for r in rows] == [("sending", 1, 30)]
