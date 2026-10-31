"""Runtime scoring from the persisted artifact. Never reads ground truth.

Eligibility comes from the account's renewal_date (a contract field, not a feature). An account
whose renewal is missing or outside the next 90 days is marked not eligible, never scored green.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.features.builder import FeatureSnapshot, load_snapshot
from app.ml.artifact import LoadedModel, load_artifact
from app.ml.model import COLUMNS

HORIZON = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class Prediction:
    org_id: UUID
    account_id: UUID
    eligible: bool
    reason: str | None
    renewal_date: datetime | None
    risk_score: float | None
    cutoff_at: datetime
    snapshot: FeatureSnapshot | None


def eligibility(renewal: datetime | None, cutoff: datetime) -> tuple[bool, str | None]:
    if renewal is None:
        return False, "no renewal date on record"
    if renewal <= cutoff:
        return False, "renewal date is not after the scoring cutoff"
    if renewal > cutoff + HORIZON:
        return False, "renewal date is outside the 90-day scoring window"
    return True, None


def load_model(artifact_dir: Path, expected_sha256: str) -> LoadedModel:
    return load_artifact(artifact_dir, expected_sha256)


def _renewal_date(engine: Engine, org_id: UUID, account_id: UUID) -> datetime | None:
    with tenant_session(engine, org_id) as session:
        row = session.execute(
            text("SELECT renewal_date FROM accounts WHERE org_id = :o AND id = :a"),
            {"o": org_id, "a": account_id},
        ).one()
    renewal: datetime | None = row.renewal_date
    return renewal


def score_account(
    engine: Engine,
    model: LoadedModel,
    org_id: UUID,
    account_id: UUID,
    cutoff: datetime,
) -> Prediction:
    renewal = _renewal_date(engine, org_id, account_id)
    eligible, reason = eligibility(renewal, cutoff)
    if not eligible:
        return Prediction(
            org_id=org_id,
            account_id=account_id,
            eligible=False,
            reason=reason,
            renewal_date=renewal,
            risk_score=None,
            cutoff_at=cutoff,
            snapshot=None,
        )
    snapshot = load_snapshot(engine, org_id, account_id, cutoff)
    row = np.array([[snapshot.values[name] for name in COLUMNS]], dtype=object)
    score = float(model.pipeline.predict_proba(row)[0, 1])
    return Prediction(
        org_id=org_id,
        account_id=account_id,
        eligible=True,
        reason=None,
        renewal_date=renewal,
        risk_score=score,
        cutoff_at=cutoff,
        snapshot=snapshot,
    )
