"""Tenant isolation exercised with direct SQL under the real application roles.

These tests do not use mocks. Each assertion runs as chi_app or chi_owner against the
migrated database, so a regression in a migration's policy or grant fails here.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db.session import tenant_session
from app.security.api_keys import issue_key

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class SeededTenant:
    org_id: uuid.UUID
    key_prefix: str
    idempotency_key: str


@pytest.fixture(scope="module")
def tenants(owner_engine: Engine, pepper: str) -> Iterator[tuple[SeededTenant, SeededTenant]]:
    seeded: list[SeededTenant] = []
    for label in ("a", "b"):
        org_id = uuid.uuid4()
        issued = issue_key(pepper)
        idem_key = f"seed-{label}-{uuid.uuid4().hex[:8]}"
        with owner_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
                {"i": org_id, "s": f"org-{label}-{org_id.hex[:6]}", "n": f"Org {label.upper()}"},
            )
        with tenant_session(owner_engine, org_id) as session:
            session.execute(
                text(
                    "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) "
                    "VALUES (:i, :o, :p, :h, 'csm')"
                ),
                {"i": uuid.uuid4(), "o": org_id, "p": issued.prefix, "h": issued.key_hash},
            )
            session.execute(
                text(
                    "INSERT INTO audit_events (org_id, event_type, entity_type, entity_id) "
                    "VALUES (:o, 'seed', 'organization', :e)"
                ),
                {"o": org_id, "e": str(org_id)},
            )
            session.execute(
                text(
                    "INSERT INTO idempotency_keys (id, org_id, key, request_hash, "
                    "response_status, response_body, expires_at) "
                    "VALUES (:i, :o, :k, 'h', 201, '{}'::jsonb, now() + interval '1 day')"
                ),
                {"i": uuid.uuid4(), "o": org_id, "k": idem_key},
            )
        seeded.append(SeededTenant(org_id, issued.prefix, idem_key))
    yield seeded[0], seeded[1]


def test_app_role_is_not_superuser_and_cannot_bypass_rls(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()

    assert row.rolsuper is False
    assert row.rolbypassrls is False


def test_app_role_owns_no_tenant_tables(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        owned = conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tableowner = current_user"
            )
        ).all()

    assert owned == []


def test_every_tenant_table_has_rls_and_force(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                "AND relname <> 'alembic_version'"
            )
        ).all()

    assert rows, "no tables found"
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows), rows


def test_no_tenant_context_returns_no_rows(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    with app_engine.connect() as conn, conn.begin():
        for table, query in (
            ("organizations", "SELECT count(*) FROM organizations"),
            ("api_keys", "SELECT count(*) FROM api_keys"),
            ("audit_events", "SELECT count(*) FROM audit_events"),
            ("idempotency_keys", "SELECT count(*) FROM idempotency_keys"),
        ):
            count = conn.execute(text(query)).scalar_one()
            assert count == 0, table


def test_cross_tenant_reads_are_invisible(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, b = tenants
    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )

        assert (
            conn.execute(
                text("SELECT count(*) FROM organizations WHERE id = :o"), {"o": b.org_id}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM audit_events WHERE org_id = :o"), {"o": b.org_id}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM idempotency_keys WHERE org_id = :o"), {"o": b.org_id}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM api_keys WHERE org_id = :o"), {"o": b.org_id}
            ).scalar_one()
            == 0
        )

        seen_orgs = conn.execute(text("SELECT DISTINCT org_id FROM audit_events")).scalars().all()
        assert set(seen_orgs) == {a.org_id}


def test_cross_tenant_write_is_rejected(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, b = tenants
    with pytest.raises(DBAPIError, match="row-level security"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (id, org_id, key, request_hash, response_status, "
                "response_body, expires_at) VALUES (:i, :o, 'x-cross', 'h', 201, '{}'::jsonb, "
                "now() + interval '1 day')"
            ),
            {"i": uuid.uuid4(), "o": b.org_id},
        )


def test_insert_without_tenant_context_is_rejected(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with pytest.raises(DBAPIError, match="row-level security"), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (id, org_id, key, request_hash, response_status, "
                "response_body, expires_at) VALUES (:i, :o, 'x-no-context', 'h', 201, "
                "'{}'::jsonb, now() + interval '1 day')"
            ),
            {"i": uuid.uuid4(), "o": a.org_id},
        )


def test_cross_tenant_update_affects_no_rows(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, b = tenants
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        result = conn.execute(
            text("UPDATE idempotency_keys SET response_status = 500 WHERE key = :k"),
            {"k": b.idempotency_key},
        )
        assert result.rowcount == 0

    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(b.org_id)}
        )
        status = conn.execute(
            text("SELECT response_status FROM idempotency_keys WHERE key = :k"),
            {"k": b.idempotency_key},
        ).scalar_one()
    assert status == 201


def test_key_lookup_is_limited_to_one_prefix(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, b = tenants
    with app_engine.connect() as conn, conn.begin():
        conn.execute(text("SELECT set_config('app.api_key_prefix', :p, true)"), {"p": a.key_prefix})
        rows = conn.execute(text("SELECT key_prefix FROM api_keys")).scalars().all()

    assert rows == [a.key_prefix]
    assert b.key_prefix not in rows


def test_key_lookup_without_prefix_returns_nothing(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    with app_engine.connect() as conn, conn.begin():
        assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0


def test_owner_is_also_subject_to_rls(
    owner_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    with owner_engine.connect() as conn, conn.begin():
        count = conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one()
    assert count == 0


def test_app_role_cannot_delete_audit_events(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(text("DELETE FROM audit_events"))


def test_app_role_cannot_update_audit_events(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(text("UPDATE audit_events SET event_type = 'tampered'"))


def test_audit_events_reject_update_and_delete_even_for_owner(
    owner_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with (
        tenant_session(owner_engine, a.org_id) as session,
        pytest.raises(DBAPIError, match="append-only"),
    ):
        session.execute(text("UPDATE audit_events SET event_type = 'tampered'"))
    with (
        tenant_session(owner_engine, a.org_id) as session,
        pytest.raises(DBAPIError, match="append-only"),
    ):
        session.execute(text("DELETE FROM audit_events"))


def _seed_snapshot_account(owner_engine: Engine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment, "
                "plan_tier, region, timezone, created_at) VALUES (:i, :o, 'Snap Co', :d, "
                "'smb', 'starter', 'na', 'UTC', now())"
            ),
            {"i": account_id, "o": org_id, "d": f"snap-{account_id.hex[:8]}.example.test"},
        )
    return account_id


_SNAPSHOT_INSERT = (
    "INSERT INTO feature_snapshots (id, org_id, account_id, cutoff_at, feature_set_version, "
    "values, provenance) VALUES (:i, :o, :a, :c, 'features-v1', '{}'::jsonb, '{}'::jsonb)"
)


def test_snapshot_insert_for_another_tenant_is_rejected(
    owner_engine: Engine, app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, b = tenants
    account_b = _seed_snapshot_account(owner_engine, b.org_id)
    with pytest.raises(DBAPIError, match="row-level security"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(
            text(_SNAPSHOT_INSERT),
            {"i": uuid.uuid4(), "o": b.org_id, "a": account_b, "c": "2026-01-01T00:00:00Z"},
        )


def test_snapshot_insert_without_tenant_context_is_rejected(
    owner_engine: Engine, app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    account_a = _seed_snapshot_account(owner_engine, a.org_id)
    with pytest.raises(DBAPIError, match="row-level security"), app_engine.begin() as conn:
        conn.execute(
            text(_SNAPSHOT_INSERT),
            {"i": uuid.uuid4(), "o": a.org_id, "a": account_a, "c": "2026-01-01T00:00:00Z"},
        )


def test_app_role_cannot_update_feature_snapshots(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(text("UPDATE feature_snapshots SET feature_set_version = 'tampered'"))


def test_app_role_cannot_delete_feature_snapshots(
    app_engine: Engine, tenants: tuple[SeededTenant, ...]
) -> None:
    a, _ = tenants
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(a.org_id)}
        )
        conn.execute(text("DELETE FROM feature_snapshots"))
