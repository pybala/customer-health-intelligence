"""Daily worker: score, decide RYG, propose within review capacity, deliver approved actions.

Each organisation runs under its own advisory lock, tenant context and worker run. A failure in
one organisation is recorded on that organisation's run and does not stop the others. The worker
never writes an approval, so an outbox message exists only for an action a human approved.
"""

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.db.session import tenant_session
from app.ml.artifact import LoadedModel, load_artifact
from app.ml.inference import score_account
from app.ml.registry import record_prediction
from app.outbox.delivery import Deliverer, simulate_send
from app.outbox.store import (
    ClaimedMessage,
    claim_due,
    enqueue_approved,
    mark_failed,
    mark_sent,
    recover_stale_locks,
    suppression_facts,
)
from app.policy.capacity import review_capacity
from app.policy.health import PolicyDecision, Status, evaluate
from app.policy.suppression import suppression_reason

ACTION_TYPE = "csm_outreach_email"
MAX_ERROR_LENGTH = 500


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunOptions:
    mode: str
    artifact_root: Path
    capacity_fraction: float
    worker_id: str
    deliver: Deliverer = simulate_send
    batch: int = 50
    lock_for: timedelta = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class OrgRun:
    org_id: UUID
    status: str
    counts: dict[str, int]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    account_id: UUID
    prediction_id: UUID
    decision: PolicyDecision
    risk_score: float
    evidence: dict[str, Any]


def run_once(
    engine: Engine, org_ids: Sequence[UUID], options: RunOptions, now: datetime
) -> list[OrgRun]:
    if now.tzinfo is None:
        raise ValueError("the worker clock must be timezone-aware")
    return [_run_org(engine, org_id, options, now) for org_id in org_ids]


def _run_org(engine: Engine, org_id: UUID, options: RunOptions, now: datetime) -> OrgRun:
    with _org_lock(engine, org_id) as acquired:
        if not acquired:
            return OrgRun(org_id, "skipped_locked", {})
        run_id = _start_run(engine, org_id, options.mode, now)
        counts: Counter[str] = Counter()
        try:
            _process_org(engine, org_id, options, now, counts)
        except Exception as exc:
            error = _error_text(exc)
            _finish_run(engine, org_id, run_id, "failed", counts, error, now)
            return OrgRun(org_id, "failed", dict(counts), error)
        _finish_run(engine, org_id, run_id, "succeeded", counts, None, now)
        return OrgRun(org_id, "succeeded", dict(counts))


@contextmanager
def _org_lock(engine: Engine, org_id: UUID) -> Iterator[bool]:
    key = f"chi-worker:{org_id}"
    with engine.connect() as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key}
            ).scalar_one()
        )
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
                conn.commit()


def _process_org(
    engine: Engine, org_id: UUID, options: RunOptions, now: datetime, counts: Counter[str]
) -> None:
    recovered = recover_stale_locks(engine, org_id, now)
    counts["stale_requeued"] += recovered["requeued"]
    counts["stale_dead_lettered"] += recovered["dead_lettered"]

    version_id, version, sha = _registered_model(engine, org_id)
    loaded = load_artifact(options.artifact_root / version, sha)
    cutoff = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    candidates, eligible = _score_and_decide(
        engine, org_id, version_id, loaded, cutoff, now, counts
    )
    counts["eligible"] = eligible
    capacity = review_capacity(eligible, options.capacity_fraction)
    counts["capacity"] = capacity
    _propose(engine, org_id, candidates, capacity, now, counts)

    delivery_mode = "dry_run" if options.mode == "dry_run" else "simulated"
    counts["outbox_created"] += enqueue_approved(engine, org_id, delivery_mode, now)
    _deliver(engine, org_id, options, now, counts)


def _registered_model(engine: Engine, org_id: UUID) -> tuple[UUID, str, str]:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text(
                "SELECT id, model_version, artifact_sha256 FROM model_versions "
                "WHERE org_id = :o AND status = 'registered' ORDER BY created_at DESC LIMIT 1"
            ),
            {"o": org_id},
        ).first()
    if row is None:
        raise WorkerError("no registered model version for this organisation")
    return row.id, row.model_version, row.artifact_sha256


def _prediction_id(
    engine: Engine, org_id: UUID, account_id: UUID, cutoff: datetime, version_id: UUID
) -> UUID | None:
    with tenant_session(engine, org_id) as session:
        found: UUID | None = session.execute(
            text(
                "SELECT id FROM predictions WHERE org_id = :o AND account_id = :a "
                "AND cutoff_at = :c AND model_version_id = :m"
            ),
            {"o": org_id, "a": account_id, "c": cutoff, "m": version_id},
        ).scalar_one_or_none()
    return found


def _score_and_decide(
    engine: Engine,
    org_id: UUID,
    version_id: UUID,
    loaded: LoadedModel,
    cutoff: datetime,
    now: datetime,
    counts: Counter[str],
) -> tuple[list[_Candidate], int]:
    with tenant_session(engine, org_id) as session:
        account_ids = [
            row.id for row in session.execute(text("SELECT id FROM accounts ORDER BY id")).all()
        ]
    candidates: list[_Candidate] = []
    eligible = 0
    for account_id in account_ids:
        prediction = score_account(engine, loaded, org_id, account_id, cutoff)
        prediction_id = _prediction_id(engine, org_id, account_id, cutoff, version_id)
        if prediction_id is None:
            record_prediction(engine, version_id, prediction, now)
            prediction_id = _prediction_id(engine, org_id, account_id, cutoff, version_id)
        counts["scored"] += 1
        if not prediction.eligible:
            counts["out_of_window"] += 1
            continue
        eligible += 1
        assert prediction.renewal_date is not None and prediction.snapshot is not None
        assert prediction.risk_score is not None and prediction_id is not None
        days = (prediction.renewal_date - cutoff).days
        decision = evaluate(prediction.snapshot.values, days)
        counts[f"status_{decision.status.value}"] += 1
        if decision.status not in (Status.RED, Status.YELLOW):
            continue
        candidates.append(
            _Candidate(
                account_id=account_id,
                prediction_id=prediction_id,
                decision=decision,
                risk_score=prediction.risk_score,
                evidence=_evidence(decision, prediction.snapshot.provenance, cutoff),
            )
        )
    candidates.sort(
        key=lambda c: (c.decision.status is not Status.RED, -c.risk_score, str(c.account_id))
    )
    return candidates, eligible


def _evidence(decision: PolicyDecision, provenance: Any, cutoff: datetime) -> dict[str, Any]:
    return {
        "policy_version": decision.policy_version,
        "status": decision.status.value,
        "cutoff_at": cutoff.isoformat(),
        "reasons": [
            {
                "code": r.code,
                "feature": r.feature,
                "observed": r.observed,
                "threshold": r.threshold,
            }
            for r in decision.reasons
        ],
        "event_ids": {
            r.feature: list(provenance[r.feature].event_ids)
            for r in decision.reasons
            if r.feature in provenance
        },
    }


def _propose(
    engine: Engine,
    org_id: UUID,
    candidates: list[_Candidate],
    capacity: int,
    now: datetime,
    counts: Counter[str],
) -> None:
    """Propose actions up to the review capacity, counting open proposals against it."""
    with tenant_session(engine, org_id) as session:
        open_count = int(
            session.execute(
                text(
                    "SELECT count(*) FROM recommended_actions "
                    "WHERE org_id = :o AND status = 'proposed'"
                ),
                {"o": org_id},
            ).scalar_one()
        )
        budget = max(0, capacity - open_count)
        for candidate in candidates:
            if budget == 0:
                break
            reason = suppression_reason(
                suppression_facts(session, org_id, candidate.account_id, now), now
            )
            if reason is not None:
                counts[f"suppressed_{reason}"] += 1
                continue
            if _has_open_action(session, org_id, candidate.account_id):
                counts["skipped_open_action"] += 1
                continue
            inserted = session.execute(
                text(
                    """
                    INSERT INTO recommended_actions (id, org_id, account_id, prediction_id,
                        action_type, reason_code, evidence)
                    VALUES (:i, :o, :a, :p, :t, :r, CAST(:e AS jsonb))
                    ON CONFLICT (org_id, prediction_id, action_type) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "i": uuid4(),
                    "o": org_id,
                    "a": candidate.account_id,
                    "p": candidate.prediction_id,
                    "t": ACTION_TYPE,
                    "r": candidate.decision.reasons[0].code,
                    "e": _json(candidate.evidence),
                },
            ).first()
            if inserted is not None:
                counts["proposed"] += 1
                budget -= 1


def _has_open_action(session: Session, org_id: UUID, account_id: UUID) -> bool:
    found = session.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM recommended_actions WHERE org_id = :o "
            "AND account_id = :a AND status IN ('proposed', 'approved'))"
        ),
        {"o": org_id, "a": account_id},
    ).scalar_one()
    return bool(found)


def _deliver(
    engine: Engine, org_id: UUID, options: RunOptions, now: datetime, counts: Counter[str]
) -> None:
    while True:
        claimed, terminal = claim_due(
            engine,
            org_id,
            options.worker_id,
            now,
            limit=options.batch,
            lock_for=options.lock_for,
        )
        for name, value in terminal.items():
            counts[name] += value
        if not claimed and not any(terminal.values()):
            return
        for message in claimed:
            _deliver_one(engine, org_id, options, message, now, counts)


def _deliver_one(
    engine: Engine,
    org_id: UUID,
    options: RunOptions,
    message: ClaimedMessage,
    now: datetime,
    counts: Counter[str],
) -> None:
    try:
        options.deliver(message)
    except Exception as exc:
        outcome = mark_failed(engine, org_id, message, options.worker_id, _error_text(exc), now)
        if outcome is None:
            counts["lock_lost"] += 1
        elif outcome == "pending":
            counts["retry_scheduled"] += 1
        else:
            counts["dead_lettered"] += 1
        return
    if mark_sent(engine, org_id, message.id, options.worker_id, now):
        counts["sent"] += 1
    else:
        counts["lock_lost"] += 1


def _start_run(engine: Engine, org_id: UUID, mode: str, now: datetime) -> UUID:
    # The org lock is held, so any run still marked running was abandoned by a crash.
    run_id = uuid4()
    with tenant_session(engine, org_id) as session:
        session.execute(
            text(
                "UPDATE worker_runs SET status = 'failed', finished_at = :now, "
                "error = 'abandoned: the process ended before the run finished' "
                "WHERE org_id = :o AND status = 'running'"
            ),
            {"o": org_id, "now": now},
        )
        session.execute(
            text(
                "INSERT INTO worker_runs (id, org_id, mode, started_at) VALUES (:i, :o, :m, :now)"
            ),
            {"i": run_id, "o": org_id, "m": mode, "now": now},
        )
    return run_id


def _finish_run(
    engine: Engine,
    org_id: UUID,
    run_id: UUID,
    status: str,
    counts: Counter[str],
    error: str | None,
    now: datetime,
) -> None:
    with tenant_session(engine, org_id) as session:
        session.execute(
            text(
                "UPDATE worker_runs SET status = :s, finished_at = :now, "
                "counts = CAST(:c AS jsonb), error = :e WHERE org_id = :o AND id = :i"
            ),
            {
                "s": status,
                "now": now,
                "c": _json(dict(counts)),
                "e": error,
                "o": org_id,
                "i": run_id,
            },
        )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _error_text(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    return message[:MAX_ERROR_LENGTH]
