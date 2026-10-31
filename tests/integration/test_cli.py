"""Operator CLI commands run against a throwaway migrated database as chi_owner."""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app import cli
from app.config import Settings
from app.db.session import tenant_session
from app.security.api_keys import split_token
from tests.conftest import TEST_PEPPER, ThrowawayDatabase, migration_env

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def operator_env(test_database: ThrowawayDatabase) -> Iterator[None]:
    with (
        migration_env(test_database.owner_url),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setenv("API_KEY_PEPPER", TEST_PEPPER)
        yield


@pytest.fixture
def org_id(owner_engine: Engine, operator_env: None) -> uuid.UUID:
    org = cli.create_org(f"cli-{uuid.uuid4().hex[:8]}", "CLI Org")
    assert isinstance(org, uuid.UUID)
    return org


def test_create_org_inserts_a_row(owner_engine: Engine, operator_env: None) -> None:
    slug = f"created-{uuid.uuid4().hex[:8]}"

    org_id = cli.create_org(slug, "Created Org")

    with tenant_session(owner_engine, org_id) as session:
        row = session.execute(
            text("SELECT display_name, status FROM organizations WHERE id = :i"), {"i": org_id}
        ).one()
    assert row.display_name == "Created Org"
    assert row.status == "active"


def test_create_org_rejects_duplicate_slug(operator_env: None) -> None:
    from sqlalchemy.exc import IntegrityError

    slug = f"dup-{uuid.uuid4().hex[:8]}"
    cli.create_org(slug, "First")

    with pytest.raises(IntegrityError):
        cli.create_org(slug, "Second")


def test_issue_key_stores_hash_not_plaintext(
    owner_engine: Engine, operator_env: None, org_id: uuid.UUID
) -> None:
    plaintext = cli.issue_api_key(org_id, "admin")

    prefix, secret = split_token(plaintext) or ("", "")
    assert prefix and secret
    with tenant_session(owner_engine, org_id) as session:
        row = session.execute(
            text("SELECT key_hash, role, org_id FROM api_keys WHERE key_prefix = :p"),
            {"p": prefix},
        ).one()
    assert row.role == "admin"
    assert row.org_id == org_id
    assert secret not in row.key_hash
    assert plaintext not in row.key_hash


def test_issued_key_authenticates_through_the_app_role(
    app_engine: Engine, operator_env: None, org_id: uuid.UUID
) -> None:
    from app.security.principal import authenticate

    plaintext = cli.issue_api_key(org_id, "readonly")

    principal = authenticate(app_engine, TEST_PEPPER, plaintext)

    assert principal.org_id == org_id
    assert principal.role == "readonly"


def test_issue_key_rejects_unknown_role(operator_env: None, org_id: uuid.UUID) -> None:
    with pytest.raises(SystemExit):
        cli.main(["issue-key", "--org-id", str(org_id), "--role", "superuser"])


def test_main_create_org_prints_uuid(
    capsys: pytest.CaptureFixture[str], operator_env: None
) -> None:
    slug = f"main-{uuid.uuid4().hex[:8]}"

    assert cli.main(["create-org", "--slug", slug, "--name", "Main Org"]) == 0

    printed = capsys.readouterr().out.strip()
    uuid.UUID(printed)


def test_operator_commands_require_migration_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://chi_app:x@localhost/chi")
    monkeypatch.setenv("API_KEY_PEPPER", "p" * 40)

    with pytest.raises(SystemExit, match="MIGRATION_DATABASE_URL is required"):
        cli.create_org("nope", "Nope")


def test_export_openapi_check_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "OPENAPI_PATH", tmp_path / "openapi.json")
    monkeypatch.setattr(cli, "Settings", lambda: _settings())

    assert cli.export_openapi(check=True) == 1

    assert cli.export_openapi(check=False) == 0
    assert cli.export_openapi(check=True) == 0

    (tmp_path / "openapi.json").write_text("{}\n")
    assert cli.export_openapi(check=True) == 1


def test_committed_openapi_matches_application() -> None:
    from app.api.main import create_app

    committed = json.loads(cli.OPENAPI_PATH.read_text())
    live = create_app(_settings()).openapi()

    assert committed == json.loads(json.dumps(live))


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://chi_app:x@localhost/chi",
        api_key_pepper=TEST_PEPPER,
        _env_file=None,
    )


def test_register_marks_candidate_registered_for_its_tenant(
    owner_engine: Engine, operator_env: None
) -> None:
    from app.ingestion.canonical import org_id_for
    from app.ml.registry import register_version

    slug = f"reg-{uuid.uuid4().hex[:8]}"
    org_id = org_id_for(slug)
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Reg')"),
            {"i": org_id, "s": slug},
        )
    version_id = register_version(
        owner_engine,
        org_id,
        model_version="test-register-v1",
        model_kind="prior",
        feature_set_version="test",
        artifact_sha256="0" * 64,
        training_window={},
        metadata={},
    )

    assert cli.register_model("test-register-v1", slug) == 0

    with tenant_session(owner_engine, org_id) as session:
        status = session.execute(
            text("SELECT status FROM model_versions WHERE id = :i"), {"i": version_id}
        ).scalar_one()
    assert status == "registered"
