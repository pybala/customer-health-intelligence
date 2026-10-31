"""Latent world: accounts, contract terms, latent health and renewal outcomes.

Outcomes are drawn here and nowhere else. Event generators only read the signal health
trajectories, so the platform has to infer risk from observable behaviour.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.ingestion.generators.config import (
    LABEL_WINDOW_DAYS,
    TERM_DAYS,
    GenerationConfig,
    OrgSpec,
    stage_rng,
)

SEGMENTS = ("smb", "mid_market", "enterprise")
PROFILES = ("full", "crm_billing", "support_heavy", "product_only", "sparse")
TERMS = ("monthly", "quarterly", "annual", "biennial")
REGIONS = ("na", "emea", "apac", "latam")
REGION_WEIGHTS = (0.45, 0.30, 0.15, 0.10)
REGION_TZ = {
    "na": ("America/New_York", "America/Chicago", "America/Los_Angeles"),
    "emea": ("Europe/London", "Europe/Berlin"),
    "apac": ("Asia/Singapore", "Australia/Sydney"),
    "latam": ("America/Sao_Paulo",),
}
PLAN_BY_SEGMENT = {
    "smb": (("starter", "team"), (0.7, 0.3)),
    "mid_market": (("team", "business"), (0.5, 0.5)),
    "enterprise": (("business", "enterprise"), (0.4, 0.6)),
}
PRICE_PER_SEAT = {"starter": 12.0, "team": 25.0, "business": 45.0, "enterprise": 70.0}
SEAT_MEDIAN = {"smb": 12.0, "mid_market": 80.0, "enterprise": 600.0}
ACTIVITY_MULTIPLIER = {"smb": 1.0, "mid_market": 3.0, "enterprise": 8.0}
HEALTH_MEAN = {"smb": -0.10, "mid_market": 0.0, "enterprise": 0.15}
PROFILE_SOURCES = {
    "full": ("product", "gmail", "zendesk", "salesforce", "stripe"),
    "crm_billing": ("salesforce", "stripe", "product"),
    "support_heavy": ("zendesk", "gmail", "stripe"),
    "product_only": ("product", "stripe"),
}
SPARSE_EXTRAS = (None, "product", "gmail", "zendesk", "salesforce")

ADJECTIVES = (
    "Amber",
    "Brisk",
    "Cobalt",
    "Dawn",
    "Ember",
    "Fjord",
    "Granite",
    "Harbor",
    "Indigo",
    "Juniper",
    "Kestrel",
    "Lumen",
    "Meridian",
    "Nimbus",
    "Onyx",
    "Pioneer",
    "Quartz",
    "Raven",
    "Summit",
    "Tundra",
    "Umber",
    "Vertex",
    "Willow",
    "Zephyr",
    "Alder",
    "Basalt",
    "Cedar",
    "Delta",
    "Echo",
    "Flint",
    "Glacier",
    "Hollow",
    "Iris",
    "Jade",
    "Krypton",
    "Lagoon",
    "Maple",
    "Nova",
    "Orchid",
    "Prairie",
)
NOUNS = (
    "Systems",
    "Labs",
    "Works",
    "Dynamics",
    "Analytics",
    "Networks",
    "Logistics",
    "Robotics",
    "Media",
    "Health",
    "Energy",
    "Capital",
    "Software",
    "Materials",
    "Foods",
    "Mobility",
    "Security",
    "Learning",
    "Retail",
    "Cloudware",
)
PREFIXES = ("", "Applied", "United", "Digital")
SUFFIXES = ("Inc", "LLC", "Ltd", "Group", "Co")
ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class Term:
    index: int
    start: datetime
    renewal: datetime
    signal_start: float
    signal_end: float
    churned: bool
    cancel_at: datetime | None
    quiet: bool
    seat_decline: float
    true_p: float
    latent_health_end: float
    label_observable: bool


@dataclass
class AccountWorld:
    org_slug: str
    id: uuid.UUID
    name: str
    suffix: str
    domain: str
    segment: str
    plan_tier: str
    region: str
    timezone: str
    seats: int
    contract_value: float
    term_type: str
    contract_start: datetime
    profile: str
    sources: tuple[str, ...]
    salesforce_id: str
    zendesk_org_id: int
    product_key: str
    stripe_customer: str
    activity: float
    terms: list[Term] = field(default_factory=list)
    pending_renewal: datetime | None = None
    pending_signal: float = 0.0
    initial_signal: float = 0.0
    ended_at: datetime | None = None

    @property
    def active_from(self) -> datetime:
        return self.terms[0].start if self.terms else self.contract_start


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _token(rng: np.random.Generator, length: int) -> str:
    return "".join(ALNUM[i] for i in rng.integers(0, len(ALNUM), size=length))


def _choice[T](rng: np.random.Generator, options: tuple[T, ...], p: tuple[float, ...]) -> T:
    return options[int(rng.choice(len(options), p=np.asarray(p) / sum(p)))]


def _simulate_terms(
    config: GenerationConfig,
    rng: np.random.Generator,
    account: AccountWorld,
    h_start: float,
) -> None:
    churn = config.churn
    term_days = TERM_DAYS[account.term_type]
    scale = term_days / 365.0
    rho = 0.85**scale
    drift = churn.health_drift_sd * math.sqrt(scale)
    history_start, anchor = config.history_start, config.anchor

    k = max(1, math.ceil((history_start - account.contract_start).days / term_days) + 1)
    while True:
        start = account.contract_start + timedelta(days=(k - 1) * term_days)
        renewal = account.contract_start + timedelta(days=k * term_days)
        if renewal > anchor:
            account.pending_renewal = renewal
            account.pending_signal = h_start
            return
        h_end = rho * h_start + drift * float(rng.standard_normal())
        decline = max(0.0, -0.08 * h_end + 0.06 * float(rng.standard_normal()))
        logit = (
            churn.intercept[account.term_type]
            - churn.b_health * h_end
            + churn.b_seat_decline * decline * 10
            + churn.noise_sd * float(rng.standard_normal())
        )
        p = _sigmoid(logit)
        churned = bool(rng.random() < p)
        quiet = churned and bool(rng.random() < churn.quiet_share)

        signal_start = max(h_start, 0.2) if quiet else h_start
        signal_end = h_end
        if not churned and rng.random() < churn.false_alarm_base:
            signal_end = h_end - 1.5

        cancel_at: datetime | None = None
        if churned:
            cancel_at = renewal + timedelta(days=int(rng.integers(0, LABEL_WINDOW_DAYS + 1)))
        account.terms.append(
            Term(
                index=k,
                start=start,
                renewal=renewal,
                signal_start=signal_start,
                signal_end=signal_end,
                churned=churned,
                cancel_at=cancel_at,
                quiet=quiet,
                seat_decline=decline,
                true_p=p,
                latent_health_end=h_end,
                label_observable=renewal + timedelta(days=LABEL_WINDOW_DAYS) <= anchor,
            )
        )
        if churned and cancel_at is not None:
            account.ended_at = cancel_at
            if cancel_at > anchor:
                account.ended_at = None
            return
        h_start = h_end
        k += 1


def generate_world(config: GenerationConfig, org: OrgSpec) -> list[AccountWorld]:
    rng = stage_rng(config, org, "world")
    base = len(ADJECTIVES) * len(NOUNS)
    if org.accounts > base * len(PREFIXES):
        raise ValueError("name space too small for the requested account count")
    names = rng.permutation(base * len(PREFIXES))[: org.accounts]
    accounts: list[AccountWorld] = []
    for i in range(org.accounts):
        segment = _choice(rng, SEGMENTS, org.segment_mix)
        tiers, tier_p = PLAN_BY_SEGMENT[segment]
        tier = _choice(rng, tiers, tier_p)
        region = _choice(rng, REGIONS, REGION_WEIGHTS)
        zones = REGION_TZ[region]
        seats = max(2, round(SEAT_MEDIAN[segment] * float(rng.lognormal(0.0, 0.6))))
        term_type = _choice(rng, TERMS, org.term_mix)
        profile = _choice(rng, PROFILES, org.profile_mix)
        if profile == "sparse":
            extra = SPARSE_EXTRAS[int(rng.integers(0, len(SPARSE_EXTRAS)))]
            sources: tuple[str, ...] = ("stripe",) if extra is None else ("stripe", extra)
        else:
            sources = PROFILE_SOURCES[profile]
        n = int(names[i])
        prefix, n = PREFIXES[n // base], n % base
        adjective, noun = ADJECTIVES[n // len(NOUNS)], NOUNS[n % len(NOUNS)]
        display = f"{prefix} {adjective} {noun}".strip()
        slug = display.replace(" ", "").lower()
        offset_days = float(rng.uniform(-700, (config.anchor - config.history_start).days - 20))
        account = AccountWorld(
            org_slug=org.slug,
            id=uuid.UUID(bytes=rng.bytes(16), version=4),
            name=display,
            suffix=SUFFIXES[int(rng.integers(0, len(SUFFIXES)))],
            domain=f"{slug}.example.test",
            segment=segment,
            plan_tier=tier,
            region=region,
            timezone=zones[int(rng.integers(0, len(zones)))],
            seats=seats,
            contract_value=round(
                seats * PRICE_PER_SEAT[tier] * 12 * float(rng.uniform(0.7, 1.0)), 2
            ),
            term_type=term_type,
            contract_start=config.history_start + timedelta(days=offset_days),
            profile=profile,
            sources=sources,
            salesforce_id="001" + _token(rng, 15),
            zendesk_org_id=int(rng.integers(10**10, 10**11)),
            product_key="acct_" + _token(rng, 10).lower(),
            stripe_customer="cus_" + _token(rng, 14),
            activity=ACTIVITY_MULTIPLIER[segment] * float(rng.lognormal(0.0, 1.0)),
        )
        h0 = HEALTH_MEAN[segment] + float(rng.standard_normal())
        account.initial_signal = h0
        _simulate_terms(config, rng, account, h0)
        accounts.append(account)
    return accounts
