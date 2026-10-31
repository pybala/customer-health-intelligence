"""Actions, approvals, outbox, suppressions and worker runs, enforced by migration 0006.

Each rule is checked with direct SQL under the real roles. The rules live in constraints,
triggers and grants, so a future worker or handler that tries to skip them still fails here.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db.session import tenant_session

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    account_id: uuid.UUID
    prediction_id: uuid.UUID
    model_version_id: uuid.UUID


def _new_org(owner_engine: Engine) -> uuid.UUID:
    org_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Actions')"),
            {"i": org_id, "s": f"actions-{org_id.hex[:10]}"},
        )
    return org_id


def _run(
    engine: Engine, org_id: uuid.UUID, sql: str, params: dict[str, object] | None = None
) -> None:
    with tenant_session(engine, org_id) as session:
        session.execute(text(sql), params or {})


def _seed_account_and_prediction(
    owner_engine: Engine, org_id: uuid.UUID, model_version_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    prediction_id = uuid.uuid4()
    _run(
        owner_engine,
        org_id,
        "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, plan_tier, "
        "region, timezone, created_at) VALUES (:i, :o, 'Acme', :d, 'smb', 'starter', 'na', 'UTC', "
        "now())",
        {"i": account_id, "o": org_id, "d": f"{account_id.hex}.example.test"},
    )
    _run(
        owner_engine,
        org_id,
        "INSERT INTO predictions (id, org_id, account_id, model_version_id, feature_set_version, "
        "cutoff_at, eligible, risk_score, feature_values, feature_provenance) VALUES (:p, :o, :a, "
        ":m, 'features-v1', now(), true, 0.5, '{}'::jsonb, '{}'::jsonb)",
        {"p": prediction_id, "o": org_id, "a": account_id, "m": model_version_id},
    )
    return account_id, prediction_id


@pytest.fixture
def tenant(owner_engine: Engine) -> Tenant:
    org_id = _new_org(owner_engine)
    model_version_id = uuid.uuid4()
    _run(
        owner_engine,
        org_id,
        "INSERT INTO model_versions (id, org_id, model_version, model_kind, feature_set_version, "
        "artifact_sha256, training_window, metadata) VALUES (:i, :o, :v, 'constant_prior', "
        "'features-v1', :h, '{}'::jsonb, '{}'::jsonb)",
        {"i": model_version_id, "o": org_id, "v": f"m-{model_version_id.hex[:8]}", "h": "0" * 64},
    )
    account_id, prediction_id = _seed_account_and_prediction(owner_engine, org_id, model_version_id)
    return Tenant(org_id, account_id, prediction_id, model_version_id)


def _propose(
    engine: Engine,
    tenant: Tenant,
    prediction_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    action_id = uuid.uuid4()
    _run(
        engine,
        tenant.org_id,
        "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, action_type, "
        "reason_code, evidence) VALUES (:i, :o, :a, :p, 'csm_outreach_email', 'risk_threshold', "
        "'{}'::jsonb)",
        {
            "i": action_id,
            "o": tenant.org_id,
            "a": account_id or tenant.account_id,
            "p": prediction_id or tenant.prediction_id,
        },
    )
    return action_id


def _record_decision(
    engine: Engine, tenant: Tenant, action_id: uuid.UUID, decision: str = "approved"
) -> uuid.UUID:
    approval_id = uuid.uuid4()
    _run(
        engine,
        tenant.org_id,
        "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
        "VALUES (:i, :o, :a, :d, :actor)",
        {
            "i": approval_id,
            "o": tenant.org_id,
            "a": action_id,
            "d": decision,
            "actor": uuid.uuid4(),
        },
    )
    return approval_id


def _set_status(engine: Engine, tenant: Tenant, action_id: uuid.UUID, status: str) -> None:
    _run(
        engine,
        tenant.org_id,
        "UPDATE recommended_actions SET status = :s WHERE id = :a",
        {"s": status, "a": action_id},
    )


def _approve(engine: Engine, tenant: Tenant, action_id: uuid.UUID) -> uuid.UUID:
    approval_id = _record_decision(engine, tenant, action_id, "approved")
    _set_status(engine, tenant, action_id, "approved")
    return approval_id


def _queue(
    engine: Engine,
    tenant: Tenant,
    action_id: uuid.UUID,
    approval_id: uuid.UUID,
    mode: str = "dry_run",
    max_attempts: int = 5,
) -> uuid.UUID:
    outbox_id = uuid.uuid4()
    _run(
        engine,
        tenant.org_id,
        "INSERT INTO outbox_messages (id, org_id, action_id, approval_id, channel, delivery_mode, "
        "payload, payload_sha256, max_attempts) VALUES (:i, :o, :a, :ap, 'email', :m, "
        "'{}'::jsonb, :h, :x)",
        {
            "i": outbox_id,
            "o": tenant.org_id,
            "a": action_id,
            "ap": approval_id,
            "m": mode,
            "h": "1" * 64,
            "x": max_attempts,
        },
    )
    return outbox_id


def _claim(engine: Engine, tenant: Tenant, outbox_id: uuid.UUID) -> None:
    _run(
        engine,
        tenant.org_id,
        "UPDATE outbox_messages SET status = 'sending', attempts = attempts + 1, "
        "locked_by = 'worker-1', locked_until = now() + interval '5 minutes' WHERE id = :i",
        {"i": outbox_id},
    )


def _release(engine: Engine, tenant: Tenant, outbox_id: uuid.UUID) -> None:
    _run(
        engine,
        tenant.org_id,
        "UPDATE outbox_messages SET status = 'pending', locked_by = NULL, locked_until = NULL, "
        "last_error = 'timeout' WHERE id = :i",
        {"i": outbox_id},
    )


def _finish(engine: Engine, tenant: Tenant, outbox_id: uuid.UUID, status: str) -> None:
    _run(
        engine,
        tenant.org_id,
        "UPDATE outbox_messages SET status = :s, locked_by = NULL, locked_until = NULL, "
        "finished_at = now() WHERE id = :i",
        {"s": status, "i": outbox_id},
    )


# Approval lifecycle


def test_outbox_message_is_refused_while_its_action_is_only_proposed(
    app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    approval = _record_decision(app_engine, tenant, action, "approved")

    with pytest.raises(DBAPIError, match="requires an approved action"):
        _queue(app_engine, tenant, action, approval)

    _set_status(app_engine, tenant, action, "approved")
    _queue(app_engine, tenant, action, approval)


def test_outbox_message_is_refused_for_a_rejected_action(
    app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    approval = _record_decision(app_engine, tenant, action, "rejected")
    _set_status(app_engine, tenant, action, "rejected")

    with pytest.raises(DBAPIError, match="requires an approved action"):
        _queue(app_engine, tenant, action, approval)


def test_action_cannot_become_approved_without_a_decision(
    app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)

    with pytest.raises(DBAPIError, match="needs a matching approval decision"):
        _set_status(app_engine, tenant, action, "approved")


def test_decision_must_match_the_status_it_sets(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    _record_decision(app_engine, tenant, action, "approved")

    with pytest.raises(DBAPIError, match="needs a matching approval decision"):
        _set_status(app_engine, tenant, action, "rejected")


def test_approval_is_refused_for_an_action_that_is_not_proposed(
    app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    _set_status(app_engine, tenant, action, "superseded")

    with pytest.raises(DBAPIError, match="needs a proposed action"):
        _record_decision(app_engine, tenant, action, "approved")


def test_only_one_decision_is_recorded_per_action(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    _record_decision(app_engine, tenant, action, "approved")

    with pytest.raises(DBAPIError, match="uq_action_one_decision"):
        _record_decision(app_engine, tenant, action, "rejected")


def test_status_cannot_change_after_a_decision(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    _approve(app_engine, tenant, action)

    with pytest.raises(DBAPIError, match="status cannot change"):
        _set_status(app_engine, tenant, action, "rejected")


def test_action_content_cannot_be_rewritten_by_the_owner(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)

    with pytest.raises(DBAPIError, match="content is immutable"):
        _run(
            owner_engine,
            tenant.org_id,
            "UPDATE recommended_actions SET evidence = '{\"tampered\": true}'::jsonb WHERE id = :a",
            {"a": action},
        )


def test_app_role_cannot_rewrite_action_content(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE recommended_actions SET reason_code = 'tampered' WHERE id = :a",
            {"a": action},
        )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE action_approvals SET decision = 'rejected'", "DELETE FROM action_approvals"],
)
def test_app_role_cannot_change_or_remove_approvals(
    app_engine: Engine, tenant: Tenant, statement: str
) -> None:
    action = _propose(app_engine, tenant)
    _record_decision(app_engine, tenant, action, "approved")

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(app_engine, tenant.org_id, statement)


def test_owner_cannot_change_an_approval_either(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    _record_decision(app_engine, tenant, action, "approved")

    with pytest.raises(DBAPIError, match="action_approvals is append-only"):
        _run(owner_engine, tenant.org_id, "UPDATE action_approvals SET decision = 'rejected'")


# Outbox lifecycle


def test_approved_message_is_sent_once_and_stays_sent(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval, mode="simulated")

    _claim(app_engine, tenant, outbox)
    _finish(app_engine, tenant, outbox, "sent")

    with pytest.raises(DBAPIError, match="is terminal"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE outbox_messages SET status = 'pending' WHERE id = :i",
            {"i": outbox},
        )


def test_dry_run_is_recorded_without_claiming_a_send(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval, mode="dry_run")

    _finish(app_engine, tenant, outbox, "dry_run_recorded")


def test_dry_run_can_never_be_recorded_as_sent(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval, mode="dry_run")
    _claim(app_engine, tenant, outbox)

    with pytest.raises(DBAPIError, match="ck_outbox_mode_matches_status"):
        _finish(app_engine, tenant, outbox, "sent")


def test_claim_must_take_a_lock(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval)

    with pytest.raises(DBAPIError, match="ck_outbox_lock_iff_sending"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE outbox_messages SET status = 'sending', attempts = attempts + 1 WHERE id = :i",
            {"i": outbox},
        )


def test_retries_are_bounded_then_dead_lettered(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval, mode="simulated", max_attempts=2)

    _claim(app_engine, tenant, outbox)
    _release(app_engine, tenant, outbox)
    _claim(app_engine, tenant, outbox)

    with pytest.raises(DBAPIError, match="retry is allowed only below"):
        _release(app_engine, tenant, outbox)

    _finish(app_engine, tenant, outbox, "dead_letter")


def test_dead_letter_is_refused_before_the_attempt_limit(
    app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval, mode="simulated", max_attempts=3)
    _claim(app_engine, tenant, outbox)

    with pytest.raises(DBAPIError, match="dead letter is allowed only at the maximum"):
        _finish(app_engine, tenant, outbox, "dead_letter")


def test_outbox_content_cannot_be_changed_by_the_owner(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval)

    with pytest.raises(DBAPIError, match="content is immutable"):
        _run(
            owner_engine,
            tenant.org_id,
            'UPDATE outbox_messages SET payload = \'{"to": "someone"}\'::jsonb WHERE id = :i',
            {"i": outbox},
        )


def test_app_role_cannot_change_outbox_payload(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    outbox = _queue(app_engine, tenant, action, approval)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE outbox_messages SET payload = '{}'::jsonb WHERE id = :i",
            {"i": outbox},
        )


def test_outbox_message_must_reference_its_own_approval(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    approved = _propose(app_engine, tenant)
    _approve(app_engine, tenant, approved)
    other_account, other_prediction = _seed_account_and_prediction(
        owner_engine, tenant.org_id, tenant.model_version_id
    )
    other = _propose(app_engine, tenant, prediction_id=other_prediction, account_id=other_account)
    other_approval = _approve(app_engine, tenant, other)

    with pytest.raises(DBAPIError, match="requires an approved action|foreign key"):
        _queue(app_engine, tenant, approved, other_approval)


def test_action_must_point_at_a_prediction_for_its_own_account(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    _, other_prediction = _seed_account_and_prediction(
        owner_engine, tenant.org_id, tenant.model_version_id
    )

    with pytest.raises(DBAPIError, match="foreign key"):
        _propose(app_engine, tenant, prediction_id=other_prediction, account_id=tenant.account_id)


def test_one_outbox_message_per_action(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    _queue(app_engine, tenant, action, approval)

    with pytest.raises(DBAPIError, match="uq_outbox_one_message_per_action"):
        _queue(app_engine, tenant, action, approval)


# Suppressions


def _insert_suppression(engine: Engine, tenant: Tenant, reason: str = "legal_hold") -> uuid.UUID:
    suppression_id = uuid.uuid4()
    _run(
        engine,
        tenant.org_id,
        "INSERT INTO suppressions (id, org_id, account_id, reason, created_by) "
        "VALUES (:i, :o, :a, :r, :actor)",
        {
            "i": suppression_id,
            "o": tenant.org_id,
            "a": tenant.account_id,
            "r": reason,
            "actor": uuid.uuid4(),
        },
    )
    return suppression_id


def test_only_one_open_legal_hold_per_account(app_engine: Engine, tenant: Tenant) -> None:
    _insert_suppression(app_engine, tenant, "legal_hold")

    with pytest.raises(DBAPIError, match="uq_open_legal_hold"):
        _insert_suppression(app_engine, tenant, "legal_hold")


def test_suppressions_cannot_be_deleted_by_the_app_role(app_engine: Engine, tenant: Tenant) -> None:
    suppression = _insert_suppression(app_engine, tenant)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(
            app_engine, tenant.org_id, "DELETE FROM suppressions WHERE id = :i", {"i": suppression}
        )


def test_suppressions_cannot_be_deleted_by_the_owner(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    suppression = _insert_suppression(app_engine, tenant)

    with pytest.raises(DBAPIError, match="cannot be deleted"):
        _run(
            owner_engine,
            tenant.org_id,
            "DELETE FROM suppressions WHERE id = :i",
            {"i": suppression},
        )


def test_suppression_reason_cannot_be_edited(app_engine: Engine, tenant: Tenant) -> None:
    suppression = _insert_suppression(app_engine, tenant)

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE suppressions SET reason = 'recent_contact' WHERE id = :i",
            {"i": suppression},
        )


def test_legal_hold_is_released_once_with_an_actor(app_engine: Engine, tenant: Tenant) -> None:
    suppression = _insert_suppression(app_engine, tenant)

    with pytest.raises(DBAPIError, match="ck_suppression_release"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE suppressions SET released_at = now() WHERE id = :i",
            {"i": suppression},
        )

    _run(
        app_engine,
        tenant.org_id,
        "UPDATE suppressions SET released_at = now(), released_by = :actor WHERE id = :i",
        {"i": suppression, "actor": uuid.uuid4()},
    )

    with pytest.raises(DBAPIError, match="already released"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE suppressions SET released_at = now(), released_by = :actor WHERE id = :i",
            {"i": suppression, "actor": uuid.uuid4()},
        )


def test_a_new_legal_hold_is_allowed_after_release(app_engine: Engine, tenant: Tenant) -> None:
    first = _insert_suppression(app_engine, tenant)
    _run(
        app_engine,
        tenant.org_id,
        "UPDATE suppressions SET released_at = now(), released_by = :actor WHERE id = :i",
        {"i": first, "actor": uuid.uuid4()},
    )

    _insert_suppression(app_engine, tenant, "legal_hold")


# Worker runs


def test_worker_run_finishes_once(app_engine: Engine, tenant: Tenant) -> None:
    run_id = uuid.uuid4()
    _run(
        app_engine,
        tenant.org_id,
        "INSERT INTO worker_runs (id, org_id, mode) VALUES (:i, :o, 'dry_run')",
        {"i": run_id, "o": tenant.org_id},
    )
    _run(
        app_engine,
        tenant.org_id,
        "UPDATE worker_runs SET status = 'succeeded', finished_at = now() WHERE id = :i",
        {"i": run_id},
    )

    with pytest.raises(DBAPIError, match="already finished"):
        _run(
            app_engine,
            tenant.org_id,
            "UPDATE worker_runs SET status = 'failed', finished_at = now() WHERE id = :i",
            {"i": run_id},
        )


def test_worker_run_cannot_be_deleted_by_the_app_role(app_engine: Engine, tenant: Tenant) -> None:
    run_id = uuid.uuid4()
    _run(
        app_engine,
        tenant.org_id,
        "INSERT INTO worker_runs (id, org_id, mode) VALUES (:i, :o, 'dry_run')",
        {"i": run_id, "o": tenant.org_id},
    )

    with pytest.raises(ProgrammingError, match="permission denied"):
        _run(app_engine, tenant.org_id, "DELETE FROM worker_runs WHERE id = :i", {"i": run_id})


# Tenant isolation over the whole lifecycle


def _seed_lifecycle(app_engine: Engine, tenant: Tenant) -> None:
    action = _propose(app_engine, tenant)
    approval = _approve(app_engine, tenant, action)
    _queue(app_engine, tenant, action, approval)
    _insert_suppression(app_engine, tenant)
    _run(
        app_engine,
        tenant.org_id,
        "INSERT INTO worker_runs (id, org_id, mode) VALUES (:i, :o, 'dry_run')",
        {"i": uuid.uuid4(), "o": tenant.org_id},
    )


LIFECYCLE_COUNTS = (
    "SELECT count(*) FROM recommended_actions",
    "SELECT count(*) FROM action_approvals",
    "SELECT count(*) FROM outbox_messages",
    "SELECT count(*) FROM suppressions",
    "SELECT count(*) FROM worker_runs",
)


def test_another_tenant_sees_none_of_the_lifecycle_rows(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    _seed_lifecycle(app_engine, tenant)
    other_org = _new_org(owner_engine)

    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(other_org)}
        )
        counts = [conn.execute(text(query)).scalar_one() for query in LIFECYCLE_COUNTS]

    assert counts == [0, 0, 0, 0, 0]


def test_the_owner_is_also_subject_to_isolation_on_new_tables(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    _seed_lifecycle(app_engine, tenant)

    with owner_engine.connect() as conn, conn.begin():
        counts = [conn.execute(text(query)).scalar_one() for query in LIFECYCLE_COUNTS]

    assert counts == [0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, action_type, "
        "reason_code, evidence) VALUES (:i, :b, :a, :p, 'csm_outreach_email', 'x', '{}'::jsonb)",
        "INSERT INTO suppressions (id, org_id, account_id, reason, created_by) "
        "VALUES (:i, :b, :a, 'legal_hold', :p)",
        "INSERT INTO worker_runs (id, org_id, mode) VALUES (:i, :b, 'dry_run')",
    ],
)
def test_cross_tenant_insert_is_rejected(
    owner_engine: Engine,
    app_engine: Engine,
    tenant: Tenant,
    sql: str,
) -> None:
    other_org = _new_org(owner_engine)
    with pytest.raises(DBAPIError, match="row-level security"):
        _run(
            app_engine,
            tenant.org_id,
            sql,
            {"i": uuid.uuid4(), "b": other_org, "a": tenant.account_id, "p": uuid.uuid4()},
        )


def test_cross_tenant_approval_insert_is_rejected(
    owner_engine: Engine, app_engine: Engine, tenant: Tenant
) -> None:
    other_org = _new_org(owner_engine)
    action = _propose(app_engine, tenant)
    with pytest.raises(DBAPIError, match="row-level security|needs a proposed action"):
        _run(
            app_engine,
            tenant.org_id,
            "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
            "VALUES (:i, :b, :a, 'approved', :actor)",
            {"i": uuid.uuid4(), "b": other_org, "a": action, "actor": uuid.uuid4()},
        )
