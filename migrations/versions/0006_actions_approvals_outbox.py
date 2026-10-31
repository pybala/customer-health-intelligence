"""Recommended actions, human approvals, outbox, suppressions and worker runs, tenant-scoped.

An outbox message can exist only for an action that has an approved decision and is in the
approved state. The database enforces this, not just the worker.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
TENANT_TABLES = (
    "recommended_actions",
    "action_approvals",
    "outbox_messages",
    "suppressions",
    "worker_runs",
)
APPEND_ONLY_TABLES = ("action_approvals",)


def upgrade() -> None:
    # A composite key lets an action reference the account and prediction it was built from,
    # so the action cannot point at another account's prediction.
    op.execute(
        "ALTER TABLE predictions ADD CONSTRAINT uq_predictions_org_id_id_account "
        "UNIQUE (org_id, id, account_id)"
    )
    op.execute(
        """
        CREATE TABLE recommended_actions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NOT NULL,
            prediction_id uuid NOT NULL,
            action_type varchar(32) NOT NULL CHECK (action_type IN ('csm_outreach_email')),
            status varchar(16) NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded')),
            reason_code varchar(64) NOT NULL,
            evidence jsonb NOT NULL,
            proposed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_recommended_actions_org_id_id UNIQUE (org_id, id),
            CONSTRAINT uq_recommended_action_per_prediction
                UNIQUE (org_id, prediction_id, action_type),
            CONSTRAINT fk_action_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id),
            CONSTRAINT fk_action_prediction FOREIGN KEY (org_id, prediction_id, account_id)
                REFERENCES predictions (org_id, id, account_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_recommended_actions_org_status ON recommended_actions (org_id, status)"
    )
    op.execute(
        """
        CREATE TABLE action_approvals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            action_id uuid NOT NULL,
            decision varchar(8) NOT NULL CHECK (decision IN ('approved', 'rejected')),
            actor_id uuid NOT NULL,
            reason varchar(500) NULL,
            decided_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_action_approvals_org_id_id_action UNIQUE (org_id, id, action_id),
            CONSTRAINT uq_action_one_decision UNIQUE (org_id, action_id),
            CONSTRAINT fk_approval_action FOREIGN KEY (org_id, action_id)
                REFERENCES recommended_actions (org_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE outbox_messages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            action_id uuid NOT NULL,
            approval_id uuid NOT NULL,
            channel varchar(16) NOT NULL CHECK (channel IN ('email')),
            delivery_mode varchar(16) NOT NULL DEFAULT 'dry_run'
                CHECK (delivery_mode IN ('dry_run', 'simulated')),
            status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'sending', 'sent', 'dry_run_recorded', 'suppressed', 'dead_letter'
            )),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
            payload jsonb NOT NULL,
            payload_sha256 char(64) NOT NULL,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            locked_by varchar(64) NULL,
            locked_until timestamptz NULL,
            last_error varchar(500) NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz NULL,
            CONSTRAINT uq_outbox_org_id_id UNIQUE (org_id, id),
            CONSTRAINT uq_outbox_one_message_per_action UNIQUE (org_id, action_id),
            CONSTRAINT fk_outbox_action FOREIGN KEY (org_id, action_id)
                REFERENCES recommended_actions (org_id, id),
            CONSTRAINT fk_outbox_approval FOREIGN KEY (org_id, approval_id, action_id)
                REFERENCES action_approvals (org_id, id, action_id),
            CONSTRAINT ck_outbox_finished_iff_terminal CHECK (
                (status IN ('sent', 'dry_run_recorded', 'suppressed', 'dead_letter'))
                = (finished_at IS NOT NULL)
            ),
            CONSTRAINT ck_outbox_lock_iff_sending CHECK (
                (status = 'sending') = (locked_until IS NOT NULL)
                AND (locked_by IS NULL) = (locked_until IS NULL)
            ),
            CONSTRAINT ck_outbox_mode_matches_status CHECK (
                NOT (delivery_mode = 'dry_run' AND status = 'sent')
                AND NOT (delivery_mode = 'simulated' AND status = 'dry_run_recorded')
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_outbox_due ON outbox_messages (org_id, status, next_attempt_at)")
    op.execute(
        """
        CREATE TABLE suppressions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            account_id uuid NOT NULL,
            reason varchar(32) NOT NULL
                CHECK (reason IN ('legal_hold', 'critical_escalation', 'recent_contact')),
            starts_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NULL,
            created_by uuid NOT NULL,
            released_at timestamptz NULL,
            released_by uuid NULL,
            source_ref varchar(128) NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_suppression_window CHECK (expires_at IS NULL OR expires_at > starts_at),
            CONSTRAINT ck_suppression_release CHECK (
                (released_at IS NULL) = (released_by IS NULL)
                AND (released_at IS NULL OR released_at >= starts_at)
            ),
            CONSTRAINT fk_suppression_account FOREIGN KEY (org_id, account_id)
                REFERENCES accounts (org_id, id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_open_legal_hold ON suppressions (org_id, account_id) "
        "WHERE reason = 'legal_hold' AND released_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE worker_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES organizations(id),
            mode varchar(16) NOT NULL CHECK (mode IN ('dry_run', 'simulated')),
            status varchar(16) NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz NULL,
            counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            error varchar(500) NULL,
            CONSTRAINT ck_worker_run_finished_iff_done
                CHECK ((status = 'running') = (finished_at IS NULL))
        )
        """
    )

    _create_guard_functions()

    op.execute(
        "CREATE TRIGGER recommended_action_guard BEFORE UPDATE ON recommended_actions "
        "FOR EACH ROW EXECUTE FUNCTION recommended_action_guard()"
    )
    op.execute(
        "CREATE TRIGGER action_approval_requires_proposed BEFORE INSERT ON action_approvals "
        "FOR EACH ROW EXECUTE FUNCTION action_approval_requires_proposed()"
    )
    op.execute(
        "CREATE TRIGGER outbox_requires_approved_action BEFORE INSERT ON outbox_messages "
        "FOR EACH ROW EXECUTE FUNCTION outbox_requires_approved_action()"
    )
    op.execute(
        "CREATE TRIGGER outbox_transition_guard BEFORE UPDATE ON outbox_messages "
        "FOR EACH ROW EXECUTE FUNCTION outbox_transition_guard()"
    )
    op.execute(
        "CREATE TRIGGER worker_run_guard BEFORE UPDATE ON worker_runs "
        "FOR EACH ROW EXECUTE FUNCTION worker_run_guard()"
    )
    op.execute(
        "CREATE TRIGGER suppression_guard BEFORE UPDATE OR DELETE ON suppressions "
        "FOR EACH ROW EXECUTE FUNCTION suppression_guard()"
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_row_mutation()"
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

    # Least privilege. Column-level UPDATE limits the application role to the lifecycle fields.
    # Nothing here grants DELETE, and approvals are never updated.
    op.execute("GRANT SELECT, INSERT ON recommended_actions TO chi_app")
    op.execute("GRANT UPDATE (status) ON recommended_actions TO chi_app")
    op.execute("GRANT SELECT, INSERT ON action_approvals TO chi_app")
    op.execute("GRANT SELECT, INSERT ON outbox_messages TO chi_app")
    op.execute(
        "GRANT UPDATE (status, attempts, next_attempt_at, last_error, locked_by, locked_until, "
        "finished_at) ON outbox_messages TO chi_app"
    )
    op.execute("GRANT SELECT, INSERT ON suppressions TO chi_app")
    op.execute("GRANT UPDATE (released_at, released_by) ON suppressions TO chi_app")
    op.execute("GRANT SELECT, INSERT ON worker_runs TO chi_app")
    op.execute("GRANT UPDATE (status, finished_at, counts, error) ON worker_runs TO chi_app")


def _create_guard_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_row_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION suppression_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'suppressions cannot be deleted';
            END IF;
            IF (NEW.id, NEW.org_id, NEW.account_id, NEW.reason, NEW.starts_at, NEW.expires_at,
                NEW.created_by, NEW.source_ref, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.org_id, OLD.account_id, OLD.reason, OLD.starts_at, OLD.expires_at,
                OLD.created_by, OLD.source_ref, OLD.created_at) THEN
                RAISE EXCEPTION 'only a release can change a suppression';
            END IF;
            IF OLD.released_at IS NOT NULL THEN
                RAISE EXCEPTION 'suppression % is already released', OLD.id;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION action_approval_requires_proposed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM recommended_actions ra
                WHERE ra.org_id = NEW.org_id AND ra.id = NEW.action_id AND ra.status = 'proposed'
            ) THEN
                RAISE EXCEPTION 'an approval decision needs a proposed action';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    # The status moves to approved or rejected only after the matching decision row exists.
    # Approval rows are inserted first in the same transaction, so this check sees them.
    op.execute(
        """
        CREATE FUNCTION recommended_action_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.org_id, NEW.account_id, NEW.prediction_id, NEW.action_type,
                NEW.reason_code, NEW.evidence)
               IS DISTINCT FROM
               (OLD.id, OLD.org_id, OLD.account_id, OLD.prediction_id, OLD.action_type,
                OLD.reason_code, OLD.evidence) THEN
                RAISE EXCEPTION 'recommended action content is immutable';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF OLD.status <> 'proposed' THEN
                    RAISE EXCEPTION 'recommended action % is %, its status cannot change',
                        OLD.id, OLD.status;
                END IF;
                IF NEW.status IN ('approved', 'rejected') AND NOT EXISTS (
                    SELECT 1 FROM action_approvals ap
                    WHERE ap.org_id = NEW.org_id AND ap.action_id = NEW.id
                      AND ap.decision = NEW.status
                ) THEN
                    RAISE EXCEPTION 'status % needs a matching approval decision', NEW.status;
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION outbox_requires_approved_action() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM action_approvals ap
                JOIN recommended_actions ra
                  ON ra.org_id = ap.org_id AND ra.id = ap.action_id
                WHERE ap.org_id = NEW.org_id AND ap.id = NEW.approval_id
                  AND ap.action_id = NEW.action_id
                  AND ap.decision = 'approved' AND ra.status = 'approved'
            ) THEN
                RAISE EXCEPTION 'outbox message requires an approved action';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION outbox_transition_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.id, NEW.org_id, NEW.action_id, NEW.approval_id, NEW.channel,
                NEW.delivery_mode, NEW.payload, NEW.payload_sha256, NEW.max_attempts,
                NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.org_id, OLD.action_id, OLD.approval_id, OLD.channel,
                OLD.delivery_mode, OLD.payload, OLD.payload_sha256, OLD.max_attempts,
                OLD.created_at) THEN
                RAISE EXCEPTION 'outbox message content is immutable';
            END IF;
            IF OLD.status IN ('sent', 'dry_run_recorded', 'suppressed', 'dead_letter') THEN
                RAISE EXCEPTION 'outbox message % is terminal (%)', OLD.id, OLD.status;
            END IF;
            IF NEW.status = OLD.status THEN
                IF NEW.attempts <> OLD.attempts THEN
                    RAISE EXCEPTION 'attempts change only on a claim or a terminal move';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status = 'pending' AND NEW.status = 'sending' THEN
                IF OLD.attempts >= OLD.max_attempts OR NEW.attempts <> OLD.attempts + 1 THEN
                    RAISE EXCEPTION 'a claim needs an attempt below the maximum';
                END IF;
            ELSIF OLD.status = 'pending' AND NEW.status IN ('suppressed', 'dry_run_recorded') THEN
                IF NEW.attempts <> OLD.attempts THEN
                    RAISE EXCEPTION 'a terminal move without a claim cannot change attempts';
                END IF;
            ELSIF OLD.status = 'sending' AND NEW.status = 'sent' THEN
                IF NEW.attempts <> OLD.attempts THEN
                    RAISE EXCEPTION 'delivery cannot change attempts';
                END IF;
            ELSIF OLD.status = 'sending' AND NEW.status = 'pending' THEN
                IF NEW.attempts <> OLD.attempts OR NEW.attempts >= NEW.max_attempts THEN
                    RAISE EXCEPTION 'a retry is allowed only below the maximum attempts';
                END IF;
            ELSIF OLD.status = 'sending' AND NEW.status = 'dead_letter' THEN
                IF NEW.attempts <> OLD.attempts OR NEW.attempts < NEW.max_attempts THEN
                    RAISE EXCEPTION 'dead letter is allowed only at the maximum attempts';
                END IF;
            ELSE
                RAISE EXCEPTION 'invalid outbox transition % -> %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION worker_run_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION 'worker run % is already finished', OLD.id;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )


def downgrade() -> None:
    for table in ("outbox_messages", "action_approvals", "suppressions", "worker_runs"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP TABLE IF EXISTS recommended_actions")
    op.execute("ALTER TABLE predictions DROP CONSTRAINT IF EXISTS uq_predictions_org_id_id_account")
    for function in (
        "reject_row_mutation()",
        "suppression_guard()",
        "action_approval_requires_proposed()",
        "recommended_action_guard()",
        "outbox_requires_approved_action()",
        "outbox_transition_guard()",
        "worker_run_guard()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
