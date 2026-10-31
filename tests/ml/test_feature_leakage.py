"""Temporal leakage tests on the canonical fixture, using the real ingestion write path.

Perturbations are derived from an existing canonical event of the same account, so no separate
mock dataset is introduced. Each test asserts a feature snapshot at a past cutoff is unchanged
by an event that happened after the cutoff, or was ingested after it.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import NoResultFound

from app.db.session import tenant_session
from app.features.builder import load_snapshot
from app.ingestion.repository import IngestionRepository
from tests.ml.conftest import FEATURE_CUTOFF, SeededAccount

pytestmark = pytest.mark.integration


def _visible_product_event(
    engine: Engine, acct: SeededAccount
) -> tuple[uuid.UUID, dict[str, object]]:
    with tenant_session(engine, acct.org_id) as session:
        row = session.execute(
            text(
                "SELECT source_record_id, event_type, event_time, attributes, raw_record_id "
                "FROM source_events WHERE org_id = :o AND account_id = :a AND source = 'product' "
                "AND event_time <= :c AND ingested_at <= :c ORDER BY event_time LIMIT 1"
            ),
            {"o": acct.org_id, "a": acct.account_id, "c": FEATURE_CUTOFF},
        ).one()
    return acct.account_id, dict(row._mapping)


def _insert_clone(
    engine: Engine,
    acct: SeededAccount,
    template: dict[str, object],
    event_time: datetime,
    ingested_at: datetime,
    suffix: str,
) -> None:
    repo = IngestionRepository(engine, acct.org_id)
    with tenant_session(engine, acct.org_id) as session:
        raw_id = session.execute(
            text(
                "SELECT id FROM raw_source_records WHERE org_id = :o AND source = 'product' LIMIT 1"
            ),
            {"o": acct.org_id},
        ).scalar_one()
    repo.insert_events(
        [
            {
                "account_id": acct.account_id,
                "source": "product",
                "event_type": template["event_type"],
                "rid": f"leak-{suffix}",
                "dedupe_key": f"leak-{suffix}:0000000000",
                "event_time": event_time,
                "ingested_at": ingested_at,
                "identity_status": "matched",
                "attributes": template["attributes"],
                "raw_record_id": raw_id,
            }
        ]
    )


@pytest.fixture(scope="module")
def probe(seeded: list[SeededAccount], app_engine: Engine) -> SeededAccount:
    """An acme account with at least one product event visible at the cutoff.

    Some canonical accounts have no product usage at all, so the first acme account is not
    always a usable template.
    """
    for account in (a for a in seeded if a.slug == "acme-saas"):
        with tenant_session(app_engine, account.org_id) as session:
            found = session.execute(
                text(
                    "SELECT 1 FROM source_events WHERE org_id = :o AND account_id = :a "
                    "AND source = 'product' AND event_time <= :c AND ingested_at <= :c LIMIT 1"
                ),
                {"o": account.org_id, "a": account.account_id, "c": FEATURE_CUTOFF},
            ).first()
        if found is not None:
            return account
    pytest.fail("no acme account has a visible product event at the cutoff")


def test_future_event_does_not_change_the_snapshot(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    _, template = _visible_product_event(app_engine, probe)
    before = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    _insert_clone(
        app_engine,
        probe,
        template,
        event_time=FEATURE_CUTOFF + timedelta(days=1),
        ingested_at=FEATURE_CUTOFF + timedelta(days=1),
        suffix="future",
    )
    after = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    assert after.values == before.values
    assert after.provenance == before.provenance


def test_late_arrival_of_a_pre_cutoff_event_is_invisible_at_that_cutoff(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    _, template = _visible_product_event(app_engine, probe)
    before = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    _insert_clone(
        app_engine,
        probe,
        template,
        event_time=FEATURE_CUTOFF - timedelta(days=2),
        ingested_at=FEATURE_CUTOFF + timedelta(days=5),
        suffix="late",
    )
    after = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    assert after.values == before.values


def test_late_arrival_becomes_visible_at_a_later_cutoff(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    _, template = _visible_product_event(app_engine, probe)
    later = FEATURE_CUTOFF + timedelta(days=10)
    before = load_snapshot(app_engine, probe.org_id, probe.account_id, later)

    _insert_clone(
        app_engine,
        probe,
        template,
        event_time=FEATURE_CUTOFF - timedelta(days=2),
        ingested_at=FEATURE_CUTOFF + timedelta(days=5),
        suffix="late-visible",
    )
    after = load_snapshot(app_engine, probe.org_id, probe.account_id, later)

    assert after.values["product_events_30d"] == before.values["product_events_30d"] + 1


def test_snapshot_only_reads_events_visible_at_its_cutoff(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    snapshot = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    for name, prov in snapshot.provenance.items():
        for event_id in prov.event_ids:
            with tenant_session(app_engine, probe.org_id) as session:
                row = session.execute(
                    text(
                        "SELECT event_time, ingested_at FROM source_events "
                        "WHERE id = :i AND org_id = :o"
                    ),
                    {"i": uuid.UUID(event_id), "o": probe.org_id},
                ).one()
            assert row.event_time <= FEATURE_CUTOFF, name
            assert row.ingested_at <= FEATURE_CUTOFF, name


def test_a_snapshot_for_another_organisations_account_is_not_found(
    seeded: list[SeededAccount], app_engine: Engine
) -> None:
    other = next(a for a in seeded if a.slug == "northwind-cloud")
    acme = next(a for a in seeded if a.slug == "acme-saas")

    with pytest.raises(NoResultFound):
        load_snapshot(app_engine, acme.org_id, other.account_id, FEATURE_CUTOFF)


def test_shuffled_event_order_gives_identical_features(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    from app.features.builder import compute_features
    from app.features.definitions import AccountProfile, VisibleEvent

    snapshot = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)
    with tenant_session(app_engine, probe.org_id) as session:
        rows = session.execute(
            text(
                "SELECT id, source, source_record_id, event_type, event_time, ingested_at, "
                "attributes FROM source_events WHERE org_id = :o AND account_id = :a "
                "AND event_time <= :c AND ingested_at <= :c"
            ),
            {"o": probe.org_id, "a": probe.account_id, "c": FEATURE_CUTOFF},
        ).all()
        profile_row = session.execute(
            text("SELECT id, segment, plan_tier, region, created_at FROM accounts WHERE id = :a"),
            {"a": probe.account_id},
        ).one()
    events = [
        VisibleEvent(
            str(r.id),
            r.source,
            r.source_record_id,
            r.event_type,
            r.event_time,
            r.ingested_at,
            dict(r.attributes or {}),
        )
        for r in rows
    ]
    profile = AccountProfile(
        str(profile_row.id),
        profile_row.segment,
        profile_row.plan_tier,
        profile_row.region,
        profile_row.created_at,
    )
    reversed_values, _ = compute_features(list(reversed(events)), profile, FEATURE_CUTOFF)

    assert reversed_values == snapshot.values


def test_identical_inputs_give_identical_snapshots(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    first = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)
    second = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    assert first.values == second.values
    assert first.provenance == second.provenance


def test_event_exactly_at_the_cutoff_is_visible(
    seeded: list[SeededAccount], app_engine: Engine, probe: SeededAccount
) -> None:
    _, template = _visible_product_event(app_engine, probe)
    before = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    _insert_clone(
        app_engine,
        probe,
        template,
        event_time=FEATURE_CUTOFF,
        ingested_at=FEATURE_CUTOFF,
        suffix="at-cutoff",
    )
    after = load_snapshot(app_engine, probe.org_id, probe.account_id, FEATURE_CUTOFF)

    assert after.values["product_events_30d"] == before.values["product_events_30d"] + 1
