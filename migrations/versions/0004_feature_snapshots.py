"""Point-in-time feature snapshots with per-feature provenance.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE feature_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NOT NULL,
            cutoff_at timestamptz NOT NULL,
            feature_set_version varchar(32) NOT NULL,
            values jsonb NOT NULL,
            provenance jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_feature_snapshot_point
                UNIQUE (org_id, account_id, cutoff_at, feature_set_version),
            CONSTRAINT fk_feature_snapshot_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id)
        )
        """
    )
    op.execute("ALTER TABLE feature_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE feature_snapshots FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON feature_snapshots
          USING (org_id = {CURRENT_ORG})
          WITH CHECK (org_id = {CURRENT_ORG})
        """
    )
    # Snapshots are written once and never changed. The application role cannot UPDATE or DELETE.
    op.execute("GRANT SELECT, INSERT ON feature_snapshots TO chi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feature_snapshots")
