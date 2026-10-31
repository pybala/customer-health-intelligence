"""Approve or reject one proposed action, idempotently, in a single transaction.

The approval row, the action status change, the idempotency record and the audit event commit
together or not at all. An approved decision also inserts the outbox message, and the database
refuses that insert unless the approval and the action agree (migration 0006).
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.api.errors import ApiProblem
from app.db.session import tenant_session
from app.outbox.store import insert_outbox_message

IDEMPOTENCY_TTL = timedelta(hours=24)
APPROVER_ROLES = frozenset({"csm", "admin"})
Decision = Literal["approved", "rejected"]


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    status_code: int
    body: dict[str, Any]


def request_hash(action_id: UUID, decision: Decision, reason: str | None) -> str:
    canonical = json.dumps(
        {"action_id": str(action_id), "decision": decision, "reason": reason},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def forbidden() -> ApiProblem:
    return ApiProblem(403, "forbidden", "Forbidden", "This role cannot decide actions.")


def decide_action(
    engine: Engine,
    *,
    org_id: UUID,
    actor_id: UUID,
    role: str,
    action_id: UUID,
    key: str,
    decision: Decision,
    reason: str | None,
    now: datetime,
) -> ApprovalResult:
    if role not in APPROVER_ROLES:
        raise forbidden()
    digest = request_hash(action_id, decision, reason)
    with tenant_session(engine, org_id) as session:
        action = _lock_action(session, org_id, action_id)
        if action is None:
            raise ApiProblem(404, "not-found", "Not Found", "Action was not found.")
        replay = _lookup_key(session, org_id, key, digest, now)
        if replay is not None:
            return replay
        _delete_expired_key(session, org_id, key, now)
        if action["status"] != "proposed":
            raise _conflict(action["status"], decision)
        result = _record_decision(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action=action,
            decision=decision,
            reason=reason,
            now=now,
        )
        _store_key(session, org_id, key, digest, result, now)
    return result


def _record_decision(
    session: Session,
    *,
    org_id: UUID,
    actor_id: UUID,
    action: Any,
    decision: Decision,
    reason: str | None,
    now: datetime,
) -> ApprovalResult:
    approval_id = uuid4()
    session.execute(
        text(
            "INSERT INTO action_approvals (id, org_id, action_id, decision, actor_id, reason, "
            "decided_at) VALUES (:i, :o, :a, :d, :actor, :r, :now)"
        ),
        {
            "i": approval_id,
            "o": org_id,
            "a": action["id"],
            "d": decision,
            "actor": actor_id,
            "r": reason,
            "now": now,
        },
    )
    session.execute(
        text("UPDATE recommended_actions SET status = :d WHERE org_id = :o AND id = :a"),
        {"d": decision, "o": org_id, "a": action["id"]},
    )
    if decision == "approved":
        insert_outbox_message(
            session,
            org_id,
            action_id=action["id"],
            approval_id=approval_id,
            account_id=action["account_id"],
            reason_code=action["reason_code"],
            delivery_mode="simulated",
            now=now,
        )
    session.execute(
        text(
            "INSERT INTO audit_events (org_id, actor_key_id, event_type, entity_type, entity_id, "
            "detail, created_at) VALUES (:o, :actor, :t, 'recommended_action', :e, "
            "CAST(:detail AS jsonb), :now)"
        ),
        {
            "o": org_id,
            "actor": actor_id,
            "t": f"action_{decision}",
            "e": str(action["id"]),
            "detail": json.dumps({"approval_id": str(approval_id), "reason": reason}),
            "now": now,
        },
    )
    body = {
        "approval_id": str(approval_id),
        "action_id": str(action["id"]),
        "decision": decision,
        "decided_at": now.isoformat(),
    }
    return ApprovalResult(status_code=201, body=body)


def _lookup_key(
    session: Session, org_id: UUID, key: str, digest: str, now: datetime
) -> ApprovalResult | None:
    row = session.execute(
        text(
            "SELECT request_hash, response_status, response_body, expires_at "
            "FROM idempotency_keys WHERE org_id = :o AND key = :k"
        ),
        {"o": org_id, "k": key},
    ).first()
    if row is None:
        return None
    if _as_utc(row.expires_at) <= now:
        return None
    if row.request_hash != digest:
        raise ApiProblem(
            422,
            "idempotency-key-reused",
            "Idempotency key reused",
            "This Idempotency-Key was used with a different request.",
        )
    return ApprovalResult(status_code=row.response_status, body=row.response_body)


def _delete_expired_key(session: Session, org_id: UUID, key: str, now: datetime) -> None:
    session.execute(
        text("DELETE FROM idempotency_keys WHERE org_id = :o AND key = :k AND expires_at <= :now"),
        {"o": org_id, "k": key, "now": now},
    )


def _lock_action(session: Session, org_id: UUID, action_id: UUID) -> Any:
    return (
        session.execute(
            text(
                "SELECT id, status, account_id, reason_code FROM recommended_actions "
                "WHERE org_id = :o AND id = :a FOR UPDATE"
            ),
            {"o": org_id, "a": action_id},
        )
        .mappings()
        .first()
    )


def _store_key(
    session: Session, org_id: UUID, key: str, digest: str, result: ApprovalResult, now: datetime
) -> None:
    session.execute(
        text(
            "INSERT INTO idempotency_keys (id, org_id, key, request_hash, response_status, "
            "response_body, created_at, expires_at) VALUES (:i, :o, :k, :h, :s, "
            "CAST(:b AS jsonb), :now, :exp)"
        ),
        {
            "i": uuid4(),
            "o": org_id,
            "k": key,
            "h": digest,
            "s": result.status_code,
            "b": json.dumps(result.body),
            "now": now,
            "exp": now + IDEMPOTENCY_TTL,
        },
    )


def _conflict(current: str, requested: Decision) -> ApiProblem:
    return ApiProblem(
        409,
        "action-already-decided",
        "Action already decided",
        f"The action is {current}; it cannot be {requested}.",
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
