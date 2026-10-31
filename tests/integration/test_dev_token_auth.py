import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.main import DEV_AUTH_WARNING, create_app
from app.config import Settings
from app.security.dev_tokens import audit_actor_id

pytestmark = pytest.mark.integration

PROBLEM = "application/problem+json"
ENDPOINT = "/api/v1/accounts"
APPROVE = "/api/v1/actions/x/approvals"


@dataclass(frozen=True)
class DevOrg:
    org_id: uuid.UUID
    token: str


def _create_org(owner_engine: Engine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
            {"i": org_id, "s": f"{slug}-{org_id.hex[:6]}", "n": slug},
        )
    return org_id


def _settings(database_url: str, pepper: str, tokens_file: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url=database_url,
        api_key_pepper=pepper,
        auth_mode="dev_tokens",
        dev_tokens_file=tokens_file,
        _env_file=None,
    )


def _write_tokens(path: Path, entries: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"tokens": entries}))


@pytest.fixture(scope="module")
def orgs(owner_engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    return _create_org(owner_engine, "dev-a"), _create_org(owner_engine, "dev-b")


@pytest.fixture(scope="module")
def tokens_file(
    tmp_path_factory: pytest.TempPathFactory, orgs: tuple[uuid.UUID, uuid.UUID]
) -> Path:
    org_a, org_b = orgs
    path = tmp_path_factory.mktemp("dev") / "dev_tokens.json"
    _write_tokens(
        path,
        [
            {"label": "a-csm", "token": "dev_" + "1" * 30, "org_id": str(org_a), "role": "csm"},
            {"label": "a-ro", "token": "dev_" + "2" * 30, "org_id": str(org_a), "role": "readonly"},
            {"label": "b-csm", "token": "dev_" + "3" * 30, "org_id": str(org_b), "role": "csm"},
        ],
    )
    return path


@pytest.fixture(scope="module")
def client(test_database, pepper: str, tokens_file: Path) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    app = create_app(_settings(test_database.app_url, pepper, tokens_file))
    with TestClient(app) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_dev_token_reaches_handler(client: TestClient) -> None:
    response = client.get(ENDPOINT, headers=_auth("dev_" + "1" * 30))

    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    ["Basic abc", "Bearer", "Bearer dev_unknown", "Bearer " + "dev_" + "1" * 29, "Bearer chi_x.y"],
)
def test_invalid_dev_token_returns_401(client: TestClient, header: str) -> None:
    response = client.get(ENDPOINT, headers={"Authorization": header})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="chi"'


def test_every_v1_route_requires_a_dev_token(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    checked = 0
    for path, operations in paths.items():
        if not path.startswith("/api/v1/") or path == "/api/v1/health":
            continue
        concrete = path
        for placeholder in ("{account_id}", "{prediction_id}", "{action_id}", "{version}"):
            concrete = concrete.replace(placeholder, "x")
        for method in operations:
            assert client.request(method.upper(), concrete).status_code == 401, path
            checked += 1
    assert checked == 11


def test_readonly_token_can_read_but_not_approve(client: TestClient) -> None:
    readonly = _auth("dev_" + "2" * 30)

    assert client.get(ENDPOINT, headers=readonly).status_code == 200
    denied = client.post(APPROVE, headers=readonly)
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith(PROBLEM)


def test_csm_token_can_reach_approve(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/actions/{uuid.uuid4()}/approvals",
        json={"decision": "approved"},
        headers={**_auth("dev_" + "1" * 30), "Idempotency-Key": "auth-probe-1"},
    )

    assert response.status_code == 404


def test_client_supplied_organisation_id_does_not_change_the_outcome(
    client: TestClient, orgs: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, org_b = orgs
    token_a = _auth("dev_" + "1" * 30)

    for response in (
        client.get(ENDPOINT, headers={**token_a, "X-Org-Id": str(org_b)}),
        client.get(ENDPOINT, params={"org_id": str(org_b)}, headers=token_a),
    ):
        assert response.status_code == 200
        assert response.json()["items"] == []


def test_each_token_resolves_to_its_own_organisation(
    test_database,  # type: ignore[no-untyped-def]
    pepper: str,
    tokens_file: Path,
    orgs: tuple[uuid.UUID, uuid.UUID],
) -> None:
    from app.api import dependencies

    org_a, org_b = orgs
    app = create_app(_settings(test_database.app_url, pepper, tokens_file))
    with TestClient(app) as probe:
        principal_a = dependencies.authenticate_dev_token(app.state.dev_tokens, "dev_" + "1" * 30)
        principal_b = dependencies.authenticate_dev_token(app.state.dev_tokens, "dev_" + "3" * 30)
        assert probe.get("/api/v1/health").status_code == 200

    assert principal_a.org_id == org_a
    assert principal_b.org_id == org_b
    assert principal_a.org_id != principal_b.org_id


def test_audit_identity_is_label_derived_not_secret_derived(
    test_database,  # type: ignore[no-untyped-def]
    pepper: str,
    tokens_file: Path,
) -> None:
    from app.api import dependencies

    app = create_app(_settings(test_database.app_url, pepper, tokens_file))
    with TestClient(app):
        principal = dependencies.authenticate_dev_token(app.state.dev_tokens, "dev_" + "1" * 30)

    assert principal.actor_id == audit_actor_id("a-csm")
    assert "1" * 30 not in str(principal.actor_id)


def test_startup_warns_that_dev_tokens_are_not_production_ready(
    test_database,  # type: ignore[no-untyped-def]
    pepper: str,
    tokens_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(_settings(test_database.app_url, pepper, tokens_file))
    with TestClient(app):
        pass

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    warnings = [r for r in records if r["level"] == "WARNING"]
    assert [r["message"] for r in warnings] == [DEV_AUTH_WARNING]
    assert "NOT PRODUCTION-READY" in DEV_AUTH_WARNING
    assert warnings[0]["tokens"] == 3
    assert "1" * 30 not in json.dumps(records)


def test_startup_fails_for_an_organisation_that_does_not_exist(
    test_database,  # type: ignore[no-untyped-def]
    pepper: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "ghost.json"
    ghost = uuid.uuid4()
    _write_tokens(
        path,
        [{"label": "ghost", "token": "dev_" + "9" * 30, "org_id": str(ghost), "role": "csm"}],
    )
    app = create_app(_settings(test_database.app_url, pepper, path))

    with pytest.raises(RuntimeError, match="do not exist"), TestClient(app):
        pass


def test_startup_fails_when_the_token_file_is_missing(
    test_database,  # type: ignore[no-untyped-def]
    pepper: str,
    tmp_path: Path,
) -> None:
    app = create_app(_settings(test_database.app_url, pepper, tmp_path / "absent.json"))

    with pytest.raises(ValueError, match="not found"), TestClient(app):
        pass


def test_api_key_mode_ignores_dev_tokens(test_database, pepper: str, tokens_file: Path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(  # type: ignore[call-arg]
        database_url=test_database.app_url,
        api_key_pepper=pepper,
        auth_mode="api_keys",
        dev_tokens_file=tokens_file,
        _env_file=None,
    )
    with TestClient(create_app(settings)) as api_key_client:
        response = api_key_client.get(ENDPOINT, headers=_auth("dev_" + "1" * 30))

    assert response.status_code == 401
