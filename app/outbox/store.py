"""Outbox persistence under the application role. Every statement runs in one tenant's context.

Each state change is guarded by the status and lock the caller expects, so a worker whose lock
expired cannot overwrite the result of the worker that recovered the row. The database
triggers in migration 0006 enforce the allowed transitions.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Engine, Result, text
from sqlalchemy.orm import Session

from app.db.session import tenant_session
from app.outbox.retry import backoff_until, failure_outcome
from app.policy.suppression import SuppressionFacts, suppression_reason

TEMPLATE = "csm_outreach_v1"
MAX_ERROR_LENGTH = 500


def _rowcount(result: Result[Any]) -> int:
    return cast(CursorResult[Any], result).rowcount


@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    id: UUID
    account_id: UUID
    attempts: int
    max_attempts: int
    payload: dict[str, Any]


def _payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def insert_outbox_message(
    session: Session,
    org_id: UUID,
    *,
    action_id: UUID,
    approval_id: UUID,
    account_id: UUID,
    reason_code: str,
    delivery_mode: str,
    now: datetime,
) -> int:
    """Insert one message. The database refuses it unless the approval approves the action."""
    payload = {
        "template": TEMPLATE,
        "action_id": str(action_id),
        "account_id": str(account_id),
        "reason_code": reason_code,
    }
    result = session.execute(
        text(
            """
            INSERT INTO outbox_messages (id, org_id, action_id, approval_id, channel,
                delivery_mode, payload, payload_sha256, next_attempt_at)
            VALUES (:i, :o, :act, :appr, 'email', :mode, CAST(:payload AS jsonb), :sha, :now)
            ON CONFLICT (org_id, action_id) DO NOTHING
            """
        ),
        {
            "i": uuid4(),
            "o": org_id,
            "act": action_id,
            "appr": approval_id,
            "mode": delivery_mode,
            "payload": json.dumps(payload, sort_keys=True),
            "sha": _payload_digest(payload),
            "now": now,
        },
    )
    return _rowcount(result)


def enqueue_approved(engine: Engine, org_id: UUID, delivery_mode: str, now: datetime) -> int:
    """Create one outbox message per approved action that has none. Idempotent per action.

    The database refuses a message unless its action is approved and its approval decision
    is 'approved', so this query cannot create a message for an unapproved action.
    """
    with tenant_session(engine, org_id) as session:
        rows = session.execute(
            text(
                """
                SELECT ra.id AS action_id, ra.account_id, ra.reason_code, ap.id AS approval_id
                FROM recommended_actions ra
                JOIN action_approvals ap ON ap.org_id = ra.org_id AND ap.action_id = ra.id
                LEFT JOIN outbox_messages om ON om.org_id = ra.org_id AND om.action_id = ra.id
                WHERE ra.org_id = :o AND ra.status = 'approved'
                  AND ap.decision = 'approved' AND om.id IS NULL
                """
            ),
            {"o": org_id},
        ).all()
        created = 0
        for row in rows:
            created += insert_outbox_message(
                session,
                org_id,
                action_id=row.action_id,
                approval_id=row.approval_id,
                account_id=row.account_id,
                reason_code=row.reason_code,
                delivery_mode=delivery_mode,
                now=now,
            )
    return created


def suppression_facts(
    session: Session, org_id: UUID, account_id: UUID, now: datetime
) -> SuppressionFacts:
    row = session.execute(
        text(
            """
            SELECT
              EXISTS (SELECT 1 FROM suppressions s
                      WHERE s.org_id = :o AND s.account_id = :a AND s.reason = 'legal_hold'
                        AND s.released_at IS NULL AND s.starts_at <= :now
                        AND (s.expires_at IS NULL OR s.expires_at > :now)) AS legal_hold,
              EXISTS (SELECT 1 FROM suppressions s
                      WHERE s.org_id = :o AND s.account_id = :a
                        AND s.reason = 'critical_escalation'
                        AND s.released_at IS NULL AND s.starts_at <= :now
                        AND (s.expires_at IS NULL OR s.expires_at > :now)) AS critical,
              (SELECT max(om.finished_at) FROM outbox_messages om
               JOIN recommended_actions ra ON ra.org_id = om.org_id AND ra.id = om.action_id
               WHERE om.org_id = :o AND ra.account_id = :a AND om.status = 'sent') AS last_contact
            """
        ),
        {"o": org_id, "a": account_id, "now": now},
    ).one()
    return SuppressionFacts(
        legal_hold=bool(row.legal_hold),
        critical_escalation_open=bool(row.critical),
        last_successful_contact_at=row.last_contact,
    )


def claim_due(
    engine: Engine,
    org_id: UUID,
    worker_id: str,
    now: datetime,
    *,
    limit: int,
    lock_for: timedelta,
) -> tuple[list[ClaimedMessage], dict[str, int]]:
    """Move due messages out of pending. Suppressed ones are terminal; dry-run ones are recorded.

    Simulated messages are claimed (pending -> sending, attempt counted, lock set) and returned
    for delivery. The claim commits before delivery so a crash leaves a visible, expiring lock.
    """
    claimed: list[ClaimedMessage] = []
    terminal = {"suppressed": 0, "dry_run_recorded": 0}
    with tenant_session(engine, org_id) as session:
        due = session.execute(
            text(
                """
                SELECT om.id, om.delivery_mode, om.attempts, om.max_attempts, om.payload,
                       ra.account_id
                FROM outbox_messages om
                JOIN recommended_actions ra ON ra.org_id = om.org_id AND ra.id = om.action_id
                WHERE om.org_id = :o AND om.status = 'pending'
                  AND om.next_attempt_at <= :now AND om.attempts < om.max_attempts
                ORDER BY om.next_attempt_at, om.id
                LIMIT :n
                FOR UPDATE OF om SKIP LOCKED
                """
            ),
            {"o": org_id, "now": now, "n": limit},
        ).all()
        for row in due:
            reason = suppression_reason(
                suppression_facts(session, org_id, row.account_id, now), now
            )
            if reason is not None:
                _finish_pending(session, org_id, row.id, "suppressed", last_error=reason, now=now)
                terminal["suppressed"] += 1
            elif row.delivery_mode == "dry_run":
                _finish_pending(session, org_id, row.id, "dry_run_recorded", None, now)
                terminal["dry_run_recorded"] += 1
            else:
                session.execute(
                    text(
                        """
                        UPDATE outbox_messages
                        SET status = 'sending', attempts = attempts + 1,
                            locked_by = :w, locked_until = :until
                        WHERE org_id = :o AND id = :i AND status = 'pending'
                        """
                    ),
                    {"o": org_id, "i": row.id, "w": worker_id, "until": now + lock_for},
                )
                claimed.append(
                    ClaimedMessage(
                        id=row.id,
                        account_id=row.account_id,
                        attempts=row.attempts + 1,
                        max_attempts=row.max_attempts,
                        payload=dict(row.payload),
                    )
                )
    return claimed, terminal


def _finish_pending(
    session: Session,
    org_id: UUID,
    message_id: UUID,
    status: str,
    last_error: str | None,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            UPDATE outbox_messages
            SET status = :s, finished_at = :now, last_error = :e
            WHERE org_id = :o AND id = :i AND status = 'pending'
            """
        ),
        {"s": status, "now": now, "e": last_error, "o": org_id, "i": message_id},
    )


def mark_sent(
    engine: Engine, org_id: UUID, message_id: UUID, worker_id: str, now: datetime
) -> bool:
    """Record a successful simulated send. False if this worker no longer holds the lock."""
    with tenant_session(engine, org_id) as session:
        result = session.execute(
            text(
                """
                UPDATE outbox_messages
                SET status = 'sent', finished_at = :now, locked_by = NULL, locked_until = NULL,
                    last_error = NULL
                WHERE org_id = :o AND id = :i AND status = 'sending' AND locked_by = :w
                """
            ),
            {"o": org_id, "i": message_id, "w": worker_id, "now": now},
        )
    return _rowcount(result) == 1


def mark_failed(
    engine: Engine,
    org_id: UUID,
    message: ClaimedMessage,
    worker_id: str,
    error: str,
    now: datetime,
) -> str | None:
    """Return the message to pending with backoff, or dead-letter it at the attempt limit.

    Returns the new status, or None if this worker no longer holds the lock.
    """
    outcome = failure_outcome(message.attempts, message.max_attempts)
    with tenant_session(engine, org_id) as session:
        if outcome == "pending":
            params: dict[str, Any] = {
                "o": org_id,
                "i": message.id,
                "w": worker_id,
                "e": error[:MAX_ERROR_LENGTH],
                "next": backoff_until(now, message.attempts),
            }
            result = session.execute(
                text(
                    """
                    UPDATE outbox_messages
                    SET status = 'pending', locked_by = NULL, locked_until = NULL,
                        last_error = :e, next_attempt_at = :next
                    WHERE org_id = :o AND id = :i AND status = 'sending' AND locked_by = :w
                    """
                ),
                params,
            )
        else:
            result = session.execute(
                text(
                    """
                    UPDATE outbox_messages
                    SET status = 'dead_letter', locked_by = NULL, locked_until = NULL,
                        last_error = :e, finished_at = :now
                    WHERE org_id = :o AND id = :i AND status = 'sending' AND locked_by = :w
                    """
                ),
                {
                    "o": org_id,
                    "i": message.id,
                    "w": worker_id,
                    "e": error[:MAX_ERROR_LENGTH],
                    "now": now,
                },
            )
    return outcome if _rowcount(result) == 1 else None


def recover_stale_locks(engine: Engine, org_id: UUID, now: datetime) -> dict[str, int]:
    """Release expired 'sending' rows. Attempts are kept, so a crash cannot reset the limit."""
    with tenant_session(engine, org_id) as session:
        retried = _rowcount(
            session.execute(
                text(
                    """
                    UPDATE outbox_messages
                    SET status = 'pending', locked_by = NULL, locked_until = NULL,
                        next_attempt_at = :now, last_error = 'stale lock recovered'
                    WHERE org_id = :o AND status = 'sending' AND locked_until <= :now
                      AND attempts < max_attempts
                    """
                ),
                {"o": org_id, "now": now},
            )
        )
        dead = _rowcount(
            session.execute(
                text(
                    """
                    UPDATE outbox_messages
                    SET status = 'dead_letter', locked_by = NULL, locked_until = NULL,
                        finished_at = :now, last_error = 'stale lock at maximum attempts'
                    WHERE org_id = :o AND status = 'sending' AND locked_until <= :now
                      AND attempts >= max_attempts
                    """
                ),
                {"o": org_id, "now": now},
            )
        )
    return {"requeued": retried, "dead_lettered": dead}
