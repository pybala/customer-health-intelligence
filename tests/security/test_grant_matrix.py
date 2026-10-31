"""Exact privilege set for each application role, read from the catalog.

If a migration widens or narrows a grant, this test fails until the expected set below is
updated on purpose. That keeps least-privilege changes visible in review.
"""

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration

EXPECTED_APP_PRIVILEGES: dict[str, frozenset[str]] = {
    "organizations": frozenset({"SELECT"}),
    "api_keys": frozenset({"SELECT"}),
    "audit_events": frozenset({"SELECT", "INSERT"}),
    "idempotency_keys": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "accounts": frozenset({"SELECT", "INSERT"}),
    "account_identities": frozenset({"SELECT", "INSERT"}),
    "raw_source_records": frozenset({"SELECT", "INSERT"}),
    "source_events": frozenset({"SELECT", "INSERT"}),
    "data_quality_issues": frozenset({"SELECT", "INSERT"}),
    "feature_snapshots": frozenset({"SELECT", "INSERT"}),
    "model_versions": frozenset({"SELECT"}),
    "model_evaluations": frozenset({"SELECT"}),
    "predictions": frozenset({"SELECT", "INSERT"}),
    "recommended_actions": frozenset({"SELECT", "INSERT"}),
    "action_approvals": frozenset({"SELECT", "INSERT"}),
    "outbox_messages": frozenset({"SELECT", "INSERT"}),
    "suppressions": frozenset({"SELECT", "INSERT"}),
    "worker_runs": frozenset({"SELECT", "INSERT"}),
}

EXPECTED_APP_API_KEY_UPDATE_COLUMNS = frozenset({"last_used_at"})

EXPECTED_APP_COLUMN_UPDATES: dict[str, frozenset[str]] = {
    "recommended_actions": frozenset({"status"}),
    "outbox_messages": frozenset(
        {
            "status",
            "attempts",
            "next_attempt_at",
            "last_error",
            "locked_by",
            "locked_until",
            "finished_at",
        }
    ),
    "suppressions": frozenset({"released_at", "released_by"}),
    "worker_runs": frozenset({"status", "finished_at", "counts", "error"}),
}


def _table_privileges(conn: Connection, grantee: str) -> dict[str, frozenset[str]]:
    rows = conn.execute(
        text(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND grantee = :g"
        ),
        {"g": grantee},
    ).all()
    result: dict[str, set[str]] = {}
    for table, privilege in rows:
        result.setdefault(table, set()).add(privilege)
    return {table: frozenset(privs) for table, privs in result.items()}


def _column_updates(conn: Connection, grantee: str, table: str) -> frozenset[str]:
    # information_schema expands table-level grants into every column, so this is only
    # meaningful for a table that has no table-level UPDATE.
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE table_schema = 'public' AND table_name = :t AND grantee = :g "
            "AND privilege_type = 'UPDATE'"
        ),
        {"t": table, "g": grantee},
    ).all()
    return frozenset(row[0] for row in rows)


def test_app_role_has_exactly_the_expected_table_privileges(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        actual = _table_privileges(conn, "chi_app")

    assert actual == EXPECTED_APP_PRIVILEGES


def test_app_role_can_update_only_last_used_at_on_api_keys(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        actual = _column_updates(conn, "chi_app", "api_keys")

    assert actual == EXPECTED_APP_API_KEY_UPDATE_COLUMNS


def test_app_role_updates_only_the_lifecycle_columns_of_new_tables(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        actual = {
            table: _column_updates(conn, "chi_app", table) for table in EXPECTED_APP_COLUMN_UPDATES
        }

    assert actual == EXPECTED_APP_COLUMN_UPDATES


def test_app_role_has_no_ddl_or_sequence_beyond_audit_sequence(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        sequences = conn.execute(
            text(
                "SELECT object_name, privilege_type FROM information_schema.usage_privileges "
                "WHERE object_type = 'SEQUENCE' AND grantee = 'chi_app'"
            )
        ).all()
        schema_create = conn.execute(
            text("SELECT has_schema_privilege('chi_app', 'public', 'CREATE')")
        ).scalar_one()

    assert [tuple(row) for row in sequences] == [("audit_events_id_seq", "USAGE")]
    assert schema_create is False


def test_app_role_cannot_create_tables(app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(text("CREATE TABLE should_not_exist (id int)"))


def test_app_role_cannot_truncate_or_alter_tenant_tables(app_engine: Engine) -> None:
    for statement in (
        "TRUNCATE api_keys",
        "ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY",
        "DROP POLICY tenant_isolation ON api_keys",
    ):
        with (
            pytest.raises(ProgrammingError, match="must be owner|permission denied"),
            app_engine.begin() as conn,
        ):
            conn.execute(text(statement))
