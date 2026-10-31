"""Organisation slug collisions fail with a clear fixture error, in a throwaway database."""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, text

from app.ingestion.canonical import FixtureError, ensure_organizations, org_id_for
from tests.conftest import alembic_config, migration_env, throwaway_database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def owner_db() -> Iterator[Engine]:
    with throwaway_database() as db, migration_env(db.owner_url):
        command.upgrade(alembic_config(), "head")
        engine = create_engine(db.owner_url)
        yield engine
        engine.dispose()


def _set_tenant(conn: Connection, org_id: uuid.UUID) -> None:
    conn.execute(text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)})


def _insert_org(engine: Engine, org_id: uuid.UUID, slug: str) -> None:
    with engine.begin() as conn:
        _set_tenant(conn, org_id)
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Other')"),
            {"i": org_id, "s": slug},
        )


def _ids_with_slug(engine: Engine, tenant: uuid.UUID, slug: str) -> list[uuid.UUID]:
    with engine.begin() as conn:
        _set_tenant(conn, tenant)
        rows = conn.execute(text("SELECT id FROM organizations WHERE slug = :s"), {"s": slug})
        return [row.id for row in rows]


def test_new_organisation_is_created_once_and_reused(owner_db: Engine) -> None:
    first = ensure_organizations(owner_db, {"acme-saas": "Acme SaaS"})
    second = ensure_organizations(owner_db, {"acme-saas": "Acme SaaS"})

    assert first == second == {"acme-saas": org_id_for("acme-saas")}
    assert _ids_with_slug(owner_db, org_id_for("acme-saas"), "acme-saas") == [
        org_id_for("acme-saas")
    ]


def test_slug_held_by_a_different_id_is_a_fixture_error_not_an_overwrite(
    owner_db: Engine,
) -> None:
    taken_id = uuid.uuid4()
    _insert_org(owner_db, taken_id, "globex-data")

    with pytest.raises(FixtureError, match="globex-data"):
        ensure_organizations(owner_db, {"globex-data": "Globex Data"})

    assert _ids_with_slug(owner_db, taken_id, "globex-data") == [taken_id]


def test_collision_does_not_block_later_organisations(owner_db: Engine) -> None:
    _insert_org(owner_db, uuid.uuid4(), "northwind-taken")

    with pytest.raises(FixtureError):
        ensure_organizations(owner_db, {"northwind-taken": "Taken"})
    ids = ensure_organizations(owner_db, {"fresh-org": "Fresh"})

    assert ids == {"fresh-org": org_id_for("fresh-org")}
