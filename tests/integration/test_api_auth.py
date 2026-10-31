import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.config import Settings
from app.db.session import tenant_session
from app.security.api_keys import issue_key

pytestmark = pytest.mark.integration

PROBLEM = "application/problem+json"
ENDPOINT = "/api/v1/accounts"


def _settings(database_url: str, pepper: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url=database_url,
        api_key_pepper=pepper,
        _env_file=None,
    )


@pytest.fixture(scope="module")
def client(test_database, pepper: str) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    from app.api.main import create_app

    app = create_app(_settings(test_database.app_url, pepper))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def csm_token(owner_engine: Engine, pepper: str) -> str:
    org_id = uuid.uuid4()
    issued = issue_key(pepper)
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'API Org')"),
            {"i": org_id, "s": f"api-{org_id.hex[:8]}"},
        )
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) "
                "VALUES (:i, :o, :p, :h, 'csm')"
            ),
            {"i": uuid.uuid4(), "o": org_id, "p": issued.prefix, "h": issued.key_hash},
        )
    return issued.plaintext


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_missing_key_returns_problem_401(client: TestClient) -> None:
    response = client.get(ENDPOINT)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM)
    assert response.headers["www-authenticate"] == 'Bearer realm="chi"'


@pytest.mark.parametrize(
    "header",
    ["Basic abc", "Bearer", "Bearer not-a-chi-token", "Bearer chi_unknown.secretvalue"],
)
def test_malformed_or_unknown_key_returns_401(client: TestClient, header: str) -> None:
    response = client.get(ENDPOINT, headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json()["status"] == 401


def test_valid_key_reaches_handler(client: TestClient, csm_token: str) -> None:
    response = client.get(ENDPOINT, headers={"Authorization": f"Bearer {csm_token}"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_key_with_wrong_pepper_is_rejected(test_database, csm_token: str, pepper: str) -> None:  # type: ignore[no-untyped-def]
    from app.api.main import create_app

    other = create_app(_settings(test_database.app_url, "a-different-pepper-" + "w" * 32))
    with TestClient(other) as wrong:
        response = wrong.get(ENDPOINT, headers={"Authorization": f"Bearer {csm_token}"})

    assert response.status_code == 401


def test_revoked_key_is_rejected(
    client: TestClient, owner_engine: Engine, test_database, pepper: str
) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid.uuid4()
    issued = issue_key(pepper)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, 'Revoked Org')"
            ),
            {"i": org_id, "s": f"rev-{org_id.hex[:8]}"},
        )
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) "
                "VALUES (:i, :o, :p, :h, 'csm')"
            ),
            {"i": uuid.uuid4(), "o": org_id, "p": issued.prefix, "h": issued.key_hash},
        )
        session.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE key_prefix = :p"),
            {"p": issued.prefix},
        )

    response = client.get(ENDPOINT, headers={"Authorization": f"Bearer {issued.plaintext}"})

    assert response.status_code == 401


def _authenticated_v1_routes(client: TestClient) -> list[tuple[str, str, str]]:
    openapi = client.get("/openapi.json").json()
    routes = []
    for path, operations in openapi["paths"].items():
        if not path.startswith("/api/v1/") or path == "/api/v1/health":
            continue
        concrete = path
        for placeholder in ("{account_id}", "{prediction_id}", "{action_id}", "{version}"):
            concrete = concrete.replace(placeholder, "x")
        for method in operations:
            routes.append((method.upper(), path, concrete))
    assert routes
    return routes


def test_every_v1_route_requires_a_key(client: TestClient) -> None:
    for method, path, concrete in _authenticated_v1_routes(client):
        response = client.request(method, concrete)
        assert response.status_code == 401, f"{method} {path}"


def test_approval_route_requires_idempotency_key_with_a_valid_key(
    client: TestClient, csm_token: str
) -> None:
    response = client.post(
        f"/api/v1/actions/{uuid.uuid4()}/approvals",
        json={"decision": "approved"},
        headers={"Authorization": f"Bearer {csm_token}"},
    )

    assert response.status_code == 422
