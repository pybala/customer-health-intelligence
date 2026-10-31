"""Tenancy, API keys, audit trail, idempotency, and row-level security.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("api_keys", "audit_events", "idempotency_keys")
CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE organizations (
            id uuid PRIMARY KEY,
            slug varchar(64) NOT NULL UNIQUE,
            display_name varchar(200) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            key_prefix varchar(32) NOT NULL UNIQUE,
            key_hash varchar(128) NOT NULL,
            role varchar(16) NOT NULL CHECK (role IN ('csm', 'admin', 'readonly')),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz NULL,
            last_used_at timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_api_keys_org_id ON api_keys (org_id)")
    op.execute(
        """
        CREATE TABLE audit_events (
            id bigserial PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            actor_key_id uuid NULL,
            event_type varchar(64) NOT NULL,
            entity_type varchar(64) NOT NULL,
            entity_id varchar(128) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_events_org_created ON audit_events (org_id, created_at)")
    op.execute(
        """
        CREATE TABLE idempotency_keys (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            key varchar(255) NOT NULL,
            request_hash varchar(64) NOT NULL,
            response_status integer NOT NULL,
            response_body jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CONSTRAINT uq_idempotency_org_key UNIQUE (org_id, key)
        )
        """
    )

    # Immutable audit trail: no role, including the owner, can alter or delete a row.
    op.execute(
        """
        CREATE FUNCTION reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
        """
    )

    # Tenant isolation. FORCE applies the policy to the table owner too.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (org_id = {CURRENT_ORG})
              WITH CHECK (org_id = {CURRENT_ORG})
            """
        )

    # Organisations: a tenant may see only its own row. Insert and delete are owner-only.
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY own_organization ON organizations FOR SELECT USING (id = {CURRENT_ORG})"
    )
    # Operator-only tenant creation, run as the owner role from the CLI.
    op.execute(
        "CREATE POLICY operator_create_organization ON organizations FOR INSERT TO chi_owner "
        "WITH CHECK (true)"
    )

    # Key lookup happens before tenant context exists. Permissive policies combine with OR,
    # so this only widens reads of api_keys to one prefix. Writes still need the tenant match.
    op.execute(
        """
        CREATE POLICY api_key_lookup ON api_keys FOR SELECT
          USING (key_prefix = NULLIF(current_setting('app.api_key_prefix', true), ''))
        """
    )

    # Application role: data access only, no DDL and no DELETE on audit or keys.
    op.execute("GRANT USAGE ON SCHEMA public TO chi_app")
    op.execute("GRANT SELECT ON organizations TO chi_app")
    op.execute("GRANT SELECT ON api_keys TO chi_app")
    op.execute("GRANT UPDATE (last_used_at) ON api_keys TO chi_app")
    op.execute("GRANT SELECT, INSERT ON audit_events TO chi_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE audit_events_id_seq TO chi_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO chi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP TABLE IF EXISTS audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS organizations")
