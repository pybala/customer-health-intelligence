"""Verify application roles exist before any tenant table is created.

Roles are created by the database bootstrap script (docker/postgres/initdb), not here,
because creating roles needs CREATEROLE and the migration role must not hold it.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_ROLES = ("chi_app",)


def upgrade() -> None:
    conn = op.get_bind()
    for role in REQUIRED_ROLES:
        present = conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        if not present:
            raise RuntimeError(
                f"Role {role} is missing. Create roles with docker/postgres/initdb/01-roles.sh."
            )


def downgrade() -> None:
    pass
