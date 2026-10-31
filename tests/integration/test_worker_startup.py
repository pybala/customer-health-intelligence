import uuid

import pytest
from sqlalchemy import Engine, text

from app.config import Settings
from app.worker.startup import WorkerConfigError, validate_worker_orgs

pytestmark = pytest.mark.integration


def _create_org(owner_engine: Engine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
            {"i": org_id, "s": f"{slug}-{org_id.hex[:6]}", "n": slug},
        )
    return org_id


def test_configured_orgs_are_returned_in_order(owner_engine: Engine, app_engine: Engine) -> None:
    first, second = _create_org(owner_engine, "w1"), _create_org(owner_engine, "w2")

    assert validate_worker_orgs(app_engine, [second, first]) == [second, first]


def test_unknown_org_stops_startup_and_is_named(owner_engine: Engine, app_engine: Engine) -> None:
    real, ghost = _create_org(owner_engine, "w3"), uuid.uuid4()

    with pytest.raises(WorkerConfigError, match=str(ghost)):
        validate_worker_orgs(app_engine, [real, ghost])


def test_empty_list_stops_startup(app_engine: Engine) -> None:
    with pytest.raises(WorkerConfigError, match="empty"):
        validate_worker_orgs(app_engine, [])


def _settings(value: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://u:p@localhost/db",
        api_key_pepper="p" * 32,
        worker_org_ids=value,  # type: ignore[arg-type]
        _env_file=None,
    )


def test_comma_separated_org_ids_are_parsed() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    assert _settings(f"{a}, {b}").worker_org_ids == [a, b]


def test_duplicate_or_malformed_org_ids_are_rejected() -> None:
    a = uuid.uuid4()

    with pytest.raises(ValueError, match="duplicates"):
        _settings(f"{a},{a}")
    with pytest.raises(ValueError, match="UUID"):
        _settings("not-a-uuid")
