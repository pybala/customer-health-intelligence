"""Offline training examples: one per (account, weekly cutoff) with an observable renewal outcome.

Outcomes are supplied by the offline workflow (app/ml/train.py). Features come from the same
point-in-time builder the runtime uses, so the visibility rule is shared.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine

from app.features.builder import FeatureSnapshot, load_snapshot

HORIZON = timedelta(days=90)
WEEK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class RenewalOutcome:
    org_slug: str
    org_id: UUID
    account_id: UUID
    renewal: datetime
    churned: bool
    label_observable: bool


@dataclass(frozen=True, slots=True)
class Example:
    org_slug: str
    org_id: UUID
    account_id: UUID
    cutoff: datetime
    renewal: datetime
    label: int
    snapshot: FeatureSnapshot


def weekly_cutoffs(start: datetime, end: datetime) -> list[datetime]:
    cutoffs: list[datetime] = []
    current = start
    while current <= end:
        cutoffs.append(current)
        current += WEEK
    return cutoffs


def build_examples(
    engine: Engine, outcomes: Sequence[RenewalOutcome], cutoffs: Sequence[datetime]
) -> list[Example]:
    """Pair each observable outcome with each weekly cutoff whose 90-day horizon holds its renewal.

    A renewal counts at cutoff c when it falls in (c, c + 90 days]. Outcomes that are not yet
    observable are skipped, never counted as a non-churn.
    """
    examples: list[Example] = []
    for outcome in outcomes:
        if not outcome.label_observable:
            continue
        for cutoff in cutoffs:
            if not (cutoff < outcome.renewal <= cutoff + HORIZON):
                continue
            snapshot = load_snapshot(engine, outcome.org_id, outcome.account_id, cutoff)
            examples.append(
                Example(
                    org_slug=outcome.org_slug,
                    org_id=outcome.org_id,
                    account_id=outcome.account_id,
                    cutoff=cutoff,
                    renewal=outcome.renewal,
                    label=int(outcome.churned),
                    snapshot=snapshot,
                )
            )
    return examples
