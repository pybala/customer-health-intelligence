from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def _set_local(session: Session, name: str, value: str) -> None:
    # set_config with is_local=true behaves like SET LOCAL and resets at transaction end,
    # so a pooled connection never carries one request's tenant into the next.
    session.execute(text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value})


def missing_organizations(engine: Engine, org_ids: Iterable[UUID]) -> list[UUID]:
    """IDs not found in organizations. RLS stays on, so each ID is checked under its own context."""
    missing: list[UUID] = []
    for org_id in org_ids:
        with tenant_session(engine, org_id) as session:
            found = session.execute(
                text("SELECT 1 FROM organizations WHERE id = :id"), {"id": org_id}
            ).first()
        if found is None:
            missing.append(org_id)
    return missing


@contextmanager
def tenant_session(engine: Engine, org_id: UUID) -> Iterator[Session]:
    with Session(engine) as session, session.begin():
        _set_local(session, "app.current_org_id", str(org_id))
        yield session
