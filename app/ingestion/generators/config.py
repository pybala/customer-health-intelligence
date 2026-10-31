"""Generation configuration. Everything that shapes the synthetic data lives here and is
hashed into the manifest, so a change in any rate or coefficient changes the config hash."""

import hashlib
import json
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

SOURCES = ("product", "gmail", "zendesk", "salesforce", "stripe")
STAGES = {"world": 1, "events": 2, "defects": 3}
TERM_DAYS = {"monthly": 30, "quarterly": 91, "annual": 365, "biennial": 730}
LABEL_WINDOW_DAYS = 30
HORIZON_DAYS = 90


@dataclass(frozen=True)
class OrgSpec:
    slug: str
    display_name: str
    accounts: int
    segment_mix: tuple[float, float, float]  # smb, mid_market, enterprise
    # full, crm_billing, support_heavy, product_only, sparse
    profile_mix: tuple[float, float, float, float, float]
    term_mix: tuple[float, float, float, float]  # monthly, quarterly, annual, biennial
    event_share: float  # share of every source budget


DEFAULT_ORGS = (
    OrgSpec(
        "acme-saas",
        "Acme SaaS",
        24,
        (0.30, 0.55, 0.15),
        (0.40, 0.20, 0.25, 0.10, 0.05),
        (0.08, 0.22, 0.70, 0.00),
        0.48,
    ),
    OrgSpec(
        "northwind-cloud",
        "Northwind Cloud",
        16,
        (0.80, 0.17, 0.03),
        (0.10, 0.10, 0.10, 0.40, 0.30),
        (0.15, 0.25, 0.60, 0.00),
        0.32,
    ),
    OrgSpec(
        "globex-data",
        "Globex Data",
        10,
        (0.10, 0.30, 0.60),
        (0.30, 0.25, 0.15, 0.20, 0.10),
        (0.00, 0.10, 0.60, 0.30),
        0.20,
    ),
)


@dataclass(frozen=True)
class DefectRates:
    duplicate: float = 0.02
    revised: float = 0.005
    late_arrival: float = 0.04
    out_of_order_batches: float = 0.06
    missing: float = 0.03
    gmail_domain_mismatch: float = 0.02
    zendesk_name_variant: float = 0.05
    stripe_stale_mapping: float = 0.01
    product_timezone_less: float = 0.03
    salesforce_missing_account_id: float = 0.04


@dataclass(frozen=True)
class ChurnModel:
    # Intercepts tuned so the pooled per-renewal churn rate lands in annual_churn_range.
    intercept: dict[str, float] = field(
        default_factory=lambda: {
            "monthly": -3.0,
            "quarterly": -2.3,
            "annual": -2.0,
            "biennial": -1.8,
        }
    )
    b_health: float = 0.9
    b_seat_decline: float = 0.5
    noise_sd: float = 0.8
    health_drift_sd: float = 0.4
    quiet_share: float = 0.15
    false_alarm_base: float = 0.12


@dataclass(frozen=True)
class GenerationConfig:
    seed: int = 20261001
    anchor: datetime = datetime(2026, 10, 1, tzinfo=UTC)
    history_days: int = 1096
    total_events: int = 1_000
    source_share: dict[str, float] = field(
        default_factory=lambda: {
            "product": 0.45,
            "gmail": 0.15,
            "zendesk": 0.15,
            "salesforce": 0.15,
            "stripe": 0.10,
        }
    )
    orgs: tuple[OrgSpec, ...] = DEFAULT_ORGS
    defects: DefectRates = field(default_factory=DefectRates)
    churn: ChurnModel = field(default_factory=ChurnModel)
    churn_rate_range: tuple[float, float] = (0.12, 0.22)

    @property
    def history_start(self) -> datetime:
        return self.anchor - timedelta(days=self.history_days)

    @property
    def total_accounts(self) -> int:
        return sum(org.accounts for org in self.orgs)

    def source_budget(self, source: str) -> int:
        return round(self.total_events * self.source_share[source])

    def org_source_budget(self, org: OrgSpec, source: str) -> int:
        """Largest-remainder split so per-org budgets always sum to the source budget."""
        total = self.source_budget(source)
        raw = [total * o.event_share for o in self.orgs]
        floors = [int(x) for x in raw]
        order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
        for i in order[: total - sum(floors)]:
            floors[i] += 1
        return floors[self.orgs.index(org)]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["anchor"] = self.anchor.isoformat()
        return data

    def config_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()


def stage_rng(
    config: GenerationConfig, org: OrgSpec, stage: str, source: str | None = None
) -> np.random.Generator:
    """Independent stream per (seed, org, stage, source): resizing one org never changes another."""
    key = [config.seed, zlib.crc32(org.slug.encode()), STAGES[stage]]
    if source is not None:
        key.append(SOURCES.index(source) + 1)
    return np.random.default_rng(np.random.SeedSequence(key))
