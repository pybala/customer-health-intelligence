"""Worker startup validation: the configured organisation list must be usable before any work."""

from uuid import UUID

from sqlalchemy import Engine

from app.db.session import missing_organizations


class WorkerConfigError(RuntimeError):
    pass


def validate_worker_orgs(engine: Engine, org_ids: list[UUID]) -> list[UUID]:
    """Return the org IDs in configured order, or raise. Each ID is checked under its own tenant."""
    if not org_ids:
        raise WorkerConfigError("WORKER_ORG_IDS is empty; the worker has no organisations to serve")
    missing = missing_organizations(engine, org_ids)
    if missing:
        listed = ", ".join(str(org) for org in missing)
        raise WorkerConfigError(
            f"WORKER_ORG_IDS references organisations that do not exist: {listed}"
        )
    return list(org_ids)
