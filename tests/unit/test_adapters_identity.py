import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.ingestion.adapters.base import IdentityHints, MalformedRecordError
from app.ingestion.adapters.sources import ADAPTERS
from app.ingestion.identity import IdentityIndex, normalise_name, resolve

ACME = uuid.uuid4()
OTHER = uuid.uuid4()

VALID: dict[str, dict[str, Any]] = {
    "product": {
        "event_id": "pe_1",
        "account_external_id": "acct_x",
        "user_id": "u1",
        "event": "login",
        "ts": "2026-03-01T09:00:00-05:00",
        "properties": {},
    },
    "gmail": {
        "id": "gm_1",
        "thread_id": "t1",
        "kind": "thread_sent",
        "sent_at": "2026-03-01T09:00:00Z",
        "external_domain": "Acme.Example.Test",
        "participant_count": 3,
        "exec_contact": True,
    },
    "zendesk": {
        "id": "zd_1",
        "organization_id": 123,
        "organization_name": "Acme Inc",
        "type": "ticket.created",
        "created_at": "2026-03-01T09:00:00Z",
        "ticket_id": 9,
    },
    "salesforce": {
        "Id": "sf_1",
        "AccountId": "001ABC",
        "Type": "opportunity_stage_change",
        "OccurredAt": "2026-03-01T09:00:00Z",
        "StageName": "Closed-Won",
    },
    "stripe": {
        "id": "evt_1",
        "customer": "cus_1",
        "type": "invoice.paid",
        "created": 1772355600,
        "data": {"amount": 1000},
        "metadata": {"crm_id": "001ABC"},
    },
}


@pytest.mark.parametrize("source", list(VALID))
def test_valid_payload_parses_with_utc_time(source: str) -> None:
    parsed = ADAPTERS[source](VALID[source])

    assert parsed.source == source
    assert parsed.event_time.utcoffset() is not None or parsed.timezone_assumed
    assert parsed.source_record_id


def test_product_offset_is_converted_to_utc() -> None:
    parsed = ADAPTERS["product"](VALID["product"])

    assert parsed.event_time == datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    assert parsed.timezone_assumed is False


def test_product_timestamp_without_offset_is_flagged_not_guessed() -> None:
    payload = {**VALID["product"], "ts": "2026-03-01T09:00:00"}

    parsed = ADAPTERS["product"](payload)

    assert parsed.timezone_assumed is True
    assert parsed.event_time.tzinfo is None


@pytest.mark.parametrize(
    ("source", "field"),
    [("gmail", "sent_at"), ("zendesk", "created_at"), ("salesforce", "OccurredAt")],
)
def test_timestamp_without_offset_is_rejected_for_iso_sources(source: str, field: str) -> None:
    payload = {**VALID[source], field: "2026-03-01T09:00:00"}

    with pytest.raises(MalformedRecordError, match="no UTC offset"):
        ADAPTERS[source](payload)


def test_stripe_epoch_timestamp_is_always_utc() -> None:
    parsed = ADAPTERS["stripe"](VALID["stripe"])

    assert parsed.event_time.utcoffset() == timedelta(0)
    assert parsed.timezone_assumed is False


def test_salesforce_stage_spelling_variants_are_normalised() -> None:
    parsed = ADAPTERS["salesforce"](VALID["salesforce"])

    assert parsed.attributes["stage"] == "Closed Won"


@pytest.mark.parametrize(
    ("source", "broken"),
    [
        ("product", {"event": "login"}),
        ("product", {**VALID["product"], "event": "teleport"}),
        ("gmail", {**VALID["gmail"], "sent_at": "2026-03-01T09:00:00"}),
        ("zendesk", {**VALID["zendesk"], "type": "ticket.exploded"}),
        (
            "salesforce",
            {"Id": "sf_2", "Type": "activity_logged", "OccurredAt": "2026-01-01T00:00:00Z"},
        ),
        ("stripe", {**VALID["stripe"], "created": "yesterday"}),
        ("stripe", {}),
    ],
)
def test_malformed_payloads_are_rejected_not_guessed(source: str, broken: dict[str, Any]) -> None:
    with pytest.raises(MalformedRecordError):
        ADAPTERS[source](broken)


def _index() -> IdentityIndex:
    index = IdentityIndex()
    index.add("salesforce", "001ABC", ACME, "exact_id")
    index.add("salesforce", "001ZZZ", OTHER, "exact_id")
    index.add("gmail", "acme.example.test", ACME, "domain")
    index.add_name("Acme Systems Inc", ACME)
    index.add_name("Cedar Labs LLC", OTHER)
    return index


def test_exact_key_wins() -> None:
    result = resolve(_index(), "salesforce", IdentityHints(exact_key="001ABC"))

    assert (result.status, result.account_id, result.method) == ("matched", ACME, "exact_id")


def test_domain_match_is_used_when_there_is_no_exact_key() -> None:
    result = resolve(_index(), "gmail", IdentityHints(domain="acme.example.test"))

    assert (result.status, result.method) == ("matched", "domain")


def test_unknown_domain_is_unmatched_not_guessed() -> None:
    result = resolve(_index(), "gmail", IdentityHints(domain="nobody.example.test"))

    assert result.status == "unmatched"
    assert result.account_id is None


def test_personal_domain_is_ambiguous() -> None:
    result = resolve(_index(), "gmail", IdentityHints(domain="freemail.example.test"))

    assert result.status == "ambiguous"
    assert result.account_id is None


@pytest.mark.parametrize(
    "variant", ["ACME SYSTEMS INC", "acme systems", "Acme Systems Incorporated", "Acme Systmes Inc"]
)
def test_name_variants_resolve_by_fuzzy_match(variant: str) -> None:
    result = resolve(_index(), "zendesk", IdentityHints(name=variant))

    assert (result.status, result.account_id) == ("matched", ACME)


def test_unrelated_name_is_unmatched() -> None:
    result = resolve(_index(), "zendesk", IdentityHints(name="Totally Different Corp"))

    assert result.status == "unmatched"


def test_conflicting_crm_reference_is_ambiguous() -> None:
    index = _index()
    index.add("stripe", "cus_1", ACME, "exact_id")

    result = resolve(
        index,
        "stripe",
        IdentityHints(exact_key="cus_1", cross_ref="001ZZZ", cross_ref_source="salesforce"),
    )

    assert result.status == "ambiguous"
    assert result.account_id is None


def test_stale_stripe_mapping_is_ambiguous_not_attributed_to_either_account() -> None:
    index = _index()
    index.add("stripe", "cus_1", ACME, "exact_id")
    payload = {**VALID["stripe"], "metadata": {"crm_id": "001ZZZ"}}

    parsed = ADAPTERS["stripe"](payload)
    result = resolve(index, "stripe", parsed.hints)

    assert result.status == "ambiguous"
    assert result.account_id is None


def test_agreeing_crm_reference_is_matched() -> None:
    index = _index()
    index.add("stripe", "cus_1", ACME, "exact_id")

    result = resolve(
        index,
        "stripe",
        IdentityHints(exact_key="cus_1", cross_ref="001ABC", cross_ref_source="salesforce"),
    )

    assert result.status == "matched"


def test_no_identity_signal_is_unmatched() -> None:
    assert resolve(_index(), "gmail", IdentityHints()).status == "unmatched"


def test_name_normalisation_drops_case_punctuation_and_legal_suffix() -> None:
    assert normalise_name("ACME  Systems, Inc.") == normalise_name("acme systems") == "acme systems"


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_zendesk_name_resolution_precision_against_ground_truth() -> None:
    """Name-only resolution on the canonical Zendesk variants, checked against the defect log.

    Every variant must resolve to its true account by name alone (precision 1.0, recall 1.0).
    No variant may resolve to a wrong account, since that would be silent misattribution.
    """
    root = Path(__file__).resolve().parents[2] / "fixtures" / "canonical"
    matched_correct = matched_wrong = total = 0
    for slug in ("acme-saas", "northwind-cloud", "globex-data"):
        index = IdentityIndex()
        for account in _read_ndjson(root / slug / "accounts.ndjson"):
            index.add_name(account["canonical_name"], uuid.UUID(account["id"]))
        for defect in _read_ndjson(root / slug / "defects.ndjson"):
            if defect["kind"] != "name_variant":
                continue
            total += 1
            result = resolve(index, "zendesk", IdentityHints(name=defect["detail"]["variant"]))
            if result.status != "matched":
                continue
            if result.account_id == uuid.UUID(defect["account_id"]):
                matched_correct += 1
            else:
                matched_wrong += 1

    assert total == 10
    assert matched_wrong == 0
    assert matched_correct == total
