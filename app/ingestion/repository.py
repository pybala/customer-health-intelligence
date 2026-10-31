"""Database access for ingestion. All methods are scoped to the repository's organisation."""

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, text

from app.db.repository import TenantRepository

CHUNK = 5000


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterable[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), CHUNK):
        yield rows[start : start + CHUNK]


class IngestionRepository(TenantRepository):
    def insert_accounts(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert accounts, returning the number added. Raises if an ID belongs to another tenant.

        The primary key is global, so ON CONFLICT would silently skip an ID already used by
        another organisation. Checking that every ID is visible to this org makes that an error.
        """
        sql = text(
            """
            INSERT INTO accounts (id, org_id, canonical_name, primary_domain, segment,
                                  plan_tier, region, timezone, created_at, renewal_date)
            VALUES (:id, :org_id, :canonical_name, :primary_domain, :segment,
                    :plan_tier, :region, :timezone, :created_at, :renewal_date)
            ON CONFLICT (id) DO NOTHING
            """
        )
        with self.session() as session:
            before = self.count_accounts_in(session)
            for chunk in _chunks([{**r, "org_id": self.org_id} for r in rows]):
                session.execute(sql, list(chunk))
            visible = {
                row[0]
                for row in session.execute(
                    text("SELECT id FROM accounts WHERE org_id = :o"), {"o": self.org_id}
                )
            }
            missing = [r["id"] for r in rows if r["id"] not in visible]
            if missing:
                raise ValueError(
                    f"{len(missing)} account IDs already exist outside this organisation"
                )
            return self.count_accounts_in(session) - before

    def count_accounts_in(self, session: Any) -> int:
        return int(
            session.execute(
                text("SELECT count(*) FROM accounts WHERE org_id = :o"), {"o": self.org_id}
            ).scalar_one()
        )

    def insert_identities(self, rows: Sequence[dict[str, Any]]) -> None:
        sql = text(
            """
            INSERT INTO account_identities (org_id, account_id, source, source_account_key,
                                            match_method, match_confidence)
            VALUES (:org_id, :account_id, :source, :key, :method, :confidence)
            ON CONFLICT (org_id, source, source_account_key) DO NOTHING
            """
        )
        with self.session() as session:
            for chunk in _chunks([{**r, "org_id": self.org_id} for r in rows]):
                session.execute(sql, list(chunk))

    def load_identity_rows(self) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.execute(
                text(
                    "SELECT a.id AS account_id, a.canonical_name, a.timezone, "
                    "i.source, i.source_account_key, i.match_method "
                    "FROM accounts a LEFT JOIN account_identities i "
                    "ON i.org_id = a.org_id AND i.account_id = a.id "
                    "WHERE a.org_id = :o"
                ),
                {"o": self.org_id},
            ).mappings()
            return [dict(r) for r in rows]

    def land_raw(self, source: str, rows: Sequence[dict[str, Any]]) -> int:
        """Insert raw rows. Identical (record id, content) is a no-op. Returns rows inserted."""
        sql = text(
            """
            INSERT INTO raw_source_records (org_id, source, source_record_id, payload,
                                            payload_sha256, received_at, ingest_batch_id)
            VALUES (:org_id, :source, :rid, CAST(:payload AS jsonb), :sha, :received_at, :batch)
            ON CONFLICT (org_id, source, source_record_id, payload_sha256) DO NOTHING
            """
        )
        prepared = [
            {
                "org_id": self.org_id,
                "source": source,
                "rid": r["source_record_id"],
                "payload": json.dumps(r["payload"], sort_keys=True, separators=(",", ":")),
                "sha": r["payload_sha256"],
                "received_at": r["received_at"],
                "batch": r["batch_id"],
            }
            for r in rows
        ]
        inserted = 0
        with self.session() as session:
            for chunk in _chunks(prepared):
                result = session.execute(sql, list(chunk))
                inserted += cast(CursorResult[Any], result).rowcount
        return inserted

    def unprocessed_raw(self, source: str) -> list[dict[str, Any]]:
        with self.session() as session:
            rows = session.execute(
                text(
                    "SELECT r.id, r.source_record_id, r.payload, r.payload_sha256, r.received_at "
                    "FROM raw_source_records r WHERE r.org_id = :o AND r.source = :s "
                    "AND NOT EXISTS (SELECT 1 FROM source_events e "
                    "WHERE e.org_id = r.org_id AND e.raw_record_id = r.id) "
                    "ORDER BY r.received_at, r.source_record_id, r.payload_sha256"
                ),
                {"o": self.org_id, "s": source},
            ).mappings()
            return [dict(r) for r in rows]

    def insert_events(self, rows: Sequence[dict[str, Any]]) -> int:
        sql = text(
            """
            INSERT INTO source_events (org_id, account_id, source, event_type, source_record_id,
                                       dedupe_key, event_time, ingested_at, identity_status,
                                       attributes, raw_record_id)
            VALUES (:org_id, :account_id, :source, :event_type, :rid, :dedupe_key, :event_time,
                    :ingested_at, :identity_status, CAST(:attributes AS jsonb), :raw_record_id)
            ON CONFLICT (org_id, source, dedupe_key) DO NOTHING
            """
        )
        prepared = [
            {**r, "org_id": self.org_id, "attributes": json.dumps(r["attributes"], default=str)}
            for r in rows
        ]
        inserted = 0
        with self.session() as session:
            for chunk in _chunks(prepared):
                inserted += cast(CursorResult[Any], session.execute(sql, list(chunk))).rowcount
        return inserted

    def insert_issues(self, rows: Sequence[dict[str, Any]]) -> int:
        sql = text(
            """
            INSERT INTO data_quality_issues (org_id, kind, source, ref, detected_at, detail)
            VALUES (:org_id, :kind, :source, :ref, :detected_at, CAST(:detail AS jsonb))
            ON CONFLICT (org_id, kind, source, ref) DO NOTHING
            """
        )
        prepared = [
            {**r, "org_id": self.org_id, "detail": json.dumps(r.get("detail", {}), default=str)}
            for r in rows
        ]
        inserted = 0
        with self.session() as session:
            for chunk in _chunks(prepared):
                inserted += cast(CursorResult[Any], session.execute(sql, list(chunk))).rowcount
        return inserted

    def record_revisions(self, source: str, detected_at: datetime) -> int:
        """Flag every raw row that has an earlier row with the same record ID and other content."""
        with self.session() as session:
            result = session.execute(
                text(
                    """
                    INSERT INTO data_quality_issues (org_id, kind, source, ref, detected_at, detail)
                    SELECT r.org_id, 'revised_record', r.source,
                           r.source_record_id || ':' || left(r.payload_sha256, 12), :at,
                           '{}'::jsonb
                    FROM raw_source_records r
                    WHERE r.org_id = :o AND r.source = :s AND EXISTS (
                        SELECT 1 FROM raw_source_records p
                        WHERE p.org_id = r.org_id AND p.source = r.source
                          AND p.source_record_id = r.source_record_id
                          AND p.payload_sha256 <> r.payload_sha256
                          AND (p.received_at, p.payload_sha256) < (r.received_at, r.payload_sha256))
                    ON CONFLICT (org_id, kind, source, ref) DO NOTHING
                    """
                ),
                {"o": self.org_id, "s": source, "at": detected_at},
            )
            return cast(CursorResult[Any], result).rowcount

    def count(self, table: str, **filters: str) -> int:
        allowed = {
            "accounts",
            "account_identities",
            "raw_source_records",
            "source_events",
            "data_quality_issues",
        }
        if table not in allowed or not all(k.isidentifier() for k in filters):
            raise ValueError("unsupported count request")
        clause = "".join(f" AND {k} = :{k}" for k in filters)
        with self.session() as session:
            return int(
                session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE org_id = :o{clause}"),  # noqa: S608
                    {"o": self.org_id, **filters},
                ).scalar_one()
            )

    def account_ids(self) -> list[UUID]:
        with self.session() as session:
            return list(
                session.execute(
                    text("SELECT id FROM accounts WHERE org_id = :o ORDER BY id"),
                    {"o": self.org_id},
                ).scalars()
            )
