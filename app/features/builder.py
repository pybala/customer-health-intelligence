"""Point-in-time feature builder.

`compute_features` is pure: it takes visible events and a profile and returns values with
per-feature provenance. `load_snapshot` reads the events under the tenant session, and the SQL
enforces `event_time <= cutoff AND ingested_at <= cutoff` before any Python sees a row.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.features.definitions import (
    EVENT_FEATURES,
    FEATURE_SET_VERSION,
    AccountProfile,
    FeatureSpec,
    FeatureValue,
    VisibleEvent,
)

DAYS_PER_MONTH = 30.4375


@dataclass(frozen=True, slots=True)
class FeatureProvenance:
    how: str
    window_days: int | None
    source: str | None
    event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    org_id: UUID
    account_id: UUID
    cutoff_at: datetime
    feature_set_version: str
    values: dict[str, FeatureValue]
    provenance: dict[str, FeatureProvenance]


def _in_window(event: VisibleEvent, cutoff: datetime, window_days: int | None) -> bool:
    if window_days is None:
        return True
    return event.event_time > cutoff - timedelta(days=window_days)


def _matches(event: VisibleEvent, spec: FeatureSpec) -> bool:
    if spec.source is not None and event.source != spec.source:
        return False
    return spec.event_types is None or event.event_type in spec.event_types


def _profile_values(profile: AccountProfile, cutoff: datetime) -> dict[str, FeatureValue]:
    tenure_days = (cutoff - profile.created_at).total_seconds() / 86400
    tenure_months = None if tenure_days < 0 else round(tenure_days / DAYS_PER_MONTH, 2)
    return {
        "segment": profile.segment,
        "plan_tier": profile.plan_tier,
        "region": profile.region,
        "tenure_months": tenure_months,
    }


def compute_features(
    events: list[VisibleEvent], profile: AccountProfile, cutoff: datetime
) -> tuple[dict[str, FeatureValue], dict[str, FeatureProvenance]]:
    # Sorting here, not in SQL, makes order-dependent features independent of arrival order.
    ordered = sorted(events, key=lambda e: (e.event_time, e.id))
    values: dict[str, FeatureValue] = {}
    provenance: dict[str, FeatureProvenance] = {}
    for spec in EVENT_FEATURES:
        matched = [
            e for e in ordered if _matches(e, spec) and _in_window(e, cutoff, spec.window_days)
        ]
        values[spec.name] = spec.aggregate(matched, cutoff)
        provenance[spec.name] = FeatureProvenance(
            how=spec.how,
            window_days=spec.window_days,
            source=spec.source,
            event_ids=tuple(e.id for e in matched),
        )
    for name, value in _profile_values(profile, cutoff).items():
        values[name] = value
        provenance[name] = FeatureProvenance(
            how="account master, current value (no as-of history in the source)",
            window_days=None,
            source="accounts",
            event_ids=(),
        )
    return values, provenance


def latest_visible_version(events: list[VisibleEvent]) -> list[VisibleEvent]:
    """Keep one version per source record. A revised record supersedes its earlier version."""
    latest: dict[tuple[str, str], VisibleEvent] = {}
    for event in events:
        key = (event.source, event.source_record_id)
        current = latest.get(key)
        if current is None or (event.ingested_at, event.id) > (current.ingested_at, current.id):
            latest[key] = event
    return list(latest.values())


_EVENT_SQL = text(
    """
    SELECT id, source, source_record_id, event_type, event_time, ingested_at, attributes
    FROM source_events
    WHERE org_id = :org_id
      AND account_id = :account_id
      AND event_time <= :cutoff
      AND ingested_at <= :cutoff
    """
)

_PROFILE_SQL = text(
    "SELECT id, segment, plan_tier, region, created_at FROM accounts "
    "WHERE org_id = :org_id AND id = :account_id"
)


def load_snapshot(
    engine: Engine, org_id: UUID, account_id: UUID, cutoff: datetime
) -> FeatureSnapshot:
    with tenant_session(engine, org_id) as session:
        profile_row = session.execute(
            _PROFILE_SQL, {"org_id": org_id, "account_id": account_id}
        ).one()
        rows = session.execute(
            _EVENT_SQL, {"org_id": org_id, "account_id": account_id, "cutoff": cutoff}
        ).all()
    events = [
        VisibleEvent(
            id=str(row.id),
            source=row.source,
            source_record_id=row.source_record_id,
            event_type=row.event_type,
            event_time=row.event_time,
            ingested_at=row.ingested_at,
            attributes=_as_dict(row.attributes),
        )
        for row in rows
    ]
    profile = AccountProfile(
        account_id=str(profile_row.id),
        segment=profile_row.segment,
        plan_tier=profile_row.plan_tier,
        region=profile_row.region,
        created_at=profile_row.created_at,
    )
    values, provenance = compute_features(latest_visible_version(events), profile, cutoff)
    return FeatureSnapshot(
        org_id=org_id,
        account_id=account_id,
        cutoff_at=cutoff,
        feature_set_version=FEATURE_SET_VERSION,
        values=values,
        provenance=provenance,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if value else {}
