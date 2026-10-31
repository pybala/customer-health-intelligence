from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response
from sqlalchemy import Engine

from app.api import approvals, read_service
from app.api.dependencies import CurrentPrincipal, get_engine
from app.api.errors import ApiProblem
from app.api.schemas import (
    AccountDetail,
    AccountList,
    ActionList,
    ApprovalRequest,
    ApprovalResponse,
    DataQuality,
    ModelEvaluation,
    ModelList,
    OutboxList,
    PredictionDetail,
    PredictionEvidence,
    PredictionList,
)

router = APIRouter(prefix="/api/v1", tags=["v1"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

Engines = Annotated[Engine, Depends(get_engine)]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query()]
OutboxStatus = Literal[
    "pending", "sending", "sent", "dry_run_recorded", "suppressed", "dead_letter"
]
AccountPath = Annotated[UUID, Path()]
PredictionPath = Annotated[UUID, Path()]


def _not_found(resource: str) -> ApiProblem:
    return ApiProblem(404, "not-found", "Not Found", f"{resource} was not found.")


AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"description": "Missing or invalid API key"},
}
PAGED_RESPONSES: dict[int | str, dict[str, object]] = {
    **AUTH_RESPONSES,
    400: {"description": "Malformed cursor, or unsupported sort"},
}
OUTBOX_RESPONSES: dict[int | str, dict[str, object]] = {
    **PAGED_RESPONSES,
    400: {"description": "Malformed cursor"},
    422: {"description": "Unknown outbox status"},
}
SortParam = Annotated[
    str,
    Query(
        json_schema_extra={"enum": ["risk_desc"]},
        description="Ranking order. risk_desc is the only supported value.",
    ),
]
NOT_FOUND_RESPONSES: dict[int | str, dict[str, object]] = {
    **AUTH_RESPONSES,
    404: {"description": "Not found in this organisation"},
}
APPROVAL_RESPONSES: dict[int | str, dict[str, object]] = {
    **AUTH_RESPONSES,
    403: {"description": "Role cannot decide actions, or readonly token"},
    404: {"description": "Action not found in this organisation"},
    409: {"description": "Action already has a different decision"},
    422: {"description": "Missing Idempotency-Key, invalid body, or key reused for another body"},
}


@router.get(
    "/accounts",
    response_model=AccountList,
    responses=PAGED_RESPONSES,
    summary="Ranked accounts by latest risk",
)
def list_accounts(
    principal: CurrentPrincipal,
    engine: Engines,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
    sort: SortParam = "risk_desc",
) -> dict[str, Any]:
    if sort != "risk_desc":
        raise ApiProblem(
            400,
            "unsupported-sort",
            "Unsupported sort",
            "The only supported sort is risk_desc.",
        )
    items, next_cursor = read_service.list_accounts(
        engine, principal.org_id, limit, read_service.decode_cursor(cursor)
    )
    return {"items": items, "next_cursor": next_cursor}


@router.get(
    "/accounts/{account_id}",
    response_model=AccountDetail,
    responses=NOT_FOUND_RESPONSES,
    summary="Account profile",
)
def get_account(
    principal: CurrentPrincipal, engine: Engines, account_id: AccountPath
) -> dict[str, Any]:
    account = read_service.get_account(engine, principal.org_id, account_id)
    if account is None:
        raise _not_found("Account")
    return account


@router.get(
    "/accounts/{account_id}/predictions",
    response_model=PredictionList,
    responses={**PAGED_RESPONSES, 404: {"description": "Not found in this organisation"}},
    summary="Prediction history for an account",
)
def list_account_predictions(
    principal: CurrentPrincipal,
    engine: Engines,
    account_id: AccountPath,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> dict[str, Any]:
    result = read_service.list_predictions(
        engine, principal.org_id, account_id, limit, read_service.decode_cursor(cursor)
    )
    if result is None:
        raise _not_found("Account")
    items, next_cursor = result
    return {"items": items, "next_cursor": next_cursor}


@router.get(
    "/predictions/{prediction_id}",
    response_model=PredictionDetail,
    responses=NOT_FOUND_RESPONSES,
    summary="One prediction snapshot with contributions",
)
def get_prediction(
    principal: CurrentPrincipal, engine: Engines, prediction_id: PredictionPath
) -> dict[str, Any]:
    prediction = read_service.get_prediction(engine, principal.org_id, prediction_id)
    if prediction is None:
        raise _not_found("Prediction")
    return prediction


@router.get(
    "/predictions/{prediction_id}/evidence",
    response_model=PredictionEvidence,
    responses=NOT_FOUND_RESPONSES,
    summary="Source events cited by a prediction",
)
def get_prediction_evidence(
    principal: CurrentPrincipal, engine: Engines, prediction_id: PredictionPath
) -> dict[str, Any]:
    evidence = read_service.prediction_evidence(engine, principal.org_id, prediction_id)
    if evidence is None:
        raise _not_found("Prediction")
    return evidence


@router.get(
    "/accounts/{account_id}/actions",
    response_model=ActionList,
    responses=NOT_FOUND_RESPONSES,
    summary="Recommended actions for an account",
)
def list_account_actions(
    principal: CurrentPrincipal, engine: Engines, account_id: AccountPath
) -> dict[str, Any]:
    actions = read_service.list_actions(engine, principal.org_id, account_id)
    if actions is None:
        raise _not_found("Account")
    return {"items": actions}


@router.post(
    "/actions/{action_id}/approvals",
    status_code=201,
    response_model=ApprovalResponse,
    responses=APPROVAL_RESPONSES,
    summary="Approve or reject one proposed action (idempotent)",
)
def approve_action(
    principal: CurrentPrincipal,
    engine: Engines,
    body: ApprovalRequest,
    action_id: AccountPath,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    response: Response,
) -> dict[str, Any]:
    result = approvals.decide_action(
        engine,
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        role=principal.role,
        action_id=action_id,
        key=idempotency_key,
        decision=body.decision,
        reason=body.reason,
        now=datetime.now(UTC),
    )
    response.status_code = result.status_code
    return result.body


@router.get(
    "/outbox",
    response_model=OutboxList,
    responses=OUTBOX_RESPONSES,
    summary="Outbox status",
)
def list_outbox(
    principal: CurrentPrincipal,
    engine: Engines,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
    status: OutboxStatus | None = None,
) -> dict[str, Any]:
    items, next_cursor = read_service.list_outbox(
        engine, principal.org_id, limit, read_service.decode_cursor(cursor), status
    )
    return {"items": items, "next_cursor": next_cursor}


@router.get(
    "/data-quality",
    response_model=DataQuality,
    responses=AUTH_RESPONSES,
    summary="Ingestion data-quality counts",
)
def get_data_quality(principal: CurrentPrincipal, engine: Engines) -> dict[str, Any]:
    return read_service.data_quality(engine, principal.org_id)


@router.get(
    "/models",
    response_model=ModelList,
    responses=AUTH_RESPONSES,
    summary="Registered model versions",
)
def list_models(principal: CurrentPrincipal, engine: Engines) -> dict[str, Any]:
    return {"items": read_service.list_models(engine, principal.org_id)}


@router.get(
    "/models/{version}/evaluation",
    response_model=ModelEvaluation,
    responses=NOT_FOUND_RESPONSES,
    summary="Persisted evaluation report for one model version",
)
def get_model_evaluation(
    principal: CurrentPrincipal, engine: Engines, version: str = Path()
) -> dict[str, Any]:
    evaluation = read_service.model_evaluation(engine, principal.org_id, version)
    if evaluation is None:
        raise _not_found("Model version")
    return evaluation
