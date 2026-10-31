"""Tenant-scoped model registry and prediction persistence.

Model metadata is written per organisation under that organisation's RLS context. A metrics row
holds only that organisation's slice of results; global metrics stay in the artifact metadata.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.features.definitions import FEATURE_SET_VERSION
from app.ml.inference import Prediction


def register_version(
    owner_engine: Engine,
    org_id: UUID,
    *,
    model_version: str,
    model_kind: str,
    feature_set_version: str,
    artifact_sha256: str,
    training_window: dict[str, Any],
    metadata: dict[str, Any],
) -> UUID:
    version_id = uuid4()
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO model_versions (id, org_id, model_version, model_kind, "
                "feature_set_version, artifact_sha256, training_window, metadata) VALUES "
                "(:i, :o, :v, :k, :f, :h, CAST(:w AS jsonb), CAST(:m AS jsonb))"
            ),
            {
                "i": version_id,
                "o": org_id,
                "v": model_version,
                "k": model_kind,
                "f": feature_set_version,
                "h": artifact_sha256,
                "w": json.dumps(training_window, sort_keys=True, default=str),
                "m": json.dumps(metadata, sort_keys=True, default=str),
            },
        )
    return version_id


def record_evaluation(
    owner_engine: Engine,
    org_id: UUID,
    model_version_id: UUID,
    *,
    split: str,
    scope: str,
    metrics: dict[str, Any],
    selection_hash: str,
) -> None:
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO model_evaluations (id, org_id, model_version_id, split, scope, "
                "metrics, selection_hash) VALUES (:i, :o, :v, :s, :c, CAST(:m AS jsonb), :h)"
            ),
            {
                "i": uuid4(),
                "o": org_id,
                "v": model_version_id,
                "s": split,
                "c": scope,
                "m": json.dumps(metrics, sort_keys=True, default=str),
                "h": selection_hash,
            },
        )


def mark_registered(owner_engine: Engine, org_id: UUID, model_version_id: UUID) -> None:
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text("UPDATE model_versions SET status = 'registered' WHERE id = :i AND org_id = :o"),
            {"i": model_version_id, "o": org_id},
        )


def record_prediction(
    app_engine: Engine,
    model_version_id: UUID,
    prediction: Prediction,
    predicted_at: datetime,
) -> None:
    snapshot = prediction.snapshot
    with tenant_session(app_engine, prediction.org_id) as session:
        session.execute(
            text(
                "INSERT INTO predictions (id, org_id, account_id, model_version_id, "
                "feature_set_version, cutoff_at, predicted_at, eligible, ineligible_reason, "
                "renewal_date, risk_score, feature_values, feature_provenance) VALUES "
                "(:i, :o, :a, :v, :f, :c, :p, :e, :r, :d, :s, CAST(:fv AS jsonb), "
                "CAST(:fp AS jsonb))"
            ),
            {
                "i": uuid4(),
                "o": prediction.org_id,
                "a": prediction.account_id,
                "v": model_version_id,
                "f": snapshot.feature_set_version if snapshot else FEATURE_SET_VERSION,
                "c": prediction.cutoff_at,
                "p": predicted_at,
                "e": prediction.eligible,
                "r": prediction.reason,
                "d": prediction.renewal_date,
                "s": prediction.risk_score,
                "fv": json.dumps(snapshot.values, default=str) if snapshot else None,
                "fp": (
                    json.dumps(
                        {
                            name: {
                                "how": p.how,
                                "window_days": p.window_days,
                                "source": p.source,
                                "event_ids": list(p.event_ids),
                            }
                            for name, p in snapshot.provenance.items()
                        },
                        sort_keys=True,
                    )
                    if snapshot
                    else None
                ),
            },
        )
