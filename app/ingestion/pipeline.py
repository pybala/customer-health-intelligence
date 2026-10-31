"""Tenant-scoped ingestion: raw landing, normalisation, identity resolution, quality issues.

Reads only `accounts.ndjson` and the per-source files. It never reads ground truth or the
defect side file; those exist for tests of this code, not as inputs to it.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from app.ingestion.adapters.base import MalformedRecordError
from app.ingestion.adapters.sources import ADAPTERS
from app.ingestion.identity import IdentityIndex, resolve
from app.ingestion.repository import IngestionRepository

SOURCES = ("salesforce", "gmail", "zendesk", "product", "stripe")
LATE_AFTER = timedelta(days=3)


@dataclass
class IngestReport:
    org_id: UUID
    accounts_inserted: int = 0
    raw_inserted: dict[str, int] = field(default_factory=dict)
    raw_received: dict[str, int] = field(default_factory=dict)
    events_inserted: dict[str, int] = field(default_factory=dict)
    issues: Counter[str] = field(default_factory=Counter)

    @property
    def total_events(self) -> int:
        return sum(self.events_inserted.values())


def payload_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dedupe_key(source_record_id: str, sha: str) -> str:
    """A revision has the same record ID but new content, so it is a separate, later event."""
    return f"{source_record_id}:{sha[:12]}"


def _read(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_accounts(repo: IngestionRepository, org_dir: Path) -> int:
    master = list(_read(org_dir / "accounts.ndjson"))
    inserted = repo.insert_accounts(
        [
            {
                "id": UUID(a["id"]),
                "canonical_name": a["canonical_name"],
                "primary_domain": a["primary_domain"],
                "segment": a["segment"],
                "plan_tier": a["plan_tier"],
                "region": a["region"],
                "timezone": a["timezone"],
                "created_at": datetime.fromisoformat(a["created_at"]),
                "renewal_date": (
                    datetime.fromisoformat(a["renewal_date"]) if a["renewal_date"] else None
                ),
            }
            for a in master
        ]
    )
    repo.insert_identities(
        [
            {
                "account_id": UUID(a["id"]),
                "source": i["source"],
                "key": i["key"],
                "method": i["method"],
                "confidence": i["confidence"],
            }
            for a in master
            for i in a["identities"]
        ]
    )
    return inserted


def _land_source(
    repo: IngestionRepository, org_dir: Path, source: str, now: datetime, report: IngestReport
) -> None:
    rows: list[dict[str, Any]] = []
    seen: Counter[tuple[str, str]] = Counter()
    batch_times: dict[str, list[datetime]] = defaultdict(list)
    for raw in _read(org_dir / f"{source}.ndjson"):
        sha = payload_sha256(raw["payload"])
        received = datetime.fromisoformat(raw["received_at"])
        rows.append({**raw, "payload_sha256": sha, "received_at": received})
        seen[(raw["source_record_id"], sha)] += 1
        batch_times[raw["batch_id"]].append(received)

    report.raw_received[source] = len(rows)
    # A re-sent record was first seen at its earliest delivery. Landing keeps the first row it
    # inserts, so collapse copies to the earliest received_at here; otherwise ingested_at, and
    # with it feature visibility, would depend on the order the file happens to list them in.
    earliest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["source_record_id"], row["payload_sha256"])
        if key not in earliest or row["received_at"] < earliest[key]["received_at"]:
            earliest[key] = row
    unique = sorted(
        earliest.values(),
        key=lambda r: (r["received_at"], r["source_record_id"], r["payload_sha256"]),
    )
    report.raw_inserted[source] = repo.land_raw(source, unique)

    issues: list[dict[str, Any]] = [
        {
            "kind": "duplicate",
            "source": source,
            "ref": dedupe_key(rid, sha),
            "detected_at": now,
            "detail": {"copies": count},
        }
        for (rid, sha), count in seen.items()
        if count > 1
    ]
    issues += [
        {
            "kind": "out_of_order",
            "source": source,
            "ref": batch,
            "detected_at": now,
            "detail": {"rows": len(times)},
        }
        for batch, times in batch_times.items()
        if times != sorted(times)
    ]
    report.issues.update(i["kind"] for i in issues)
    repo.insert_issues(issues)


def _build_index(repo: IngestionRepository) -> tuple[IdentityIndex, dict[UUID, str]]:
    index = IdentityIndex()
    zones: dict[UUID, str] = {}
    for row in repo.load_identity_rows():
        account_id = row["account_id"]
        zones[account_id] = row["timezone"]
        index.add_name(row["canonical_name"], account_id)
        if row["source"] is not None:
            index.add(row["source"], row["source_account_key"], account_id, row["match_method"])
    return index, zones


def _process_source(
    repo: IngestionRepository,
    source: str,
    index: IdentityIndex,
    zones: dict[UUID, str],
    now: datetime,
    report: IngestReport,
) -> None:
    parse = ADAPTERS[source]
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for raw in repo.unprocessed_raw(source):
        ref = dedupe_key(raw["source_record_id"], raw["payload_sha256"])
        try:
            parsed = parse(raw["payload"])
        except MalformedRecordError as exc:
            issues.append(
                {
                    "kind": "malformed_record",
                    "source": source,
                    "ref": ref,
                    "detected_at": now,
                    "detail": {"error": str(exc)},
                }
            )
            continue

        resolution = resolve(index, source, parsed.hints)
        event_time = parsed.event_time
        if parsed.timezone_assumed:
            zone = zones.get(resolution.account_id) if resolution.account_id else None
            event_time = event_time.replace(tzinfo=ZoneInfo(zone or "UTC")).astimezone(UTC)
            issues.append(
                {
                    "kind": "timezone_assumed",
                    "source": source,
                    "ref": ref,
                    "detected_at": now,
                    "detail": {"assumed_zone": zone or "UTC"},
                }
            )
        if resolution.status != "matched":
            kind = (
                "ambiguous_identity" if resolution.status == "ambiguous" else "unmatched_identity"
            )
            issues.append(
                {
                    "kind": kind,
                    "source": source,
                    "ref": ref,
                    "detected_at": now,
                    "detail": {"reason": resolution.reason},
                }
            )
        if raw["received_at"] - event_time > LATE_AFTER:
            issues.append(
                {
                    "kind": "late_arrival",
                    "source": source,
                    "ref": ref,
                    "detected_at": now,
                    "detail": {"lag_days": (raw["received_at"] - event_time).days},
                }
            )
        events.append(
            {
                "account_id": resolution.account_id,
                "source": source,
                "event_type": parsed.event_type,
                "rid": raw["source_record_id"],
                "dedupe_key": ref,
                "event_time": event_time,
                "ingested_at": raw["received_at"],
                "identity_status": resolution.status,
                "attributes": {
                    **parsed.attributes,
                    "match_method": resolution.method,
                    "match_confidence": resolution.confidence,
                },
                "raw_record_id": raw["id"],
            }
        )

    report.events_inserted[source] = repo.insert_events(events)
    report.issues.update(i["kind"] for i in issues)
    repo.insert_issues(issues)
    repo.record_revisions(source, now)


def ingest_org(engine: Engine, org_id: UUID, org_dir: Path, now: datetime) -> IngestReport:
    """Idempotent. Re-running with the same files inserts nothing new."""
    repo = IngestionRepository(engine, org_id)
    report = IngestReport(org_id)
    report.accounts_inserted = load_accounts(repo, org_dir)
    for source in SOURCES:
        _land_source(repo, org_dir, source, now, report)
    index, zones = _build_index(repo)
    for source in SOURCES:
        _process_source(repo, source, index, zones, now, report)
    return report
