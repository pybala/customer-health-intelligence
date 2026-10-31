"""Adapter contract: parse one source-native payload into a normalised, identity-candidate form."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class MalformedRecordError(ValueError):
    """The payload cannot be interpreted. The record is kept raw and logged, never dropped."""


@dataclass(frozen=True, slots=True)
class IdentityHints:
    """Everything a payload says about which account it belongs to."""

    exact_key: str | None = None  # the source's own account key
    domain: str | None = None
    name: str | None = None
    cross_ref: str | None = None  # another source's key for the account, for conflict detection
    cross_ref_source: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    source: str
    source_record_id: str
    event_type: str
    event_time: datetime  # naive only when timezone_assumed is True
    timezone_assumed: bool
    hints: IdentityHints
    attributes: dict[str, Any] = field(default_factory=dict)


def parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise MalformedRecordError(f"{field_name} is missing or not a string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MalformedRecordError(f"{field_name} is not an ISO timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        raise MalformedRecordError(f"{field_name} has no UTC offset")
    return moment.astimezone(UTC)


def require(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None or value == "":
        raise MalformedRecordError(f"missing required field {key!r}")
    return value
