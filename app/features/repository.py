"""Persist and read feature snapshots. Append-only: the application role cannot UPDATE them."""

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.features.builder import FeatureSnapshot


def save_snapshot(engine: Engine, snapshot: FeatureSnapshot) -> None:
    provenance = {
        name: {
            "how": p.how,
            "window_days": p.window_days,
            "source": p.source,
            "event_ids": list(p.event_ids),
        }
        for name, p in snapshot.provenance.items()
    }
    with tenant_session(engine, snapshot.org_id) as session:
        session.execute(
            text(
                """
                INSERT INTO feature_snapshots
                    (org_id, account_id, cutoff_at, feature_set_version, values, provenance)
                VALUES
                    (:org_id, :account_id, :cutoff_at, :version,
                     CAST(:values AS jsonb), CAST(:provenance AS jsonb))
                ON CONFLICT (org_id, account_id, cutoff_at, feature_set_version) DO NOTHING
                """
            ),
            {
                "org_id": snapshot.org_id,
                "account_id": snapshot.account_id,
                "cutoff_at": snapshot.cutoff_at,
                "version": snapshot.feature_set_version,
                "values": json.dumps(snapshot.values),
                "provenance": json.dumps(provenance),
            },
        )


def read_snapshot(
    engine: Engine, org_id: UUID, account_id: UUID, cutoff: datetime, version: str
) -> dict[str, object] | None:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text(
                """
                SELECT values, provenance FROM feature_snapshots
                WHERE org_id = :org_id AND account_id = :account_id
                  AND cutoff_at = :cutoff AND feature_set_version = :version
                """
            ),
            {"org_id": org_id, "account_id": account_id, "cutoff": cutoff, "version": version},
        ).first()
    return None if row is None else {"values": row.values, "provenance": row.provenance}
