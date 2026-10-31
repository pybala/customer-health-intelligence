"""Source-native raw payload shapes. Adapters must parse these, not a pre-cleaned format."""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.ingestion.generators.events import CleanEvent
from app.ingestion.generators.world import AccountWorld

FREEMAIL_DOMAIN = "freemail.example.test"
SF_HYPHEN_FROM = datetime(2025, 4, 1, tzinfo=UTC)
ZENDESK_TYPES = {
    "ticket_opened": "ticket.created",
    "ticket_solved": "ticket.solved",
    "csat_response": "satisfaction.rated",
    "escalation": "ticket.escalated",
}
STRIPE_TYPES = {
    "invoice_paid": "invoice.paid",
    "invoice_failed": "invoice.payment_failed",
    "subscription_updated": "customer.subscription.updated",
    "payment_method_expired": "payment_method.expired",
}
ID_PREFIX = {"product": "pe", "gmail": "gm", "zendesk": "zd", "salesforce": "00T", "stripe": "evt"}


@dataclass(frozen=True, slots=True)
class Flags:
    """Defect decisions for one record, made by the injector."""

    zendesk_name: str | None = None
    gmail_domain: str | None = None
    product_tz_naive: bool = False
    sf_account_id_missing: bool = False
    stripe_crm_ref: str | None = None


def canonical_name(account: AccountWorld) -> str:
    return f"{account.name} {account.suffix}"


def record_id(event: CleanEvent) -> str:
    return f"{ID_PREFIX[event.source]}_{event.org_slug[:3]}_{event.seq:07d}"


def _utc_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    builder = _BUILDERS[event.source]
    return builder(event, account, flags)


def _product(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    local = event.event_time.astimezone(ZoneInfo(account.timezone))
    stamp = local.strftime("%Y-%m-%dT%H:%M:%S")
    if not flags.product_tz_naive:
        offset = local.strftime("%z")
        stamp += f"{offset[:3]}:{offset[3:]}"
    return {
        "event_id": record_id(event),
        "account_external_id": account.product_key,
        "user_id": event.attributes.get("user"),
        "event": event.event_type,
        "ts": stamp,
        "properties": {k: v for k, v in event.attributes.items() if k in {"calls", "seats_delta"}},
    }


def _gmail(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    return {
        "id": record_id(event),
        "thread_id": event.attributes["thread"],
        "kind": event.event_type,
        "sent_at": _utc_z(event.event_time),
        "external_domain": flags.gmail_domain or account.domain,
        "participant_count": event.attributes["participants"],
        "exec_contact": event.attributes["exec_contact"],
    }


def _zendesk(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": record_id(event),
        "organization_id": account.zendesk_org_id,
        "organization_name": flags.zendesk_name or canonical_name(account),
        "type": ZENDESK_TYPES[event.event_type],
        "created_at": _utc_z(event.event_time),
        "ticket_id": event.attributes["ticket"],
    }
    if "csat" in event.attributes:
        payload["csat_score"] = event.attributes["csat"]
    if "resolution_hours" in event.attributes:
        payload["resolution_hours"] = event.attributes["resolution_hours"]
    return payload


def _salesforce(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    payload: dict[str, object] = {
        "Id": record_id(event),
        "AccountId": None if flags.sf_account_id_missing else account.salesforce_id,
        "Type": event.event_type,
        "OccurredAt": _utc_z(event.event_time),
    }
    if flags.sf_account_id_missing:
        payload["ContactDomain"] = account.domain
    stage = event.attributes.get("stage")
    if isinstance(stage, str):
        hyphen = event.event_time >= SF_HYPHEN_FROM
        payload["StageName"] = stage.replace(" ", "-") if hyphen else stage
    if "forecast_pct" in event.attributes:
        payload["ForecastPct"] = event.attributes["forecast_pct"]
    return payload


def _stripe(event: CleanEvent, account: AccountWorld, flags: Flags) -> dict[str, object]:
    data: dict[str, object] = {}
    if "amount_minor" in event.attributes:
        data["amount"] = event.attributes["amount_minor"]
        data["currency"] = "usd"
    if "seats" in event.attributes:
        data["quantity"] = event.attributes["seats"]
    return {
        "id": record_id(event),
        "customer": account.stripe_customer,
        "type": STRIPE_TYPES[event.event_type],
        "created": int(event.event_time.timestamp()),
        "data": data,
        "billing_domain": account.domain,
        "metadata": {"crm_id": flags.stripe_crm_ref or account.salesforce_id},
    }


_BUILDERS = {
    "product": _product,
    "gmail": _gmail,
    "zendesk": _zendesk,
    "salesforce": _salesforce,
    "stripe": _stripe,
}


def revise(source: str, payload: dict[str, object]) -> dict[str, object]:
    """Same source record ID with changed content, as a source correcting itself."""
    changed = dict(payload)
    changed["revision"] = 1
    if source == "salesforce":
        changed["StageName"] = (
            "Negotiation" if payload.get("StageName") != "Negotiation" else "Proposal"
        )
    elif source == "zendesk":
        changed["csat_score"] = max(1, int(payload.get("csat_score", 3)) - 1)  # type: ignore[call-overload]
    elif source == "stripe":
        data = dict(payload["data"])  # type: ignore[call-overload]
        data["amount"] = int(data.get("amount", 10000) * 0.95)
        changed["data"] = data
    elif source == "gmail":
        changed["participant_count"] = int(payload["participant_count"]) + 1  # type: ignore[call-overload]
    else:
        changed["user_id"] = f"{payload['user_id']}x"
    return changed
