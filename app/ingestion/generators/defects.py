"""Defect injection: turns clean events into the messy records a source would really deliver."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.ingestion.generators.config import DefectRates, GenerationConfig, OrgSpec, stage_rng
from app.ingestion.generators.events import CleanEvent
from app.ingestion.generators.payloads import (
    FREEMAIL_DOMAIN,
    Flags,
    build_payload,
    canonical_name,
    record_id,
    revise,
)
from app.ingestion.generators.world import AccountWorld

BATCH_NAMESPACE = uuid.UUID("6f1c1d0e-5b7a-4d6e-9d57-2f6c3a0b9a11")
SUFFIX_VARIANTS = {
    "Inc": ("Inc.", "Incorporated", ""),
    "LLC": ("L.L.C.", "LLC.", ""),
    "Ltd": ("Ltd.", "Limited", ""),
    "Group": ("Grp", "Group.", ""),
    "Co": ("Co.", "Company", ""),
}
REGISTERED_ZENDESK_SHARE = 0.6
GMAIL_ALIAS_SHARE = 0.5


@dataclass(slots=True)
class EmittedRecord:
    org_slug: str
    source: str
    source_record_id: str
    payload: dict[str, object]
    received_at: datetime
    batch_id: str
    event_time: datetime
    account_index: int
    clean_record_id: str


@dataclass(slots=True)
class DefectEntry:
    org_slug: str
    source: str
    kind: str
    clean_record_id: str
    account_index: int
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class InjectionResult:
    records: list[EmittedRecord]
    defects: list[DefectEntry]
    clean_count: int
    emitted_count: int
    kept_count: int
    late_eligible: int


@dataclass(frozen=True)
class AccountRegistry:
    """Per-account identity facts the platform is assumed to hold before ingestion."""

    zendesk_registered: frozenset[int]
    gmail_alias: dict[int, str]
    stripe_stale: dict[int, int]


def clean_count_for_budget(config: GenerationConfig, budget: int) -> int:
    rates = config.defects
    return round(budget / (1.0 - rates.missing + rates.duplicate + rates.revised))


def build_registry(
    config: GenerationConfig, org: OrgSpec, accounts: list[AccountWorld]
) -> AccountRegistry:
    rng = stage_rng(config, org, "defects")
    registered = frozenset(
        i for i in range(len(accounts)) if rng.random() < REGISTERED_ZENDESK_SHARE
    )
    alias = {
        i: f"{accounts[i].domain.split('.')[0]}-group.example.test"
        for i in range(len(accounts))
        if rng.random() < GMAIL_ALIAS_SHARE
    }
    stale: dict[int, int] = {}
    stripe_accounts = [i for i, a in enumerate(accounts) if "stripe" in a.sources]
    for i in stripe_accounts:
        if rng.random() < config.defects.stripe_stale_mapping:
            other = int(rng.integers(0, len(accounts)))
            if other != i:
                stale[i] = other
    return AccountRegistry(registered, alias, stale)


def _name_variant(account: AccountWorld, rng: np.random.Generator) -> str:
    base, suffix = account.name, account.suffix
    choice = int(rng.integers(0, 4))
    if choice == 0:
        return f"{base} {suffix}".upper()
    if choice == 1:
        return base.lower()
    if choice == 2:
        return f"{base} {SUFFIX_VARIANTS[suffix][int(rng.integers(0, 3))]}".strip()
    letters = list(base)
    pos = int(rng.integers(1, max(2, len(letters) - 2)))
    letters[pos], letters[pos + 1] = letters[pos + 1], letters[pos]
    return f"{''.join(letters)} {suffix}"


def _lag(rng: np.random.Generator) -> timedelta:
    return timedelta(seconds=int(rng.integers(60, 6 * 3600)))


def inject(
    config: GenerationConfig,
    org: OrgSpec,
    accounts: list[AccountWorld],
    registry: AccountRegistry,
    source: str,
    clean: list[CleanEvent],
    budget: int,
) -> InjectionResult:
    rates = config.defects
    rng = stage_rng(config, org, "defects", source)
    anchor = config.anchor
    n = len(clean)
    missing_idx = set(rng.choice(n, size=round(rates.missing * n), replace=False).tolist())
    kept = [e for i, e in enumerate(clean) if i not in missing_idx]
    n_rev = round(rates.revised * n)
    n_dup = budget - len(kept) - n_rev
    if n_dup < 0 or n_dup > len(kept):
        raise ValueError(f"cannot reach budget {budget} for {org.slug}/{source}")

    defects: list[DefectEntry] = [
        DefectEntry(org.slug, source, "missing_source", record_id(clean[i]), clean[i].account_index)
        for i in sorted(missing_idx)
    ]
    records: list[EmittedRecord] = []
    late_cutoff = anchor - timedelta(days=46)

    for event in kept:
        account = accounts[event.account_index]
        flags, tags = _flags(event, account, registry, accounts, rates, rng, source)
        rid = record_id(event)
        for kind, detail in tags:
            defects.append(DefectEntry(org.slug, source, kind, rid, event.account_index, detail))
        received = event.event_time + _lag(rng)
        if event.event_time <= late_cutoff and rng.random() < rates.late_arrival:
            received = event.event_time + timedelta(days=float(rng.uniform(3, 45)))
            defects.append(DefectEntry(org.slug, source, "late_arrival", rid, event.account_index))
        received = min(received, anchor)
        records.append(
            EmittedRecord(
                org.slug,
                source,
                rid,
                build_payload(event, account, flags),
                received,
                "",
                event.event_time,
                event.account_index,
                rid,
            )
        )

    originals = list(records)
    for pos in rng.choice(len(originals), size=n_rev, replace=False).tolist():
        base = originals[pos]
        later = min(base.received_at + timedelta(days=float(rng.uniform(1, 10))), anchor)
        records.append(
            EmittedRecord(
                org.slug,
                source,
                base.source_record_id,
                revise(source, base.payload),
                later,
                "",
                base.event_time,
                base.account_index,
                base.clean_record_id,
            )
        )
        defects.append(
            DefectEntry(
                org.slug, source, "revised_record", base.clean_record_id, base.account_index
            )
        )
    for pos in rng.choice(len(originals), size=n_dup, replace=False).tolist():
        base = originals[pos]
        again = min(base.received_at + timedelta(hours=float(rng.uniform(0, 72))), anchor)
        records.append(
            EmittedRecord(
                org.slug,
                source,
                base.source_record_id,
                base.payload,
                again,
                "",
                base.event_time,
                base.account_index,
                base.clean_record_id,
            )
        )
        defects.append(
            DefectEntry(org.slug, source, "duplicate", base.clean_record_id, base.account_index)
        )

    _assign_batches_and_order(records, rng, rates.out_of_order_batches, defects, org.slug, source)
    late_eligible = sum(1 for e in kept if e.event_time <= late_cutoff)
    return InjectionResult(records, defects, n, len(records), len(kept), late_eligible)


def _flags(
    event: CleanEvent,
    account: AccountWorld,
    registry: AccountRegistry,
    accounts: list[AccountWorld],
    rates: DefectRates,
    rng: np.random.Generator,
    source: str,
) -> tuple[Flags, list[tuple[str, dict[str, object]]]]:
    r = rates
    idx = event.account_index
    tags: list[tuple[str, dict[str, object]]] = []
    if source == "zendesk" and rng.random() < r.zendesk_name_variant:
        variant = _name_variant(account, rng)
        if variant != canonical_name(account):
            tags.append(("name_variant", {"variant": variant}))
            return Flags(zendesk_name=variant), tags
    if source == "gmail" and rng.random() < r.gmail_domain_mismatch:
        alias = registry.gmail_alias.get(idx)
        if alias is not None and rng.random() < 0.5:
            tags.append(("domain_alias", {"domain": alias}))
            return Flags(gmail_domain=alias), tags
        tags.append(("domain_freemail", {"domain": FREEMAIL_DOMAIN}))
        return Flags(gmail_domain=FREEMAIL_DOMAIN), tags
    if source == "product" and rng.random() < r.product_timezone_less:
        tags.append(("timezone_less", {}))
        return Flags(product_tz_naive=True), tags
    if source == "salesforce" and rng.random() < r.salesforce_missing_account_id:
        tags.append(("sf_account_id_missing", {}))
        return Flags(sf_account_id_missing=True), tags
    if source == "stripe" and idx in registry.stripe_stale:
        other = registry.stripe_stale[idx]
        return (
            Flags(stripe_crm_ref=accounts[other].salesforce_id),
            [("stale_mapping", {"other_account_index": other})],
        )
    return Flags(), tags


def _assign_batches_and_order(
    records: list[EmittedRecord],
    rng: np.random.Generator,
    shuffle_rate: float,
    defects: list[DefectEntry],
    org_slug: str,
    source: str,
) -> None:
    records.sort(key=lambda r: (r.received_at, r.source_record_id))
    by_day: dict[str, list[int]] = {}
    for position, record in enumerate(records):
        day = record.received_at.strftime("%Y-%m-%d")
        record.batch_id = str(uuid.uuid5(BATCH_NAMESPACE, f"{org_slug}/{source}/{day}"))
        by_day.setdefault(day, []).append(position)
    for day in sorted(by_day):
        positions = by_day[day]
        if len(positions) > 1 and rng.random() < shuffle_rate:
            shuffled = rng.permutation(len(positions))
            block = [records[positions[k]] for k in shuffled]
            for slot, record in zip(positions, block, strict=True):
                records[slot] = record
            defects.append(
                DefectEntry(
                    org_slug, source, "out_of_order_batch", day, -1, {"rows": len(positions)}
                )
            )
