"""Clean (defect-free) business events per source, driven by each account's health signal."""

import bisect
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from app.ingestion.generators.config import GenerationConfig, OrgSpec, stage_rng
from app.ingestion.generators.world import AccountWorld

# (event_type, relative weight, health sensitivity k). Acceptance is proportional to
# exp(k * health), so k > 0 means healthier accounts emit more of this type.
EVENT_TYPES: dict[str, tuple[tuple[str, float, float], ...]] = {
    "product": (
        ("login", 0.50, 0.55),
        ("feature_used", 0.30, 0.45),
        ("api_call_batch", 0.10, 0.35),
        ("export", 0.05, 0.15),
        ("seat_added", 0.05, 0.60),
    ),
    "gmail": (
        ("thread_sent", 0.45, 0.20),
        ("thread_received", 0.40, 0.30),
        ("meeting_invite", 0.15, 0.40),
    ),
    "zendesk": (
        ("ticket_opened", 0.40, -0.40),
        ("ticket_solved", 0.30, -0.15),
        ("csat_response", 0.20, 0.10),
        ("escalation", 0.10, -0.70),
    ),
    "salesforce": (
        ("opportunity_stage_change", 0.30, 0.10),
        ("activity_logged", 0.40, 0.25),
        ("contact_role_change", 0.10, -0.10),
        ("renewal_forecast_update", 0.20, 0.00),
    ),
    "stripe": (
        ("invoice_paid", 0.80, 0.05),
        ("invoice_failed", 0.10, -0.80),
        ("subscription_updated", 0.07, 0.10),
        ("payment_method_expired", 0.03, -0.60),
    ),
}
H_CLIP = 3.0
STAGES_POSITIVE = ("Prospecting", "Qualification", "Proposal", "Negotiation", "Closed Won")
STAGES_NEGATIVE = ("Closed Lost", "Negotiation", "Qualification")


@dataclass(slots=True)
class CleanEvent:
    org_slug: str
    source: str
    seq: int
    event_type: str
    account_index: int
    event_time: datetime
    attributes: dict[str, object] = field(default_factory=dict)


class SignalTrack:
    """Health signal over time for one account, piecewise linear within each term."""

    def __init__(self, account: AccountWorld, config: GenerationConfig) -> None:
        self.starts: list[float] = []
        self.ends: list[float] = []
        self.s0: list[float] = []
        self.s1: list[float] = []
        self.quiet: list[bool] = []
        for term in account.terms:
            self._add(term.start, term.renewal, term.signal_start, term.signal_end, term.quiet)
        if account.pending_renewal is not None:
            last_end = account.terms[-1].renewal if account.terms else account.contract_start
            self._add(
                last_end,
                account.pending_renewal,
                account.pending_signal,
                account.pending_signal,
                False,
            )
        if not self.starts:
            self._add(
                account.contract_start,
                config.anchor,
                account.initial_signal,
                account.initial_signal,
                False,
            )
        self.first = self.starts[0]

    def _add(self, start: datetime, end: datetime, s0: float, s1: float, quiet: bool) -> None:
        self.starts.append(start.timestamp())
        self.ends.append(end.timestamp())
        self.s0.append(s0)
        self.s1.append(s1)
        self.quiet.append(quiet)

    def at(self, ts: float) -> float:
        i = max(0, bisect.bisect_right(self.starts, ts) - 1)
        span = max(self.ends[i] - self.starts[i], 1.0)
        frac = min(max((ts - self.starts[i]) / span, 0.0), 1.0)
        if self.quiet[i]:
            frac = frac**4
        return self.s0[i] + (self.s1[i] - self.s0[i]) * frac


def active_interval(account: AccountWorld, config: GenerationConfig) -> tuple[float, float]:
    start = max(config.history_start, account.active_from).timestamp()
    end = (account.ended_at or config.anchor).timestamp()
    return start, min(end, config.anchor.timestamp())


def _attributes(
    source: str,
    event_type: str,
    account: AccountWorld,
    health: float,
    rng: np.random.Generator,
) -> dict[str, object]:
    if source == "product":
        users = max(1, int(account.seats * rng.uniform(0.2, 0.9)))
        attrs: dict[str, object] = {"user": f"u{int(rng.integers(1, users + 1)):05d}"}
        if event_type == "api_call_batch":
            attrs["calls"] = int(rng.lognormal(4.0 + 0.2 * health, 0.8))
        if event_type == "seat_added":
            attrs["seats_delta"] = int(rng.integers(1, 6))
        return attrs
    if source == "gmail":
        return {
            "thread": f"t{int(rng.integers(10**8, 10**9))}",
            "participants": int(rng.integers(2, 7)),
            "exec_contact": bool(rng.random() < 1.0 / (1.0 + math.exp(-(health - 0.3)))),
        }
    if source == "zendesk":
        attrs = {"ticket": int(rng.integers(10**6, 10**7))}
        if event_type == "csat_response":
            attrs["csat"] = int(np.clip(round(3.6 + 0.6 * health + rng.normal(0, 0.9)), 1, 5))
        if event_type == "ticket_solved":
            attrs["resolution_hours"] = round(float(rng.lognormal(2.8 - 0.1 * health, 0.7)), 1)
        return attrs
    if source == "salesforce":
        if event_type == "opportunity_stage_change":
            pool = STAGES_POSITIVE if health + rng.normal(0, 0.8) > -0.2 else STAGES_NEGATIVE
            return {"stage": pool[int(rng.integers(0, len(pool)))]}
        if event_type == "renewal_forecast_update":
            pct = round(70 + 12 * health + rng.normal(0, 10))
            return {"forecast_pct": int(np.clip(pct, 0, 100))}
        return {}
    if event_type in {"invoice_paid", "invoice_failed"}:
        return {"amount_minor": int(account.contract_value / 12 * 100 * rng.uniform(0.9, 1.1))}
    if event_type == "subscription_updated":
        return {"seats": account.seats}
    return {}


def generate_clean_events(
    config: GenerationConfig,
    org: OrgSpec,
    accounts: list[AccountWorld],
    source: str,
    count: int,
) -> list[CleanEvent]:
    """Exactly `count` clean events for one source. Candidates are thinned by health."""
    rng = stage_rng(config, org, "events", source)
    eligible = [i for i, a in enumerate(accounts) if source in a.sources]
    if not eligible or count == 0:
        return []
    tracks = {i: SignalTrack(accounts[i], config) for i in eligible}
    intervals = {i: active_interval(accounts[i], config) for i in eligible}
    weights = np.array(
        [
            max(accounts[i].activity * max(intervals[i][1] - intervals[i][0], 0.0), 1e-9)
            for i in eligible
        ]
    )
    weights /= weights.sum()
    starts = np.array([intervals[i][0] for i in eligible])
    ends = np.array([intervals[i][1] for i in eligible])

    types = EVENT_TYPES[source]
    type_names = [t[0] for t in types]
    type_p = np.array([t[1] for t in types])
    type_p /= type_p.sum()
    type_k = np.array([t[2] for t in types])

    accepted: list[CleanEvent] = []
    seq = 0
    while len(accepted) < count:
        n = max(1000, (count - len(accepted)) * 4)
        who = rng.choice(len(eligible), size=n, p=weights)
        kind = rng.choice(len(types), size=n, p=type_p)
        ts = starts[who] + rng.random(n) * (ends[who] - starts[who])
        u = rng.random(n)
        for j in range(n):
            if len(accepted) >= count:
                break
            idx = eligible[int(who[j])]
            health = float(np.clip(tracks[idx].at(float(ts[j])), -H_CLIP, H_CLIP))
            k = float(type_k[kind[j]])
            if u[j] > math.exp(k * health - abs(k) * H_CLIP):
                continue
            event_type = type_names[int(kind[j])]
            accepted.append(
                CleanEvent(
                    org_slug=org.slug,
                    source=source,
                    seq=seq,
                    event_type=event_type,
                    account_index=idx,
                    event_time=datetime.fromtimestamp(float(ts[j]), UTC).replace(microsecond=0),
                    attributes=_attributes(source, event_type, accounts[idx], health, rng),
                )
            )
            seq += 1
    accepted.sort(key=lambda e: (e.event_time, e.seq))
    for new_seq, event in enumerate(accepted):
        event.seq = new_seq
    return accepted
