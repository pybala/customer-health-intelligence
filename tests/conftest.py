import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
TEST_PEPPER = "test-pepper-" + "x" * 32


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set. Run tests with: uv run --env-file .env pytest")
    return value


@dataclass(frozen=True)
class ThrowawayDatabase:
    name: str
    owner_url: str
    app_url: str


@contextmanager
def throwaway_database() -> Iterator[ThrowawayDatabase]:
    """Create an empty database owned by chi_owner and drop it on exit.

    Roles come from docker/postgres/initdb, so they exist in the cluster before this runs.
    """
    owner_base = make_url(_required_env("MIGRATION_DATABASE_URL"))
    app_base = make_url(_required_env("DATABASE_URL"))
    name = f"chi_test_{uuid.uuid4().hex[:10]}"

    admin_url = owner_base.set(
        username="postgres", password=_required_env("POSTGRES_PASSWORD"), database="postgres"
    )
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" OWNER chi_owner'))

    owner_url = owner_base.set(database=name).render_as_string(hide_password=False)
    app_url = app_base.set(database=name).render_as_string(hide_password=False)
    try:
        yield ThrowawayDatabase(
            name=name,
            owner_url=owner_url,
            app_url=app_url,
        )
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@contextmanager
def migration_env(owner_url: str) -> Iterator[None]:
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = owner_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@pytest.fixture(scope="session")
def test_database() -> Iterator[ThrowawayDatabase]:
    with throwaway_database() as db, migration_env(db.owner_url):
        command.upgrade(alembic_config(), "head")
        yield db


@pytest.fixture(scope="session")
def owner_engine(test_database: ThrowawayDatabase) -> Iterator[Engine]:
    engine = create_engine(test_database.owner_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(test_database: ThrowawayDatabase) -> Iterator[Engine]:
    engine = create_engine(test_database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def pepper() -> str:
    return TEST_PEPPER
