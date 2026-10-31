"""Migration round trip on its own throwaway database, leaving the shared test database alone."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import alembic_config, migration_env, throwaway_database

pytestmark = pytest.mark.integration

TENANT_TABLES = {
    "api_keys",
    "audit_events",
    "idempotency_keys",
    "organizations",
    "accounts",
    "account_identities",
    "raw_source_records",
    "source_events",
    "data_quality_issues",
    "recommended_actions",
    "action_approvals",
    "outbox_messages",
    "suppressions",
    "worker_runs",
}


def _public_tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names(schema="public")) - {"alembic_version"}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_round_trip() -> None:
    with throwaway_database() as db, migration_env(db.owner_url):
        cfg = alembic_config()

        command.upgrade(cfg, "head")
        assert _public_tables(db.owner_url) >= TENANT_TABLES

        command.downgrade(cfg, "base")
        assert _public_tables(db.owner_url) == set()

        command.upgrade(cfg, "head")
        assert _public_tables(db.owner_url) >= TENANT_TABLES


def test_downgrade_removes_audit_guard_function() -> None:
    with throwaway_database() as db, migration_env(db.owner_url):
        cfg = alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

        engine = create_engine(db.owner_url)
        try:
            with engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT to_regproc('reject_audit_mutation') IS NOT NULL")
                ).scalar_one()
        finally:
            engine.dispose()

    assert exists is False
