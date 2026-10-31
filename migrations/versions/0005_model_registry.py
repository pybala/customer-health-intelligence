"""Model registry, evaluations and predictions, tenant-scoped with RLS.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            model_version varchar(64) NOT NULL,
            model_kind varchar(32) NOT NULL,
            feature_set_version varchar(32) NOT NULL,
            artifact_sha256 varchar(64) NOT NULL,
            training_window jsonb NOT NULL,
            metadata jsonb NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'candidate'
                CHECK (status IN ('candidate', 'registered', 'retired')),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_model_version UNIQUE (org_id, model_version),
            CONSTRAINT uq_model_versions_org_id_id UNIQUE (org_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE model_evaluations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            model_version_id uuid NOT NULL,
            split varchar(16) NOT NULL CHECK (split IN ('validation', 'test')),
            scope varchar(64) NOT NULL,
            metrics jsonb NOT NULL,
            selection_hash varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_evaluation_model_version FOREIGN KEY (org_id, model_version_id)
                REFERENCES model_versions (org_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NOT NULL,
            model_version_id uuid NOT NULL,
            feature_set_version varchar(32) NOT NULL,
            cutoff_at timestamptz NOT NULL,
            predicted_at timestamptz NOT NULL DEFAULT now(),
            eligible boolean NOT NULL,
            ineligible_reason varchar(128),
            renewal_date timestamptz,
            risk_score double precision,
            feature_values jsonb,
            feature_provenance jsonb,
            CONSTRAINT fk_prediction_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id),
            CONSTRAINT fk_prediction_model_version FOREIGN KEY (org_id, model_version_id)
                REFERENCES model_versions (org_id, id),
            CONSTRAINT ck_prediction_risk_range
                CHECK (risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 1)),
            CONSTRAINT ck_prediction_eligibility CHECK (
                (eligible AND risk_score IS NOT NULL AND feature_values IS NOT NULL
                    AND feature_provenance IS NOT NULL AND ineligible_reason IS NULL)
                OR
                (NOT eligible AND risk_score IS NULL AND feature_values IS NULL
                    AND feature_provenance IS NULL AND ineligible_reason IS NOT NULL)
            )
        )
        """
    )
    for table in ("model_versions", "model_evaluations", "predictions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (org_id = {CURRENT_ORG})
              WITH CHECK (org_id = {CURRENT_ORG})
            """
        )
    # The owner (offline CLI) writes model_versions, model_evaluations and status changes.
    # The application role reads registered metadata and appends predictions; it never
    # updates or deletes a prediction or a version's status.
    op.execute("GRANT SELECT ON model_versions TO chi_app")
    op.execute("GRANT SELECT ON model_evaluations TO chi_app")
    op.execute("GRANT SELECT, INSERT ON predictions TO chi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS predictions")
    op.execute("DROP TABLE IF EXISTS model_evaluations")
    op.execute("DROP TABLE IF EXISTS model_versions")
