"""Worker runs on seeded canonical data: run tracking, idempotent reruns, and the approval gate."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.db.session import tenant_session
from app.features.definitions import FEATURE_SET_VERSION
from app.ingestion.canonical import org_id_for
from app.ml.artifact import LoadedModel, save_artifact
from app.ml.registry import mark_registered, register_version
from app.outbox.store import ClaimedMessage
from app.worker.run import OrgRun, RunOptions, run_once
from tests.ml.conftest import SeededAccount

pytestmark = pytest.mark.integration

ACME = org_id_for("acme-saas")
ANCHOR = datetime(2026, 10, 1, tzinfo=UTC)
NOW = ANCHOR + timedelta(hours=12)


@pytest.fixture(scope="module")
def artifact_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("worker-models")


@pytest.fixture(scope="module")
def acme_model(
    owner_engine: Engine,
    fitted_model: LoadedModel,
    artifact_root: Path,
    seeded: list[SeededAccount],
) -> str:
    version = f"worker-{uuid.uuid4().hex[:8]}"
    digest = save_artifact(
        artifact_root / version, fitted_model.pipeline, {"model_version": version}
    )
    version_id = register_version(
        owner_engine,
        ACME,
        model_version=version,
        model_kind="logistic",
        feature_set_version=FEATURE_SET_VERSION,
        artifact_sha256=digest,
        training_window={"start": "2024-01-01", "end": "2026-09-30"},
        metadata={},
    )
    mark_registered(owner_engine, ACME, version_id)
    return version


def _run(app_engine: Engine, artifact_root: Path, mode: str, now: datetime) -> OrgRun:
    options = RunOptions(
        mode=mode,
        artifact_root=artifact_root,
        capacity_fraction=0.10,
        worker_id="test-worker",
    )
    [result] = run_once(app_engine, [ACME], options, now)
    return result


def _scalar(owner_engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with tenant_session(owner_engine, ACME) as session:
        return session.execute(text(sql), {"o": ACME, **(params or {})}).scalar_one()


def _org_totals(owner_engine: Engine) -> tuple[int, int, int]:
    return (
        _scalar(owner_engine, "SELECT count(*) FROM predictions WHERE org_id = :o"),
        _scalar(owner_engine, "SELECT count(*) FROM recommended_actions WHERE org_id = :o"),
        _scalar(owner_engine, "SELECT count(*) FROM outbox_messages WHERE org_id = :o"),
    )


def _first_proposed(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, model: str
) -> uuid.UUID:
    """A proposed action from a fresh cutoff day.

    One action per prediction is the rule, so earlier tests that decided the proposals at the
    anchor leave nothing open there. Later cutoff days produce new predictions and new proposals.
    """
    for day in range(1, 11):
        found = _proposed_or_none(owner_engine)
        if found is not None:
            return found
        _run(app_engine, artifact_root, "dry_run", NOW + timedelta(days=day))
    found = _proposed_or_none(owner_engine)
    if found is None:
        pytest.fail("the worker proposed no action for acme-saas on any of ten cutoff days")
    return found


def _proposed_or_none(owner_engine: Engine) -> uuid.UUID | None:
    with tenant_session(owner_engine, ACME) as session:
        found: uuid.UUID | None = session.execute(
            text(
                "SELECT id FROM recommended_actions WHERE org_id = :o AND status = 'proposed' "
                "ORDER BY proposed_at, id LIMIT 1"
            ),
            {"o": ACME},
        ).scalar_one_or_none()
    return found


def _decide(app_engine: Engine, action_id: uuid.UUID, decision: str) -> uuid.UUID:
    approval_id = uuid.uuid4()
    with tenant_session(app_engine, ACME) as session:
        session.execute(
            text(
                "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
                "VALUES (:i, :o, :a, :d, :actor)"
            ),
            {"i": approval_id, "o": ACME, "a": action_id, "d": decision, "actor": uuid.uuid4()},
        )
        session.execute(
            text("UPDATE recommended_actions SET status = :d WHERE org_id = :o AND id = :a"),
            {"d": decision, "o": ACME, "a": action_id},
        )
    return approval_id


def test_dry_run_run_is_recorded_and_stays_within_capacity(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    result = _run(app_engine, artifact_root, "dry_run", NOW)

    assert result.status == "succeeded", result.error
    assert result.counts.get("proposed", 0) <= result.counts["capacity"]
    assert (
        _scalar(
            owner_engine,
            "SELECT status FROM worker_runs WHERE org_id = :o AND started_at = :t",
            {"t": NOW},
        )
        == "succeeded"
    )


def test_rerun_creates_no_new_predictions_or_actions(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    _run(app_engine, artifact_root, "dry_run", NOW)
    before = _org_totals(owner_engine)

    result = _run(app_engine, artifact_root, "dry_run", NOW + timedelta(minutes=1))

    assert result.status == "succeeded", result.error
    assert result.counts.get("proposed", 0) == 0
    assert _org_totals(owner_engine) == before


def test_worker_never_creates_outbox_for_an_unapproved_action(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    _run(app_engine, artifact_root, "dry_run", NOW + timedelta(minutes=2))

    unapproved = _scalar(
        owner_engine,
        "SELECT count(*) FROM outbox_messages om "
        "JOIN recommended_actions ra ON ra.org_id = om.org_id AND ra.id = om.action_id "
        "WHERE om.org_id = :o AND ra.status <> 'approved'",
    )
    assert unapproved == 0


def test_database_refuses_outbox_message_for_rejected_action(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    action_id = _first_proposed(app_engine, owner_engine, artifact_root, acme_model)
    approval_id = _decide(app_engine, action_id, "rejected")

    with (
        pytest.raises(DBAPIError, match="requires an approved action"),
        tenant_session(app_engine, ACME) as session,
    ):
        session.execute(
            text(
                "INSERT INTO outbox_messages (id, org_id, action_id, approval_id, channel, "
                "delivery_mode, payload, payload_sha256) VALUES (:i, :o, :a, :p, 'email', "
                "'dry_run', CAST('{}' AS jsonb), :h)"
            ),
            {"i": uuid.uuid4(), "o": ACME, "a": action_id, "p": approval_id, "h": "0" * 64},
        )


def test_approved_action_is_recorded_as_dry_run_and_never_sent(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    action_id = _first_proposed(app_engine, owner_engine, artifact_root, acme_model)
    _decide(app_engine, action_id, "approved")

    result = _run(app_engine, artifact_root, "dry_run", NOW + timedelta(minutes=3))

    assert result.status == "succeeded", result.error
    assert result.counts["dry_run_recorded"] >= 1
    status, attempts, finished_at = _outbox_row(owner_engine, action_id)
    assert status == "dry_run_recorded"
    assert attempts == 0
    assert finished_at is not None


def _outbox_row(owner_engine: Engine, action_id: uuid.UUID) -> tuple[str, int, datetime | None]:
    with tenant_session(owner_engine, ACME) as session:
        row = session.execute(
            text(
                "SELECT status, attempts, finished_at FROM outbox_messages "
                "WHERE org_id = :o AND action_id = :a"
            ),
            {"o": ACME, "a": action_id},
        ).one()
    return row.status, row.attempts, row.finished_at


def test_org_held_by_another_worker_is_skipped_not_run(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    before = _scalar(owner_engine, "SELECT count(*) FROM worker_runs WHERE org_id = :o")
    with owner_engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"),
            {"k": f"chi-worker:{ACME}"},
        )
        holder.commit()
        try:
            result = _run(app_engine, artifact_root, "dry_run", NOW + timedelta(hours=1))
        finally:
            holder.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"),
                {"k": f"chi-worker:{ACME}"},
            )
            holder.commit()

    assert result.status == "skipped_locked"
    assert _scalar(owner_engine, "SELECT count(*) FROM worker_runs WHERE org_id = :o") == before


def test_run_left_running_by_a_crash_is_marked_failed_on_the_next_run(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    abandoned = uuid.uuid4()
    with tenant_session(owner_engine, ACME) as session:
        session.execute(
            text(
                "INSERT INTO worker_runs (id, org_id, mode, started_at) "
                "VALUES (:i, :o, 'dry_run', :t)"
            ),
            {"i": abandoned, "o": ACME, "t": NOW + timedelta(hours=2)},
        )

    result = _run(app_engine, artifact_root, "dry_run", NOW + timedelta(hours=3))

    assert result.status == "succeeded", result.error
    with tenant_session(owner_engine, ACME) as session:
        row = session.execute(
            text("SELECT status, error FROM worker_runs WHERE id = :i"), {"i": abandoned}
        ).one()
        still_running = session.execute(
            text("SELECT count(*) FROM worker_runs WHERE org_id = :o AND status = 'running'"),
            {"o": ACME},
        ).scalar_one()
    assert row.status == "failed"
    assert row.error.startswith("abandoned")
    assert still_running == 0


def test_simulated_delivery_retries_through_the_worker_then_sends(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    action_id = _first_proposed(app_engine, owner_engine, artifact_root, acme_model)
    _decide(app_engine, action_id, "approved")
    attempts_seen: list[int] = []

    def flaky_once(message: ClaimedMessage) -> None:
        attempts_seen.append(message.attempts)
        if len(attempts_seen) == 1:
            raise RuntimeError("simulated transport timeout")

    options = RunOptions(
        mode="simulated",
        artifact_root=artifact_root,
        capacity_fraction=0.10,
        worker_id="test-worker",
        deliver=flaky_once,
    )
    failing_at = NOW + timedelta(hours=5)
    [first] = run_once(app_engine, [ACME], options, failing_at)

    assert first.status == "succeeded", first.error
    assert first.counts["retry_scheduled"] == 1
    assert _outbox_row(owner_engine, action_id)[0] == "pending"

    [second] = run_once(app_engine, [ACME], options, failing_at + timedelta(minutes=10))

    assert second.counts["sent"] == 1
    assert attempts_seen == [1, 2]
    status, attempts, finished_at = _outbox_row(owner_engine, action_id)
    assert (status, attempts) == ("sent", 2)
    assert finished_at is not None


def test_worker_run_rows_are_invisible_to_another_tenant(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    _run(app_engine, artifact_root, "dry_run", NOW + timedelta(hours=4))
    other = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Other')"),
            {"i": other, "s": f"other-{other.hex[:10]}"},
        )

    with tenant_session(app_engine, other) as session:
        visible = session.execute(
            text("SELECT count(*) FROM worker_runs WHERE org_id = :o"), {"o": ACME}
        ).scalar_one()
    assert visible == 0


def _proposed_from_fresh_day(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, first_day: int
) -> uuid.UUID:
    """Run the worker on successive cutoff days until one yields a proposal.

    A day yields a proposal only when some eligible account is RED or YELLOW, so the scan
    covers a wide window. Days with only insufficient-data accounts propose nothing by design.
    """
    for day in range(first_day, first_day + 90):
        found = _proposed_or_none(owner_engine)
        if found is not None:
            return found
        _run(app_engine, artifact_root, "dry_run", NOW + timedelta(days=day))
    found = _proposed_or_none(owner_engine)
    if found is None:
        pytest.fail("the worker proposed no action in the 90-day scan window")
    return found


@pytest.fixture
def isolated_tenant(
    owner_engine: Engine,
    app_engine: Engine,
    fitted_model: LoadedModel,
    artifact_root: Path,
) -> tuple[uuid.UUID, str]:
    """A fresh organisation with one account, a registered model and one approved action.

    Built directly rather than from the canonical data, so no earlier test's open actions can
    block it. Returns the organisation and its approved action.
    """
    org_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Iso')"),
            {"i": org_id, "s": f"iso-{org_id.hex[:10]}"},
        )
    version = f"iso-{uuid.uuid4().hex[:8]}"
    digest = save_artifact(
        artifact_root / version, fitted_model.pipeline, {"model_version": version}
    )
    version_id = register_version(
        owner_engine,
        org_id,
        model_version=version,
        model_kind="logistic",
        feature_set_version=FEATURE_SET_VERSION,
        artifact_sha256=digest,
        training_window={"start": "2024-01-01", "end": "2026-09-30"},
        metadata={},
    )
    mark_registered(owner_engine, org_id, version_id)
    account_id = uuid.uuid4()
    prediction_id = uuid.uuid4()
    action_id = uuid.uuid4()
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, "
                "plan_tier, region, timezone, created_at) VALUES (:i, :o, 'Iso', :d, 'smb', "
                "'starter', 'na', 'UTC', now())"
            ),
            {"i": account_id, "o": org_id, "d": f"{account_id.hex}.example.test"},
        )
        session.execute(
            text(
                "INSERT INTO predictions (id, org_id, account_id, model_version_id, "
                "feature_set_version, cutoff_at, eligible, risk_score, feature_values, "
                "feature_provenance) VALUES (:p, :o, :a, :m, :f, :c, true, 0.5, "
                "'{}'::jsonb, '{}'::jsonb)"
            ),
            {
                "p": prediction_id,
                "o": org_id,
                "a": account_id,
                "m": version_id,
                "f": FEATURE_SET_VERSION,
                "c": NOW,
            },
        )
        session.execute(
            text(
                "INSERT INTO recommended_actions (id, org_id, account_id, prediction_id, "
                "action_type, reason_code, evidence) VALUES (:i, :o, :a, :p, "
                "'csm_outreach_email', 'risk_threshold', '{}'::jsonb)"
            ),
            {"i": action_id, "o": org_id, "a": account_id, "p": prediction_id},
        )
        session.execute(
            text(
                "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id) "
                "VALUES (:i, :o, :a, 'approved', :actor)"
            ),
            {"i": uuid.uuid4(), "o": org_id, "a": action_id, "actor": uuid.uuid4()},
        )
        session.execute(
            text(
                "UPDATE recommended_actions SET status = 'approved' WHERE org_id = :o AND id = :a"
            ),
            {"o": org_id, "a": action_id},
        )
    return org_id, str(action_id)


def test_second_worker_is_skipped_while_first_holds_the_org_lock_mid_delivery(
    app_engine: Engine,
    owner_engine: Engine,
    artifact_root: Path,
    isolated_tenant: tuple[uuid.UUID, str],
) -> None:
    org_id, action_id = isolated_tenant
    when = NOW
    entered = threading.Event()
    release = threading.Event()

    def holding_deliver(message: ClaimedMessage) -> None:
        entered.set()
        release.wait(timeout=30)

    holder = RunOptions(
        mode="simulated",
        artifact_root=artifact_root,
        capacity_fraction=0.10,
        worker_id="holder",
        deliver=holding_deliver,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_once, app_engine, [org_id], holder, when)
        try:
            assert entered.wait(timeout=30), "the first worker never reached delivery"
            [contender] = run_once(
                app_engine,
                [org_id],
                RunOptions(
                    mode="simulated",
                    artifact_root=artifact_root,
                    capacity_fraction=0.10,
                    worker_id="contender",
                ),
                when,
            )
        finally:
            release.set()
        [holding] = future.result(timeout=60)

    assert contender.status == "skipped_locked"
    assert contender.counts == {}
    assert holding.status == "succeeded", holding.error
    assert holding.counts["sent"] == 1
    with tenant_session(owner_engine, org_id) as session:
        runs = session.execute(
            text("SELECT status FROM worker_runs WHERE org_id = :o"), {"o": org_id}
        ).all()
        status = session.execute(
            text("SELECT status FROM outbox_messages WHERE org_id = :o AND action_id = :a"),
            {"o": org_id, "a": uuid.UUID(action_id)},
        ).scalar_one()
    assert [r.status for r in runs] == ["succeeded"]
    assert status == "sent"


def test_racing_workers_create_no_duplicate_predictions_or_actions(
    app_engine: Engine, owner_engine: Engine, artifact_root: Path, acme_model: str
) -> None:
    when = NOW + timedelta(days=80)
    barrier = threading.Barrier(2)

    def race() -> OrgRun:
        barrier.wait(timeout=30)
        return _run(app_engine, artifact_root, "dry_run", when)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=120) for f in [pool.submit(race) for _ in range(2)]]

    assert {r.status for r in results} <= {"succeeded", "skipped_locked"}
    assert any(r.status == "succeeded" for r in results)
    cutoff = when.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    accounts = _scalar(owner_engine, "SELECT count(*) FROM accounts WHERE org_id = :o")
    predictions = _scalar(
        owner_engine,
        "SELECT count(*) FROM predictions WHERE org_id = :o AND cutoff_at = :c",
        {"c": cutoff},
    )
    duplicated = _scalar(
        owner_engine,
        "SELECT count(*) FROM (SELECT account_id FROM predictions "
        "WHERE org_id = :o AND cutoff_at = :c GROUP BY account_id, model_version_id "
        "HAVING count(*) > 1) d",
        {"c": cutoff},
    )
    assert predictions == accounts
    assert duplicated == 0
