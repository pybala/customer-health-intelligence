from datetime import UTC, datetime, timedelta

from app.outbox.retry import BASE_BACKOFF, MAX_BACKOFF, backoff_until, failure_outcome, lock_expired

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_failure_below_maximum_returns_to_pending() -> None:
    assert failure_outcome(attempts=1, max_attempts=5) == "pending"


def test_failure_at_maximum_dead_letters() -> None:
    assert failure_outcome(attempts=5, max_attempts=5) == "dead_letter"


def test_backoff_grows_exponentially_and_is_capped() -> None:
    assert backoff_until(NOW, 1) == NOW + BASE_BACKOFF
    assert backoff_until(NOW, 2) == NOW + BASE_BACKOFF * 2
    assert backoff_until(NOW, 3) == NOW + BASE_BACKOFF * 4
    assert backoff_until(NOW, 20) == NOW + MAX_BACKOFF


def test_lock_expiry_is_inclusive_of_the_deadline() -> None:
    assert lock_expired(NOW, NOW) is True
    assert lock_expired(NOW + timedelta(seconds=1), NOW) is False
    assert lock_expired(None, NOW) is False
