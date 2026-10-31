"""Read-only queries for the Group 1 API. Every call runs in one tenant's RLS context.

Cursors are opaque to clients: base64url JSON of the last sort key. A malformed cursor is a
client error, not a server fault. Ranked accounts sort by latest risk score (highest first,
accounts with no scored prediction last), then by account id, so a cursor names one row.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.api.errors import ApiProblem
from app.db.session import tenant_session
from app.policy.read import ryg_for_prediction

LATEST_PREDICTION_SQL = """
    SELECT DISTINCT ON (p.account_id)
           p.account_id, p.id AS prediction_id, p.model_version_id, p.cutoff_at,
           p.predicted_at, p.eligible,
           p.ineligible_reason, p.risk_score, p.feature_values, p.renewal_date
    FROM predictions p
    WHERE p.org_id = :o
    ORDER BY p.account_id, p.cutoff_at DESC, p.predicted_at DESC, p.id DESC
"""


def encode_cursor(key: dict[str, Any]) -> str:
    raw = json.dumps(key, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _invalid_cursor() from exc
    if not isinstance(decoded, dict):
        raise _invalid_cursor()
    return decoded


def list_accounts(
    engine: Engine, org_id: UUID, limit: int, cursor: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"o": org_id, "limit": limit + 1}
    if cursor is None:
        after_clause = ""
    else:
        after_clause, params = _after_clause(cursor, params)
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                f"""
                WITH latest AS ({LATEST_PREDICTION_SQL})
                SELECT a.id AS account_id, a.canonical_name, a.segment, a.renewal_date,
                       l.prediction_id, l.model_version_id, l.cutoff_at, l.predicted_at, l.eligible,
                       l.ineligible_reason, l.risk_score, l.feature_values,
                       l.renewal_date AS scored_renewal_date
                FROM accounts a
                LEFT JOIN latest l ON l.account_id = a.id
                WHERE a.org_id = :o {after_clause}
                ORDER BY l.risk_score DESC NULLS LAST, a.id
                LIMIT :limit
                """  # noqa: S608
            ),
            params,
        ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            {
                "risk_score": last.risk_score,
                "account_id": str(last.account_id),
            }
        )
    return [_account_row(row) for row in page], next_cursor


def _after_clause(cursor: dict[str, Any], params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        account_id = UUID(str(cursor["account_id"]))
    except (KeyError, ValueError) as exc:
        raise _invalid_cursor() from exc
    params["after_id"] = account_id
    if "risk_score" not in cursor:
        raise _invalid_cursor()
    score = cursor["risk_score"]
    if score is None:
        return "AND l.risk_score IS NULL AND a.id > :after_id", params
    if not isinstance(score, int | float) or isinstance(score, bool):
        raise _invalid_cursor()
    params["after_score"] = float(score)
    clause = (
        "AND (l.risk_score < :after_score "
        "OR (l.risk_score = :after_score AND a.id > :after_id) "
        "OR l.risk_score IS NULL)"
    )
    return clause, params


def get_account(engine: Engine, org_id: UUID, account_id: UUID) -> dict[str, Any] | None:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text(
                f"""
                WITH latest AS ({LATEST_PREDICTION_SQL})
                SELECT a.id AS account_id, a.canonical_name, a.primary_domain, a.segment,
                       a.plan_tier, a.region, a.timezone, a.renewal_date,
                       l.prediction_id, l.model_version_id, l.cutoff_at, l.predicted_at, l.eligible,
                       l.ineligible_reason, l.risk_score, l.feature_values,
                       l.renewal_date AS scored_renewal_date
                FROM accounts a
                LEFT JOIN latest l ON l.account_id = a.id
                WHERE a.org_id = :o AND a.id = :a
                """  # noqa: S608
            ),
            {"o": org_id, "a": account_id},
        ).first()
    if row is None:
        return None
    return {
        "account_id": str(row.account_id),
        "canonical_name": row.canonical_name,
        "primary_domain": row.primary_domain,
        "segment": row.segment,
        "plan_tier": row.plan_tier,
        "region": row.region,
        "timezone": row.timezone,
        "renewal_date": row.renewal_date,
        "latest_prediction": _latest_prediction(row),
    }


def _account_row(row: Any) -> dict[str, Any]:
    return {
        "account_id": str(row.account_id),
        "canonical_name": row.canonical_name,
        "segment": row.segment,
        "renewal_date": row.renewal_date,
        "latest_prediction": _latest_prediction(row),
    }


def _latest_prediction(row: Any) -> dict[str, Any] | None:
    if row.prediction_id is None:
        return None
    return _prediction_payload(
        prediction_id=row.prediction_id,
        model_version_id=row.model_version_id,
        feature_cutoff_at=row.cutoff_at,
        scored_at=row.predicted_at,
        eligible=row.eligible,
        ineligible_reason=row.ineligible_reason,
        risk_score=row.risk_score,
        renewal_date=row.scored_renewal_date,
        feature_values=row.feature_values,
    )


def _prediction_payload(
    *,
    prediction_id: UUID,
    model_version_id: UUID,
    feature_cutoff_at: datetime,
    scored_at: datetime | None,
    eligible: bool,
    ineligible_reason: str | None,
    risk_score: float | None,
    renewal_date: datetime | None,
    feature_values: dict[str, Any] | None,
) -> dict[str, Any]:
    view = ryg_for_prediction(feature_values, renewal_date, feature_cutoff_at, eligible)
    if view is not None:
        ryg_state = "evaluated"
    elif not eligible:
        ryg_state = "not_eligible"
    else:
        ryg_state = "no_snapshot"
    payload: dict[str, Any] = {
        "prediction_id": str(prediction_id),
        "model_version_id": str(model_version_id),
        "feature_cutoff_at": feature_cutoff_at,
        "eligible": eligible,
        "ineligible_reason": ineligible_reason,
        "risk_score": risk_score,
        "ryg_state": ryg_state,
        "ryg": None if view is None else _ryg_payload(view),
    }
    if scored_at is not None:
        payload["scored_at"] = scored_at
    return payload


def _ryg_payload(view: Any) -> dict[str, Any]:
    return {
        "status": view.status,
        "policy_version": view.policy_version,
        "feature_cutoff_at": view.cutoff_at,
        "reasons": view.reasons,
    }


def _invalid_cursor() -> ApiProblem:
    return ApiProblem(400, "invalid-cursor", "Invalid cursor", "The cursor is not valid.")


def list_predictions(
    engine: Engine, org_id: UUID, account_id: UUID, limit: int, cursor: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], str | None] | None:
    if not _account_exists(engine, org_id, account_id):
        return None
    params: dict[str, Any] = {"o": org_id, "a": account_id, "limit": limit + 1}
    after_clause = ""
    if cursor is not None:
        after_clause = "AND (p.cutoff_at, p.id) < (:after_cutoff, :after_id)"
        params["after_cutoff"] = _cursor_time(cursor, "cutoff_at")
        params["after_id"] = _cursor_uuid(cursor, "prediction_id")
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                f"""
                SELECT p.id AS prediction_id, p.model_version_id, p.cutoff_at, p.predicted_at,
                       p.eligible, p.ineligible_reason, p.risk_score, p.renewal_date,
                       p.feature_values, p.feature_set_version
                FROM predictions p
                WHERE p.org_id = :o AND p.account_id = :a {after_clause}
                ORDER BY p.cutoff_at DESC, p.id DESC
                LIMIT :limit
                """  # noqa: S608
            ),
            params,
        ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            {"cutoff_at": last.cutoff_at.isoformat(), "prediction_id": str(last.prediction_id)}
        )
    return [_prediction_row(row) for row in page], next_cursor


def get_prediction(engine: Engine, org_id: UUID, prediction_id: UUID) -> dict[str, Any] | None:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text(
                """
                SELECT p.id AS prediction_id, p.account_id, p.model_version_id, p.cutoff_at,
                       p.predicted_at, p.eligible, p.ineligible_reason, p.risk_score,
                       p.renewal_date, p.feature_values, p.feature_provenance,
                       p.feature_set_version, mv.model_version
                FROM predictions p
                JOIN model_versions mv ON mv.org_id = p.org_id AND mv.id = p.model_version_id
                WHERE p.org_id = :o AND p.id = :i
                """
            ),
            {"o": org_id, "i": prediction_id},
        ).first()
    if row is None:
        return None
    detail = _prediction_row(row)
    detail.update(
        {
            "account_id": str(row.account_id),
            "model_version": row.model_version,
            "feature_set_version": row.feature_set_version,
            "renewal_date": row.renewal_date,
            "feature_values": row.feature_values,
            "contributions": row.feature_provenance or {},
        }
    )
    return detail


def prediction_evidence(engine: Engine, org_id: UUID, prediction_id: UUID) -> dict[str, Any] | None:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text("SELECT feature_provenance FROM predictions WHERE org_id = :o AND id = :i"),
            {"o": org_id, "i": prediction_id},
        ).first()
        if row is None:
            return None
        event_ids = sorted(
            {
                event_id
                for entry in (row.feature_provenance or {}).values()
                for event_id in entry.get("event_ids", [])
            }
        )
        events = session.execute(
            text(
                """
                SELECT id, source, event_type, event_time, ingested_at, source_record_id
                FROM source_events
                WHERE org_id = :o AND id = ANY(CAST(:ids AS uuid[]))
                ORDER BY event_time, id
                """
            ),
            {"o": org_id, "ids": event_ids},
        ).all()
    return {
        "prediction_id": str(prediction_id),
        "events": [
            {
                "event_id": str(e.id),
                "source": e.source,
                "event_type": e.event_type,
                "event_time": e.event_time,
                "ingested_at": e.ingested_at,
                "source_record_id": e.source_record_id,
            }
            for e in events
        ],
    }


def list_actions(engine: Engine, org_id: UUID, account_id: UUID) -> list[dict[str, Any]] | None:
    if not _account_exists(engine, org_id, account_id):
        return None
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                """
                SELECT id, prediction_id, action_type, status, reason_code, evidence, proposed_at
                FROM recommended_actions
                WHERE org_id = :o AND account_id = :a
                ORDER BY proposed_at DESC, id DESC
                """
            ),
            {"o": org_id, "a": account_id},
        ).all()
    return [
        {
            "action_id": str(r.id),
            "prediction_id": str(r.prediction_id),
            "action_type": r.action_type,
            "status": r.status,
            "reason_code": r.reason_code,
            "evidence": r.evidence,
            "proposed_at": r.proposed_at,
        }
        for r in rows
    ]


def list_outbox(
    engine: Engine,
    org_id: UUID,
    limit: int,
    cursor: dict[str, Any] | None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"o": org_id, "limit": limit + 1}
    after_clause = ""
    if status is not None:
        after_clause += " AND om.status = :status"
        params["status"] = status
    if cursor is not None:
        after_clause = "AND (om.created_at, om.id) < (:after_created, :after_id)"
        params["after_created"] = _cursor_time(cursor, "created_at")
        params["after_id"] = _cursor_uuid(cursor, "outbox_id")
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                f"""
                SELECT om.id AS outbox_id, om.action_id, om.channel, om.delivery_mode, om.status,
                       om.attempts, om.max_attempts, om.next_attempt_at, om.last_error,
                       om.created_at, om.finished_at
                FROM outbox_messages om
                WHERE om.org_id = :o {after_clause}
                ORDER BY om.created_at DESC, om.id DESC
                LIMIT :limit
                """  # noqa: S608
            ),
            params,
        ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "outbox_id": str(last.outbox_id)}
        )
    return [
        {
            "outbox_id": str(r.outbox_id),
            "action_id": str(r.action_id),
            "channel": r.channel,
            "delivery_mode": r.delivery_mode,
            "status": r.status,
            "attempts": r.attempts,
            "max_attempts": r.max_attempts,
            "next_attempt_at": r.next_attempt_at,
            "last_error": r.last_error,
            "created_at": r.created_at,
            "finished_at": r.finished_at,
        }
        for r in page
    ], next_cursor


def list_models(engine: Engine, org_id: UUID) -> list[dict[str, Any]]:
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                """
                SELECT id, model_version, model_kind, feature_set_version, artifact_sha256,
                       status, created_at
                FROM model_versions WHERE org_id = :o
                ORDER BY created_at DESC, id DESC
                """
            ),
            {"o": org_id},
        ).all()
    return [
        {
            "model_version_id": str(r.id),
            "model_version": r.model_version,
            "model_kind": r.model_kind,
            "feature_set_version": r.feature_set_version,
            "artifact_sha256": r.artifact_sha256,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def model_evaluation(engine: Engine, org_id: UUID, version: str) -> dict[str, Any] | None:
    with tenant_session(engine, org_id) as session:
        model = session.execute(
            text(
                "SELECT id, model_version, model_kind, metadata FROM model_versions "
                "WHERE org_id = :o AND model_version = :v"
            ),
            {"o": org_id, "v": version},
        ).first()
        if model is None:
            return None
        rows = session.execute(
            text(
                "SELECT split, scope, metrics, selection_hash, created_at FROM model_evaluations "
                "WHERE org_id = :o AND model_version_id = :m ORDER BY split, scope"
            ),
            {"o": org_id, "m": model.id},
        ).all()
    return {
        "model_version": model.model_version,
        "model_kind": model.model_kind,
        "metadata": model.metadata,
        "evaluations": [
            {
                "split": r.split,
                "scope": r.scope,
                "metrics": r.metrics,
                "selection_hash": r.selection_hash,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


def data_quality(engine: Engine, org_id: UUID) -> dict[str, Any]:
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                "SELECT kind, source, count(*) AS n FROM data_quality_issues "
                "WHERE org_id = :o GROUP BY kind, source ORDER BY kind, source"
            ),
            {"o": org_id},
        ).all()
    return {
        "issues": [{"kind": r.kind, "source": r.source, "count": r.n} for r in rows],
        "total": sum(r.n for r in rows),
    }


def _account_exists(engine: Engine, org_id: UUID, account_id: UUID) -> bool:
    with tenant_session(engine, org_id) as session:
        found = session.execute(
            text("SELECT 1 FROM accounts WHERE org_id = :o AND id = :a"),
            {"o": org_id, "a": account_id},
        ).first()
    return found is not None


def _prediction_row(row: Any) -> dict[str, Any]:
    return _prediction_payload(
        prediction_id=row.prediction_id,
        model_version_id=row.model_version_id,
        feature_cutoff_at=row.cutoff_at,
        scored_at=row.predicted_at,
        eligible=row.eligible,
        ineligible_reason=row.ineligible_reason,
        risk_score=row.risk_score,
        renewal_date=row.renewal_date,
        feature_values=row.feature_values,
    )


def _cursor_time(cursor: dict[str, Any], key: str) -> datetime:
    try:
        return datetime.fromisoformat(str(cursor[key]))
    except (KeyError, ValueError) as exc:
        raise _invalid_cursor() from exc


def _cursor_uuid(cursor: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(cursor[key]))
    except (KeyError, ValueError) as exc:
        raise _invalid_cursor() from exc
