import json
import math
import uuid
from collections import Counter
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.ingestion.adapters.sources import ADAPTERS
from app.ingestion.canonical import FIXTURE_DIR, INGEST_NOW, org_id_for, seed_canonical
from app.ingestion.pipeline import IngestReport, ingest_org
from app.ingestion.repository import IngestionRepository

pytestmark = pytest.mark.integration

ORG_SLUGS = ("acme-saas", "northwind-cloud", "globex-data")


def _create_org(owner_engine: Engine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:i, :s, :n)"),
            {"i": org_id, "s": f"{slug}-{org_id.hex[:6]}", "n": slug},
        )
    return org_id


@pytest.fixture(scope="module")
def loaded(owner_engine: Engine, app_engine: Engine) -> dict[str, tuple[uuid.UUID, IngestReport]]:
    reports = seed_canonical(owner_engine, app_engine, FIXTURE_DIR)
    return {slug: (org_id_for(slug), reports[slug]) for slug in ORG_SLUGS}


def _defects(slug: str) -> list[dict[str, object]]:
    lines = (FIXTURE_DIR / slug / "defects.ndjson").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_canonical_load_has_exactly_50_accounts_and_1000_emitted_events(
    loaded: dict[str, tuple[uuid.UUID, IngestReport]], app_engine: Engine
) -> None:
    accounts = sum(
        IngestionRepository(app_engine, org_id).count("accounts") for org_id, _ in loaded.values()
    )
    emitted = sum(sum(r.raw_received.values()) for _, r in loaded.values())

    assert accounts == 50
    assert emitted == 1000


def test_every_emitted_record_is_landed_or_deduplicated(
    loaded: dict[str, tuple[uuid.UUID, IngestReport]],
) -> None:
    for slug, (_, report) in loaded.items():
        defects = Counter(d["kind"] for d in _defects(slug))
        received = sum(report.raw_received.values())
        landed = sum(report.raw_inserted.values())
        assert received - landed == defects["duplicate"], slug


def test_reingesting_creates_zero_new_rows(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    org_id, first = loaded["acme-saas"]
    before = IngestionRepository(app_engine, org_id).count("source_events")

    again = ingest_org(app_engine, org_id, FIXTURE_DIR / "acme-saas", INGEST_NOW)

    assert sum(again.raw_inserted.values()) == 0
    assert again.total_events == 0
    assert again.accounts_inserted == 0
    assert IngestionRepository(app_engine, org_id).count("source_events") == before
    assert before == first.total_events


def test_reseeding_the_canonical_load_is_idempotent(
    owner_engine: Engine, app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    counts_before = {
        slug: IngestionRepository(app_engine, org_id).count("source_events")
        for slug, (org_id, _) in loaded.items()
    }

    again = seed_canonical(owner_engine, app_engine, FIXTURE_DIR)

    assert all(r.total_events == 0 for r in again.values())
    assert all(r.accounts_inserted == 0 for r in again.values())
    for slug, (org_id, _) in loaded.items():
        assert IngestionRepository(app_engine, org_id).count("source_events") == counts_before[slug]


def _issue_counts(app_engine: Engine, org_id: uuid.UUID) -> Counter[str]:
    repo = IngestionRepository(app_engine, org_id)
    with repo.session() as session:
        rows = session.execute(
            text("SELECT kind, count(*) FROM data_quality_issues WHERE org_id = :o GROUP BY kind"),
            {"o": org_id},
        ).all()
    return Counter({kind: int(n) for kind, n in rows})


def test_every_injected_defect_kind_is_detected_or_resolved(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    for slug, (org_id, _) in loaded.items():
        injected = Counter(d["kind"] for d in _defects(slug))
        found = _issue_counts(app_engine, org_id)

        assert found["revised_record"] == injected["revised_record"], slug
        # A revised record keeps its timezone-less timestamp, so it is flagged once per revision.
        revised_naive = _revised_timezone_less(slug)
        assert found["timezone_assumed"] == injected["timezone_less"] + revised_naive, slug
        assert found["malformed_record"] == 0, slug


def _revised_timezone_less(slug: str) -> int:
    rows = _defects(slug)
    naive = {d["clean_record_id"] for d in rows if d["kind"] == "timezone_less"}
    return sum(1 for d in rows if d["kind"] == "revised_record" and d["clean_record_id"] in naive)


def test_unmatched_or_ambiguous_events_keep_their_event_and_are_logged(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    for _, (org_id, _) in loaded.items():
        repo = IngestionRepository(app_engine, org_id)
        with repo.session() as session:
            unresolved = session.execute(
                text(
                    "SELECT count(*) FROM source_events WHERE org_id = :o "
                    "AND identity_status <> 'matched'"
                ),
                {"o": org_id},
            ).scalar_one()
            orphans = session.execute(
                text(
                    "SELECT count(*) FROM source_events e WHERE e.org_id = :o "
                    "AND e.identity_status <> 'matched' AND NOT EXISTS ("
                    "SELECT 1 FROM data_quality_issues d WHERE d.org_id = e.org_id "
                    "AND d.source = e.source AND d.ref = e.dedupe_key "
                    "AND d.kind IN ('unmatched_identity', 'ambiguous_identity'))"
                ),
                {"o": org_id},
            ).scalar_one()
        assert orphans == 0
        assert unresolved > 0


def test_zendesk_name_variants_resolve_at_least_95_percent(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    """Every organisation is checked, including ones with zero variants.

    Required correct per organisation is ceil(0.95 * expected). For expected <= 20 that is
    all of them, so small organisations must resolve every variant.
    """
    expected_total = 0
    for slug, (org_id, _) in loaded.items():
        variants = [d for d in _defects(slug) if d["kind"] == "name_variant"]
        with IngestionRepository(app_engine, org_id).session() as session:
            rows = session.execute(
                text(
                    "SELECT e.source_record_id, e.account_id::text, e.identity_status "
                    "FROM source_events e WHERE e.org_id = :o AND e.source = 'zendesk'"
                ),
                {"o": org_id},
            ).all()
            unresolved = session.execute(
                text(
                    "SELECT count(*) FROM source_events e WHERE e.org_id = :o "
                    "AND e.source = 'zendesk' AND e.identity_status <> 'matched' AND NOT EXISTS ("
                    "SELECT 1 FROM data_quality_issues d WHERE d.org_id = e.org_id "
                    "AND d.source = 'zendesk' AND d.ref = e.dedupe_key "
                    "AND d.kind IN ('unmatched_identity', 'ambiguous_identity'))"
                ),
                {"o": org_id},
            ).scalar_one()
        assert unresolved == 0, slug
        by_record = {rid: (acct, status) for rid, acct, status in rows}
        wanted = {str(v["clean_record_id"]): str(v["account_id"]) for v in variants}
        correct = sum(
            1
            for rid, acct in wanted.items()
            if by_record.get(rid, (None, None)) == (acct, "matched")
        )
        expected_total += len(wanted)
        required = math.ceil(0.95 * len(wanted))
        assert correct >= required, (slug, "expected", len(wanted), "correct", correct)
    assert expected_total > 0, "canonical fixture has no Zendesk name variants"


def test_canonical_stale_stripe_mappings_are_flagged_ambiguous_not_misattributed(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    for slug, (org_id, _) in loaded.items():
        stale = {str(d["clean_record_id"]) for d in _defects(slug) if d["kind"] == "stale_mapping"}
        if not stale:
            continue
        with IngestionRepository(app_engine, org_id).session() as session:
            rows = session.execute(
                text(
                    "SELECT source_record_id, identity_status FROM source_events "
                    "WHERE org_id = :o AND source = 'stripe'"
                ),
                {"o": org_id},
            ).all()
        got = {rid: status for rid, status in rows if rid in stale}
        assert len(got) == len(stale), slug
        assert set(got.values()) <= {"ambiguous"}, slug


def test_no_event_is_attributed_to_another_tenants_account(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    (org_a, _), (org_b, _) = loaded["acme-saas"], loaded["northwind-cloud"]
    repo_a = IngestionRepository(app_engine, org_a)

    with repo_a.session() as session:
        foreign_accounts = session.execute(
            text("SELECT count(*) FROM accounts WHERE org_id = :b"), {"b": org_b}
        ).scalar_one()
        foreign_events = session.execute(
            text("SELECT count(*) FROM source_events WHERE org_id = :b"), {"b": org_b}
        ).scalar_one()
        mismatched = session.execute(
            text(
                "SELECT count(*) FROM source_events e JOIN accounts a ON a.id = e.account_id "
                "WHERE a.org_id <> e.org_id"
            )
        ).scalar_one()

    assert (foreign_accounts, foreign_events, mismatched) == (0, 0, 0)
    assert repo_a.count("source_events") > 0
    assert set(repo_a.account_ids()).isdisjoint(
        IngestionRepository(app_engine, org_b).account_ids()
    )


def test_writing_a_row_for_another_org_is_rejected_by_rls(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    from sqlalchemy.exc import ProgrammingError

    (org_a, _), (org_b, _) = loaded["acme-saas"], loaded["northwind-cloud"]

    with pytest.raises(ProgrammingError, match="row-level security"):
        _insert_as(app_engine, org_a, org_b)


def _insert_as(engine: Engine, context_org: uuid.UUID, row_org: uuid.UUID) -> None:
    repo = IngestionRepository(engine, context_org)
    with repo.session() as session:
        session.execute(
            text(
                "INSERT INTO data_quality_issues (org_id, kind, source, ref) "
                "VALUES (:o, 'duplicate', 'stripe', 'cross-tenant')"
            ),
            {"o": row_org},
        )


def test_no_events_without_a_matching_raw_record(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    for _, (org_id, _) in loaded.items():
        with IngestionRepository(app_engine, org_id).session() as session:
            orphans = session.execute(
                text(
                    "SELECT count(*) FROM source_events e WHERE e.org_id = :o AND NOT EXISTS "
                    "(SELECT 1 FROM raw_source_records r WHERE r.org_id = e.org_id "
                    "AND r.id = e.raw_record_id)"
                ),
                {"o": org_id},
            ).scalar_one()
        assert orphans == 0


def _event_fingerprint(app_engine: Engine, org_id: uuid.UUID) -> list[tuple[object, ...]]:
    with IngestionRepository(app_engine, org_id).session() as session:
        rows = session.execute(
            text(
                "SELECT e.source, e.dedupe_key, a.canonical_name, e.event_type, e.event_time, "
                "e.ingested_at, e.identity_status FROM source_events e "
                "LEFT JOIN accounts a ON a.org_id = e.org_id AND a.id = e.account_id "
                "WHERE e.org_id = :o ORDER BY e.source, e.dedupe_key"
            ),
            {"o": org_id},
        ).all()
    return [tuple(r) for r in rows]


def _copy_with_new_account_ids(source_dir: Path, target: Path, salt: str, reverse: bool) -> None:
    """Copy a canonical org. Account IDs are globally unique, so each copy needs its own."""
    target.mkdir(parents=True)
    for file in source_dir.glob("*.ndjson"):
        lines = file.read_text().splitlines()
        if file.name == "accounts.ndjson":
            records = [json.loads(line) for line in lines]
            for record in records:
                record["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, salt + record["id"]))
            lines = [json.dumps(record) for record in records]
        elif file.name not in {"ground_truth.ndjson", "defects.ndjson"} and reverse:
            lines = list(reversed(lines))
        (target / file.name).write_text("\n".join(lines) + "\n")


def test_arrival_order_does_not_change_the_normalised_events(
    owner_engine: Engine, app_engine: Engine, tmp_path: Path
) -> None:
    in_order = tmp_path / "in-order"
    reversed_dir = tmp_path / "reversed"
    _copy_with_new_account_ids(FIXTURE_DIR / "acme-saas", in_order, "ref:", reverse=False)
    _copy_with_new_account_ids(FIXTURE_DIR / "acme-saas", reversed_dir, "rev:", reverse=True)
    reference = _create_org(owner_engine, "order-ref")
    reordered = _create_org(owner_engine, "order-reversed")

    ingest_org(app_engine, reference, in_order, INGEST_NOW)
    ingest_org(app_engine, reordered, reversed_dir, INGEST_NOW)

    expected = _event_fingerprint(app_engine, reference)
    assert expected
    assert _event_fingerprint(app_engine, reordered) == expected


def _copy_delivered_on_time(source_dir: Path, target: Path, salt: str) -> None:
    """Copy a canonical org, delivering every record at its event time.

    Account IDs are rewritten so the copy can be ingested alongside the original. Only arrival
    time changes: the normalised events should then match the late-arriving original, apart
    from `ingested_at`.
    """
    target.mkdir(parents=True)
    for file in source_dir.glob("*.ndjson"):
        if file.name in {"ground_truth.ndjson", "defects.ndjson"}:
            continue
        lines = file.read_text().splitlines()
        if file.name == "accounts.ndjson":
            records = [json.loads(line) for line in lines]
            for record in records:
                record["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, salt + record["id"]))
            lines = [json.dumps(record) for record in records]
        else:
            lines = [_delivered_at_event_time(file.name, line) for line in lines]
        (target / file.name).write_text("\n".join(lines) + "\n")


def _delivered_at_event_time(source: str, line: str) -> str:
    record = json.loads(line)
    parsed = ADAPTERS[source.removesuffix(".ndjson")](record["payload"])
    moment = parsed.event_time
    if parsed.timezone_assumed:
        moment = moment.replace(tzinfo=UTC)
    record["received_at"] = moment.isoformat()
    return json.dumps(record)


def test_late_arrival_normalises_to_the_same_event_as_on_time_delivery(
    owner_engine: Engine, app_engine: Engine, tmp_path: Path
) -> None:
    late_dir = tmp_path / "late"
    on_time_dir = tmp_path / "on-time"
    _copy_with_new_account_ids(FIXTURE_DIR / "acme-saas", late_dir, "late-arrival:", reverse=False)
    _copy_delivered_on_time(FIXTURE_DIR / "acme-saas", on_time_dir, "on-time-delivery:")
    late_org = _create_org(owner_engine, "late-arrival")
    on_time_org = _create_org(owner_engine, "on-time")

    ingest_org(app_engine, late_org, late_dir, INGEST_NOW)
    ingest_org(app_engine, on_time_org, on_time_dir, INGEST_NOW)

    late = _event_fingerprint(app_engine, late_org)
    on_time = _event_fingerprint(app_engine, on_time_org)
    assert late, "fixture produced no events"
    assert [row[:5] for row in late] == [row[:5] for row in on_time]
    assert [row[6] for row in late] == [row[6] for row in on_time]


def test_late_arrivals_keep_their_original_event_time_and_later_ingested_at(
    app_engine: Engine, loaded: dict[str, tuple[uuid.UUID, IngestReport]]
) -> None:
    org_id, _ = loaded["acme-saas"]
    with IngestionRepository(app_engine, org_id).session() as session:
        late = session.execute(
            text(
                "SELECT count(*) FROM source_events WHERE org_id = :o "
                "AND ingested_at - event_time > interval '3 days'"
            ),
            {"o": org_id},
        ).scalar_one()
        backwards = session.execute(
            text(
                "SELECT count(*) FROM source_events WHERE org_id = :o AND ingested_at < event_time"
            ),
            {"o": org_id},
        ).scalar_one()
        flagged = session.execute(
            text(
                "SELECT count(*) FROM data_quality_issues "
                "WHERE org_id = :o AND kind = 'late_arrival'"
            ),
            {"o": org_id},
        ).scalar_one()

    assert late > 0
    assert backwards == 0
    assert flagged >= late - 5


def test_account_id_owned_by_another_tenant_is_an_error_not_a_silent_skip(
    owner_engine: Engine,
    app_engine: Engine,
    loaded: dict[str, tuple[uuid.UUID, IngestReport]],
) -> None:
    intruder = _create_org(owner_engine, "id-collision")

    with pytest.raises(ValueError, match="already exist outside this organisation"):
        ingest_org(app_engine, intruder, FIXTURE_DIR / "acme-saas", INGEST_NOW)
