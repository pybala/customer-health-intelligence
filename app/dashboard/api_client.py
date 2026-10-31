"""HTTP client for the dashboard. It sends only the caller's token; the API derives the org."""

import uuid
from typing import Any, Literal

import httpx

PAGE_SIZE = 200
Decision = Literal["approved", "rejected"]


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def accounts(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE}
            if cursor is not None:
                params["cursor"] = cursor
            page = self._get("/api/v1/accounts", params)
            items.extend(_rows(page, "items"))
            cursor = page["next_cursor"]
            if cursor is None:
                return items

    def account(self, account_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/accounts/{account_id}")

    def predictions(self, account_id: str) -> list[dict[str, Any]]:
        page = self._get(f"/api/v1/accounts/{account_id}/predictions", {"limit": PAGE_SIZE})
        return _rows(page, "items")

    def prediction(self, prediction_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/predictions/{prediction_id}")

    def evidence(self, prediction_id: str) -> list[dict[str, Any]]:
        return _rows(self._get(f"/api/v1/predictions/{prediction_id}/evidence"), "events")

    def actions(self, account_id: str) -> list[dict[str, Any]]:
        return _rows(self._get(f"/api/v1/accounts/{account_id}/actions"), "items")

    def outbox(self, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        if status is not None:
            params["status"] = status
        return _rows(self._get("/api/v1/outbox", params), "items")

    def data_quality(self) -> dict[str, Any]:
        return self._get("/api/v1/data-quality")

    def decide(
        self,
        action_id: str,
        decision: Decision,
        idempotency_key: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision}
        if reason:
            body["reason"] = reason
        response = self._http.post(
            f"/api/v1/actions/{action_id}/approvals",
            json=body,
            headers={"Idempotency-Key": idempotency_key},
        )
        return _decode(response)

    def new_idempotency_key(self) -> str:
        return str(uuid.uuid4())

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return _decode(self._http.get(path, params=params))


def _rows(page: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = page[key]
    return rows


def _decode(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("detail", response.reason_phrase))
        except ValueError:
            detail = response.reason_phrase
        raise ApiError(response.status_code, detail)
    body: dict[str, Any] = response.json()
    return body
