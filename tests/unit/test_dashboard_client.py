import json
from typing import Any

import httpx
import pytest

from app.dashboard.api_client import ApiClient, ApiError

TOKEN = "dev_" + "x" * 30


def _client(handler: Any) -> ApiClient:
    return ApiClient("http://api.test", TOKEN, transport=httpx.MockTransport(handler))


def test_every_request_carries_only_the_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    _client(handler).accounts()

    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert "org_id" not in str(seen[0].url)


def test_accounts_follow_the_cursor_until_the_last_page() -> None:
    pages = [
        {"items": [{"account_id": "1"}], "next_cursor": "abc"},
        {"items": [{"account_id": "2"}], "next_cursor": None},
    ]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=pages[len(requested) - 1])

    items = _client(handler).accounts()

    assert [item["account_id"] for item in items] == ["1", "2"]
    assert "cursor=abc" in requested[1]


def test_decide_sends_the_idempotency_key_and_decision() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers["Idempotency-Key"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"decision": "approved"})

    result = _client(handler).decide("a1", "approved", "key-1")

    assert captured == {
        "path": "/api/v1/actions/a1/approvals",
        "key": "key-1",
        "body": {"decision": "approved"},
    }
    assert result == {"decision": "approved"}


def test_decide_omits_empty_reason() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={})

    _client(handler).decide("a1", "rejected", "k", reason="")

    assert bodies == [{"decision": "rejected"}]


def test_api_errors_surface_status_and_problem_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"type": "x", "title": "t", "status": 409, "detail": "already decided"},
        )

    with pytest.raises(ApiError) as caught:
        _client(handler).decide("a1", "approved", "k")

    assert caught.value.status == 409
    assert caught.value.detail == "already decided"


def test_non_json_error_body_still_raises_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(ApiError) as caught:
        _client(handler).outbox()

    assert caught.value.status == 502


def test_outbox_status_filter_is_passed_through() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    _client(handler).outbox("sent")

    assert "status=sent" in urls[0]
