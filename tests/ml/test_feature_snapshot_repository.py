"""Feature snapshots round-trip through the database with values and provenance intact."""

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app.features.builder import load_snapshot
from app.features.definitions import FEATURE_SET_VERSION
from app.features.repository import read_snapshot, save_snapshot
from tests.ml.conftest import FEATURE_CUTOFF, SeededAccount

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def target(seeded: list[SeededAccount]) -> SeededAccount:
    return next(a for a in seeded if a.slug == "acme-saas")


def test_saved_snapshot_reads_back_with_identical_values_and_provenance(
    seeded: list[SeededAccount], app_engine: Engine, target: SeededAccount
) -> None:
    snapshot = load_snapshot(app_engine, target.org_id, target.account_id, FEATURE_CUTOFF)
    save_snapshot(app_engine, snapshot)

    stored = read_snapshot(
        app_engine, target.org_id, target.account_id, FEATURE_CUTOFF, FEATURE_SET_VERSION
    )

    assert stored is not None
    assert stored["values"] == snapshot.values
    expected = {
        name: {
            "how": p.how,
            "window_days": p.window_days,
            "source": p.source,
            "event_ids": list(p.event_ids),
        }
        for name, p in snapshot.provenance.items()
    }
    assert stored["provenance"] == expected


def test_saving_the_same_snapshot_twice_stores_one_row(
    seeded: list[SeededAccount], app_engine: Engine, target: SeededAccount
) -> None:
    cutoff = FEATURE_CUTOFF + timedelta(days=1)
    snapshot = load_snapshot(app_engine, target.org_id, target.account_id, cutoff)
    save_snapshot(app_engine, snapshot)
    save_snapshot(app_engine, snapshot)

    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(target.org_id)}
        )
        count = conn.execute(
            text("SELECT count(*) FROM feature_snapshots WHERE account_id = :a AND cutoff_at = :c"),
            {"a": target.account_id, "c": cutoff},
        ).scalar_one()
    assert count == 1


def test_reading_a_missing_snapshot_returns_none(
    seeded: list[SeededAccount], app_engine: Engine, target: SeededAccount
) -> None:
    missing = read_snapshot(
        app_engine,
        target.org_id,
        target.account_id,
        FEATURE_CUTOFF - timedelta(days=400),
        FEATURE_SET_VERSION,
    )

    assert missing is None


def test_snapshots_are_keyed_by_feature_set_version(
    seeded: list[SeededAccount], app_engine: Engine, target: SeededAccount
) -> None:
    snapshot = load_snapshot(app_engine, target.org_id, target.account_id, FEATURE_CUTOFF)
    save_snapshot(app_engine, snapshot)

    other_version = read_snapshot(
        app_engine, target.org_id, target.account_id, FEATURE_CUTOFF, "features-v0"
    )

    assert other_version is None


def test_another_organisations_snapshot_is_not_read(
    seeded: list[SeededAccount], app_engine: Engine, target: SeededAccount
) -> None:
    other = next(a for a in seeded if a.slug == "northwind-cloud")
    snapshot = load_snapshot(app_engine, target.org_id, target.account_id, FEATURE_CUTOFF)
    save_snapshot(app_engine, snapshot)

    leaked = read_snapshot(
        app_engine, other.org_id, target.account_id, FEATURE_CUTOFF, FEATURE_SET_VERSION
    )

    assert leaked is None
