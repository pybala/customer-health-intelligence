"""The five source adapters. Each parses its native shape and nothing else."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.ingestion.adapters.base import (
    IdentityHints,
    MalformedRecordError,
    ParsedRecord,
    parse_utc,
    require,
)

PRODUCT_EVENTS = {"login", "feature_used", "seat_added", "api_call_batch", "export"}
GMAIL_EVENTS = {"thread_sent", "thread_received", "meeting_invite"}
ZENDESK_EVENTS = {
    "ticket.created": "ticket_opened",
    "ticket.solved": "ticket_solved",
    "satisfaction.rated": "csat_response",
    "ticket.escalated": "escalation",
}
SALESFORCE_EVENTS = {
    "opportunity_stage_change",
    "activity_logged",
    "contact_role_change",
    "renewal_forecast_update",
}
STRIPE_EVENTS = {
    "invoice.paid": "invoice_paid",
    "invoice.payment_failed": "invoice_failed",
    "customer.subscription.updated": "subscription_updated",
    "payment_method.expired": "payment_method_expired",
}


def _checked(value: str, allowed: set[str], what: str) -> str:
    if value not in allowed:
        raise MalformedRecordError(f"unknown {what}: {value!r}")
    return value


def parse_product(payload: dict[str, Any]) -> ParsedRecord:
    raw_ts = str(require(payload, "ts"))
    try:
        moment = datetime.fromisoformat(raw_ts)
    except ValueError as exc:
        raise MalformedRecordError(f"ts is not an ISO timestamp: {raw_ts!r}") from exc
    assumed = moment.tzinfo is None
    properties = payload.get("properties") or {}
    attributes: dict[str, Any] = {"user": payload.get("user_id"), **properties}
    return ParsedRecord(
        source="product",
        source_record_id=str(require(payload, "event_id")),
        event_type=_checked(str(require(payload, "event")), PRODUCT_EVENTS, "product event"),
        event_time=moment if assumed else moment.astimezone(UTC),
        timezone_assumed=assumed,
        hints=IdentityHints(exact_key=str(require(payload, "account_external_id"))),
        attributes=attributes,
    )


def parse_gmail(payload: dict[str, Any]) -> ParsedRecord:
    return ParsedRecord(
        source="gmail",
        source_record_id=str(require(payload, "id")),
        event_type=_checked(str(require(payload, "kind")), GMAIL_EVENTS, "gmail kind"),
        event_time=parse_utc(payload.get("sent_at"), "sent_at"),
        timezone_assumed=False,
        hints=IdentityHints(domain=str(require(payload, "external_domain")).lower()),
        attributes={
            "thread": payload.get("thread_id"),
            "participants": payload.get("participant_count"),
            "exec_contact": bool(payload.get("exec_contact")),
        },
    )


def parse_zendesk(payload: dict[str, Any]) -> ParsedRecord:
    attributes: dict[str, Any] = {"ticket": payload.get("ticket_id")}
    for key, target in (("csat_score", "csat"), ("resolution_hours", "resolution_hours")):
        if payload.get(key) is not None:
            attributes[target] = payload[key]
    kind = str(require(payload, "type"))
    if kind not in ZENDESK_EVENTS:
        raise MalformedRecordError(f"unknown zendesk type: {kind!r}")
    return ParsedRecord(
        source="zendesk",
        source_record_id=str(require(payload, "id")),
        event_type=ZENDESK_EVENTS[kind],
        event_time=parse_utc(payload.get("created_at"), "created_at"),
        timezone_assumed=False,
        hints=IdentityHints(
            exact_key=str(require(payload, "organization_id")),
            name=payload.get("organization_name"),
        ),
        attributes=attributes,
    )


def parse_salesforce(payload: dict[str, Any]) -> ParsedRecord:
    attributes: dict[str, Any] = {}
    stage = payload.get("StageName")
    if isinstance(stage, str):
        attributes["stage"] = stage.replace("-", " ")
    if payload.get("ForecastPct") is not None:
        attributes["forecast_pct"] = payload["ForecastPct"]
    account_id = payload.get("AccountId")
    domain = payload.get("ContactDomain")
    if not account_id and not domain:
        raise MalformedRecordError("salesforce record has neither AccountId nor ContactDomain")
    return ParsedRecord(
        source="salesforce",
        source_record_id=str(require(payload, "Id")),
        event_type=_checked(str(require(payload, "Type")), SALESFORCE_EVENTS, "salesforce type"),
        event_time=parse_utc(payload.get("OccurredAt"), "OccurredAt"),
        timezone_assumed=False,
        hints=IdentityHints(
            exact_key=str(account_id) if account_id else None,
            domain=str(domain).lower() if domain else None,
        ),
        attributes=attributes,
    )


def parse_stripe(payload: dict[str, Any]) -> ParsedRecord:
    created = require(payload, "created")
    if not isinstance(created, int):
        raise MalformedRecordError("created must be epoch seconds")
    data = payload.get("data") or {}
    attributes: dict[str, Any] = {}
    if "amount" in data:
        attributes["amount_minor"] = data["amount"]
    if "quantity" in data:
        attributes["seats"] = data["quantity"]
    kind = str(require(payload, "type"))
    if kind not in STRIPE_EVENTS:
        raise MalformedRecordError(f"unknown stripe type: {kind!r}")
    metadata = payload.get("metadata") or {}
    return ParsedRecord(
        source="stripe",
        source_record_id=str(require(payload, "id")),
        event_type=STRIPE_EVENTS[kind],
        event_time=datetime.fromtimestamp(created, UTC),
        timezone_assumed=False,
        hints=IdentityHints(
            exact_key=str(require(payload, "customer")),
            domain=str(payload["billing_domain"]).lower()
            if payload.get("billing_domain")
            else None,
            cross_ref=metadata.get("crm_id"),
            cross_ref_source="salesforce",
        ),
        attributes=attributes,
    )


ADAPTERS: dict[str, Callable[[dict[str, Any]], ParsedRecord]] = {
    "product": parse_product,
    "gmail": parse_gmail,
    "zendesk": parse_zendesk,
    "salesforce": parse_salesforce,
    "stripe": parse_stripe,
}
