"""Retry and stale-lock rules for outbox messages. The database enforces the transitions."""

from datetime import datetime, timedelta
from typing import Literal

BASE_BACKOFF = timedelta(minutes=5)
MAX_BACKOFF = timedelta(hours=1)


def failure_outcome(attempts: int, max_attempts: int) -> Literal["pending", "dead_letter"]:
    return "pending" if attempts < max_attempts else "dead_letter"


def backoff_until(now: datetime, attempts: int) -> datetime:
    delay: timedelta = min(BASE_BACKOFF * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)
    until: datetime = now + delay
    return until


def lock_expired(locked_until: datetime | None, now: datetime) -> bool:
    return locked_until is not None and locked_until <= now
