"""Base class for tenant-scoped data access.

A repository is bound to one organisation at construction. Every statement runs inside a
transaction whose tenant context is that organisation, and every query also filters on
org_id explicitly, so isolation does not depend on RLS alone.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import tenant_session


class TenantRepository:
    def __init__(self, engine: Engine, org_id: UUID) -> None:
        self._engine = engine
        self.org_id = org_id

    @contextmanager
    def session(self) -> Iterator[Session]:
        with tenant_session(self._engine, self.org_id) as session:
            yield session
