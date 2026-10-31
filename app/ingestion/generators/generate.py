"""Generation entry point: writes the canonical fixture files and manifest, then validates."""

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app.ingestion.generators.config import SOURCES, GenerationConfig, OrgSpec
from app.ingestion.generators.defects import (
    AccountRegistry,
    InjectionResult,
    build_registry,
    clean_count_for_budget,
    inject,
)
from app.ingestion.generators.events import generate_clean_events
from app.ingestion.generators.payloads import canonical_name
from app.ingestion.generators.world import AccountWorld, generate_world

GENERATOR_VERSION = "1"
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class GenerationValidationError(Exception):
    pass


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_iso)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _write_lines(path: Path, rows: Sequence[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_dump(row) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _account_master(accounts: list[AccountWorld], registry: object) -> list[dict[str, object]]:
    """Reference data the platform holds before ingestion: names, domains and known identities."""
    rows = []
    for index, a in enumerate(accounts):
        identities: list[dict[str, object]] = [
            {"source": "product", "key": a.product_key, "method": "exact_id", "confidence": 1.0},
            {
                "source": "salesforce",
                "key": a.salesforce_id,
                "method": "exact_id",
                "confidence": 1.0,
            },
            {"source": "stripe", "key": a.stripe_customer, "method": "exact_id", "confidence": 1.0},
            {"source": "gmail", "key": a.domain, "method": "domain", "confidence": 1.0},
        ]
        if index in registry.zendesk_registered:  # type: ignore[attr-defined]
            identities.append(
                {
                    "source": "zendesk",
                    "key": str(a.zendesk_org_id),
                    "method": "exact_id",
                    "confidence": 1.0,
                }
            )
        alias = registry.gmail_alias.get(index)  # type: ignore[attr-defined]
        if alias:
            identities.append(
                {"source": "gmail", "key": alias, "method": "domain", "confidence": 0.9}
            )
        rows.append(
            {
                "id": str(a.id),
                "canonical_name": canonical_name(a),
                "primary_domain": a.domain,
                "segment": a.segment,
                "plan_tier": a.plan_tier,
                "region": a.region,
                "timezone": a.timezone,
                "created_at": a.contract_start,
                "renewal_date": a.pending_renewal,
                "identities": identities,
            }
        )
    return rows


def _ground_truth(accounts: list[AccountWorld]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for a in accounts:
        rows.append(
            {
                "kind": "account",
                "account_id": str(a.id),
                "profile": a.profile,
                "term_type": a.term_type,
                "segment": a.segment,
                "ended_at": a.ended_at,
                "pending_renewal": a.pending_renewal,
            }
        )
        for t in a.terms:
            rows.append({"kind": "term", "account_id": str(a.id), **asdict(t)})
    return rows


@dataclass
class _Tally:
    clean: int = 0
    kept: int = 0
    late_eligible: int = 0
    batches: int = 0
    kinds: Counter[str] = field(default_factory=Counter)

    def add(self, result: InjectionResult) -> None:
        self.clean += result.clean_count
        self.kept += result.kept_count
        self.late_eligible += result.late_eligible
        sizes = Counter(r.batch_id for r in result.records)
        self.batches += sum(1 for size in sizes.values() if size > 1)
        self.kinds.update(d.kind for d in result.defects)


def _tolerance(rate: float, denominator: int) -> float:
    """Three binomial standard errors around the configured rate, with a floor of two points."""
    return max(0.02, 3 * math.sqrt(rate * (1 - rate) / denominator))


def _rate_row(
    source: str,
    metric: str,
    kinds: tuple[str, ...],
    configured: float,
    count: int,
    denominator: int,
    basis: str,
) -> dict[str, object]:
    achieved = count / denominator if denominator else 0.0
    tolerance = _tolerance(configured, denominator) if denominator else 0.0
    return {
        "source": source,
        "metric": metric,
        "defect_kinds": list(kinds),
        "configured_rate": configured,
        "achieved_count": count,
        "denominator": denominator,
        "denominator_basis": basis,
        "achieved_rate": round(achieved, 4),
        "tolerance": round(tolerance, 4),
        "within_tolerance": denominator > 0 and abs(achieved - configured) <= tolerance,
    }


def _defect_rates(
    config: GenerationConfig,
    tallies: dict[str, _Tally],
    stripe_accounts: int,
    stale_accounts: int,
) -> list[dict[str, object]]:
    r = config.defects
    specs: list[tuple[str, str, tuple[str, ...], float, str]] = [
        ("product", "timezone_less", ("timezone_less",), r.product_timezone_less, "kept"),
        (
            "gmail",
            "domain_mismatch",
            ("domain_freemail", "domain_alias"),
            r.gmail_domain_mismatch,
            "kept",
        ),
        ("zendesk", "name_variant", ("name_variant",), r.zendesk_name_variant, "kept"),
        (
            "salesforce",
            "account_id_missing",
            ("sf_account_id_missing",),
            r.salesforce_missing_account_id,
            "kept",
        ),
    ]
    for source in SOURCES:
        specs += [
            (source, "missing_source", ("missing_source",), r.missing, "clean"),
            (source, "duplicate", ("duplicate",), r.duplicate, "clean"),
            (source, "revised_record", ("revised_record",), r.revised, "clean"),
            (source, "late_arrival", ("late_arrival",), r.late_arrival, "late_eligible"),
            (
                source,
                "out_of_order_batch",
                ("out_of_order_batch",),
                r.out_of_order_batches,
                "batches",
            ),
        ]
    rows = []
    for source, metric, kinds, configured, basis in specs:
        tally = tallies[source]
        count = sum(tally.kinds[k] for k in kinds)
        rows.append(
            _rate_row(source, metric, kinds, configured, count, getattr(tally, basis), basis)
        )
    rows.append(
        _rate_row(
            "stripe",
            "stale_account",
            ("stale_mapping",),
            config.defects.stripe_stale_mapping,
            stale_accounts,
            stripe_accounts,
            "stripe accounts",
        )
    )
    return rows


def _write_org(
    config: GenerationConfig, org: OrgSpec, root: Path
) -> tuple[list[AccountWorld], dict[str, InjectionResult], AccountRegistry]:
    accounts = generate_world(config, org)
    registry = build_registry(config, org, accounts)
    results: dict[str, InjectionResult] = {}
    defect_rows: list[dict[str, object]] = []
    for source in SOURCES:
        budget = config.org_source_budget(org, source)
        clean = generate_clean_events(
            config, org, accounts, source, clean_count_for_budget(config, budget)
        )
        result = inject(config, org, accounts, registry, source, clean, budget)
        results[source] = result
        _write_lines(
            root / org.slug / f"{source}.ndjson",
            [
                {
                    "source_record_id": r.source_record_id,
                    "received_at": r.received_at,
                    "batch_id": r.batch_id,
                    "payload": r.payload,
                }
                for r in result.records
            ],
        )
        defect_rows.extend(
            {
                "org": d.org_slug,
                "source": d.source,
                "kind": d.kind,
                "clean_record_id": d.clean_record_id,
                "account_id": (str(accounts[d.account_index].id) if d.account_index >= 0 else None),
                "detail": d.detail,
            }
            for d in result.defects
        )
    _write_lines(root / org.slug / "accounts.ndjson", _account_master(accounts, registry))
    _write_lines(root / org.slug / "ground_truth.ndjson", _ground_truth(accounts))
    _write_lines(root / org.slug / "defects.ndjson", defect_rows)
    return accounts, results, registry


def generate(config: GenerationConfig, root: Path) -> Path:
    """Overwrite the fixture files and manifest in `root`, then validate them."""
    manifest: dict[str, object] = {
        "generator_version": GENERATOR_VERSION,
        "seed": config.seed,
        "config_hash": config.config_hash(),
        "config": config.to_dict(),
        "orgs": {},
    }
    pooled: list[bool] = []
    total_events = 0
    by_term: dict[str, list[bool]] = {}
    tallies = {source: _Tally() for source in SOURCES}
    stripe_accounts = 0
    stale_accounts = 0
    for org in config.orgs:
        accounts, results, registry = _write_org(config, org, root)
        for source, result in results.items():
            tallies[source].add(result)
        stripe_accounts += sum(1 for a in accounts if "stripe" in a.sources)
        stale_accounts += len(registry.stripe_stale)
        observable = [
            (a.term_type, t.churned) for a in accounts for t in a.terms if t.label_observable
        ]
        pooled.extend(c for _, c in observable)
        for term_type, churned in observable:
            by_term.setdefault(term_type, []).append(churned)
        total_events += sum(r.emitted_count for r in results.values())
        defects: Counter[str] = Counter()
        for result in results.values():
            defects.update(d.kind for d in result.defects)
        manifest["orgs"][org.slug] = {  # type: ignore[index]
            "display_name": org.display_name,
            "accounts": len(accounts),
            "events": {s: r.emitted_count for s, r in results.items()},
            "clean_events": {s: r.clean_count for s, r in results.items()},
            "defects": dict(sorted(defects.items())),
            "observable_renewals": len(observable),
            "churn_rate": round(sum(c for _, c in observable) / max(len(observable), 1), 4),
        }
    manifest["pooled_churn_rate"] = round(sum(pooled) / max(len(pooled), 1), 4)
    manifest["churn_rate_by_term"] = {
        k: {"n": len(v), "rate": round(sum(v) / len(v), 4)} for k, v in sorted(by_term.items())
    }
    manifest["total_events"] = total_events
    manifest["defect_rates"] = _defect_rates(config, tallies, stripe_accounts, stale_accounts)
    manifest["files"] = {
        str(p.relative_to(root)): _sha256(p) for p in sorted(root.rglob("*.ndjson"))
    }
    (root / "manifest.json").write_text(_dump(manifest) + "\n", encoding="utf-8")
    validate(config, root)
    return root


def validate(config: GenerationConfig, root: Path) -> None:
    """Raise GenerationValidationError on the first violated rule."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest["total_events"] != config.total_events:
        problems.append(f"total events {manifest['total_events']} != {config.total_events}")
    accounts = sum(o["accounts"] for o in manifest["orgs"].values())
    if accounts != config.total_accounts:
        problems.append(f"accounts {accounts} != {config.total_accounts}")
    for source in SOURCES:
        got = sum(o["events"][source] for o in manifest["orgs"].values())
        if got != config.source_budget(source):
            problems.append(f"{source} events {got} != {config.source_budget(source)}")
    low, high = config.churn_rate_range
    if not low <= manifest["pooled_churn_rate"] <= high:
        problems.append(f"churn rate {manifest['pooled_churn_rate']} outside [{low}, {high}]")
    for row in manifest["defect_rates"]:
        achieved = row["achieved_count"] / row["denominator"] if row["denominator"] else 0.0
        if row["denominator"] == 0 or abs(achieved - row["configured_rate"]) > _tolerance(
            row["configured_rate"], row["denominator"]
        ):
            problems.append(
                f"defect rate {row['source']}/{row['metric']} achieved {achieved:.4f}"
                f" vs configured {row['configured_rate']} (tolerance {row['tolerance']})"
            )
    for name, digest in manifest["files"].items():
        if _sha256(root / name) != digest:
            problems.append(f"checksum mismatch: {name}")
    for path in root.rglob("*.ndjson"):
        for match in EMAIL_PATTERN.finditer(path.read_text(encoding="utf-8")):
            if not match.group(0).endswith("example.test"):
                problems.append(f"real-looking email in {path.name}")
                break
    if problems:
        raise GenerationValidationError("; ".join(problems))
