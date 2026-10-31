from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_BASE = "https://chi.example.test/problems/"


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.headers = headers or {}


def problem_body(problem: ApiProblem) -> dict[str, Any]:
    return {
        "type": PROBLEM_BASE + problem.slug,
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
    }


async def handle_problem(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblem)
    return JSONResponse(
        status_code=exc.status,
        content=problem_body(exc),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=exc.headers,
    )
