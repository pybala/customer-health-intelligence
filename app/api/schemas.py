"""Response models for the read endpoints. Each field is a documented contract."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RygStatus = Literal["red", "yellow", "green", "insufficient_data"]
RygState = Literal["evaluated", "not_eligible", "no_snapshot"]


class RygReason(BaseModel):
    code: str
    feature: str
    observed: float | int | None
    threshold: float
    severity: RygStatus


class Ryg(BaseModel):
    status: RygStatus = Field(
        description="insufficient_data is a RYG status with reasons; it is not green."
    )
    policy_version: str
    feature_cutoff_at: datetime
    reasons: list[RygReason]


class PredictionSummary(BaseModel):
    prediction_id: str
    model_version_id: str
    feature_cutoff_at: datetime
    scored_at: datetime | None
    eligible: bool
    ineligible_reason: str | None
    risk_score: float | None = Field(description="ML risk score. Not the RYG status.")
    ryg_state: RygState = Field(
        description=(
            "evaluated: ryg is present. not_eligible: the account was outside the scoring "
            "window, so there is no RYG. no_snapshot: eligible but no stored snapshot to "
            "evaluate. None of these is green."
        )
    )
    ryg: Ryg | None = Field(description="Read-time RYG. Null unless ryg_state is evaluated.")


class PredictionListItem(PredictionSummary):
    scored_at: datetime


class AccountSummary(BaseModel):
    account_id: str
    canonical_name: str
    segment: str
    renewal_date: datetime | None
    latest_prediction: PredictionSummary | None


class AccountList(BaseModel):
    items: list[AccountSummary]
    next_cursor: str | None


class AccountDetail(BaseModel):
    account_id: str
    canonical_name: str
    primary_domain: str
    segment: str
    plan_tier: str
    region: str
    timezone: str
    renewal_date: datetime | None
    latest_prediction: PredictionSummary | None


class PredictionList(BaseModel):
    items: list[PredictionListItem]
    next_cursor: str | None


class PredictionDetail(PredictionListItem):
    account_id: str
    model_version: str
    feature_set_version: str
    renewal_date: datetime | None
    feature_values: dict[str, Any] | None
    contributions: dict[str, Any] = Field(
        description="Per-feature provenance: method, window, source, and cited event ids."
    )


class EvidenceEvent(BaseModel):
    event_id: str
    source: str
    event_type: str
    event_time: datetime
    ingested_at: datetime
    source_record_id: str


class PredictionEvidence(BaseModel):
    prediction_id: str
    events: list[EvidenceEvent]


class ActionItem(BaseModel):
    action_id: str
    prediction_id: str
    action_type: str
    status: str
    reason_code: str
    evidence: dict[str, Any]
    proposed_at: datetime


class ActionList(BaseModel):
    items: list[ActionItem]


class OutboxItem(BaseModel):
    outbox_id: str
    action_id: str
    channel: str
    delivery_mode: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None


class OutboxList(BaseModel):
    items: list[OutboxItem]
    next_cursor: str | None


class ModelItem(BaseModel):
    model_version_id: str
    model_version: str
    model_kind: str
    feature_set_version: str
    artifact_sha256: str
    status: str
    created_at: datetime


class ModelList(BaseModel):
    items: list[ModelItem]


class ModelEvaluationItem(BaseModel):
    split: str
    scope: str
    metrics: dict[str, Any]
    selection_hash: str
    created_at: datetime


class ModelEvaluation(BaseModel):
    model_version: str
    model_kind: str
    metadata: dict[str, Any]
    evaluations: list[ModelEvaluationItem]


class DataQualityCount(BaseModel):
    kind: str
    source: str
    count: int


class DataQuality(BaseModel):
    issues: list[DataQualityCount]
    total: int


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


class ApprovalResponse(BaseModel):
    approval_id: str
    action_id: str
    decision: Literal["approved", "rejected"]
    decided_at: datetime
