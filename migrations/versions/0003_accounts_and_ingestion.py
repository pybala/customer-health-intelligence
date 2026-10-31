"""Accounts, identities, raw landing, normalised events and data quality issues.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "accounts",
    "account_identities",
    "raw_source_records",
    "source_events",
    "data_quality_issues",
)
CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
SOURCES = "('salesforce', 'gmail', 'zendesk', 'product', 'stripe')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE accounts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            canonical_name varchar(200) NOT NULL,
            primary_domain varchar(255) NOT NULL,
            segment varchar(16) NOT NULL CHECK (segment IN ('smb', 'mid_market', 'enterprise')),
            plan_tier varchar(16) NOT NULL,
            region varchar(16) NOT NULL,
            timezone varchar(64) NOT NULL DEFAULT 'UTC',
            created_at timestamptz NOT NULL,
            renewal_date timestamptz NULL,
            CONSTRAINT uq_accounts_org_id_id UNIQUE (org_id, id),
            CONSTRAINT uq_accounts_org_domain UNIQUE (org_id, primary_domain)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE account_identities (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NOT NULL,
            source varchar(16) NOT NULL CHECK (source IN {SOURCES}),
            source_account_key varchar(255) NOT NULL,
            match_method varchar(16) NOT NULL
                CHECK (match_method IN ('exact_id', 'domain', 'fuzzy_name', 'manual')),
            match_confidence numeric(4,3) NOT NULL CHECK (match_confidence BETWEEN 0 AND 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_identity_org_source_key UNIQUE (org_id, source, source_account_key),
            CONSTRAINT fk_identity_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE raw_source_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            source varchar(16) NOT NULL CHECK (source IN {SOURCES}),
            source_record_id varchar(255) NOT NULL,
            payload jsonb NOT NULL,
            payload_sha256 char(64) NOT NULL,
            received_at timestamptz NOT NULL,
            ingest_batch_id uuid NOT NULL,
            CONSTRAINT uq_raw_org_source_record_hash
                UNIQUE (org_id, source, source_record_id, payload_sha256),
            CONSTRAINT uq_raw_org_id_id UNIQUE (org_id, id)
        )
        """
    )
    # dedupe_key is source_record_id plus a content hash prefix, so a revised record is a new
    # event (append-only, visible only from its own ingested_at) and an exact re-send is a no-op.
    op.execute(
        f"""
        CREATE TABLE source_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NULL,
            source varchar(16) NOT NULL CHECK (source IN {SOURCES}),
            event_type varchar(64) NOT NULL,
            source_record_id varchar(255) NOT NULL,
            dedupe_key varchar(300) NOT NULL,
            event_time timestamptz NOT NULL,
            ingested_at timestamptz NOT NULL,
            identity_status varchar(16) NOT NULL
                CHECK (identity_status IN ('matched', 'unmatched', 'ambiguous')),
            attributes jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            raw_record_id uuid NOT NULL,
            CONSTRAINT uq_events_org_source_dedupe UNIQUE (org_id, source, dedupe_key),
            CONSTRAINT fk_events_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id),
            CONSTRAINT fk_events_raw FOREIGN KEY (org_id, raw_record_id)
                REFERENCES raw_source_records (org_id, id),
            CONSTRAINT ck_events_matched_has_account
                CHECK ((identity_status = 'matched') = (account_id IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_events_org_account_time "
        "ON source_events (org_id, account_id, event_time)"
    )
    op.execute("CREATE INDEX ix_source_events_org_ingested ON source_events (org_id, ingested_at)")
    op.execute(
        """
        CREATE TABLE data_quality_issues (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            run_id uuid NULL,
            kind varchar(32) NOT NULL CHECK (kind IN (
                'duplicate', 'late_arrival', 'out_of_order', 'unmatched_identity',
                'ambiguous_identity', 'missing_source', 'revised_record', 'timezone_assumed',
                'malformed_record'
            )),
            source varchar(16) NOT NULL,
            ref varchar(400) NOT NULL,
            detected_at timestamptz NOT NULL DEFAULT now(),
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT uq_dq_org_kind_source_ref UNIQUE (org_id, kind, source, ref)
        )
        """
    )

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

    # Raw and normalised data is append-only for the application role: no UPDATE, no DELETE.
    # An unresolved identity stays unresolved and visible; it is never patched in retrospect.
    op.execute("GRANT SELECT, INSERT ON accounts TO chi_app")
    op.execute("GRANT SELECT, INSERT ON account_identities TO chi_app")
    op.execute("GRANT SELECT, INSERT ON raw_source_records TO chi_app")
    op.execute("GRANT SELECT, INSERT ON source_events TO chi_app")
    op.execute("GRANT SELECT, INSERT ON data_quality_issues TO chi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS data_quality_issues")
    op.execute("DROP TABLE IF EXISTS source_events")
    op.execute("DROP TABLE IF EXISTS raw_source_records")
    op.execute("DROP TABLE IF EXISTS account_identities")
    op.execute("DROP TABLE IF EXISTS accounts")
