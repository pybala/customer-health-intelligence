from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text

from app.api.dependencies import get_engine
from app.api.errors import ApiProblem

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    database: str


@router.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
def health(engine: Annotated[Engine, Depends(get_engine)]) -> HealthResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise ApiProblem(
            503, "database-unavailable", "Service Unavailable", "Database is unreachable."
        ) from exc
    return HealthResponse(status="ok", database="ok")
