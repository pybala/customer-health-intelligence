"""Delivery adapters. The only adapter is a local simulator; nothing is sent over any network."""

from collections.abc import Callable

from app.outbox.store import ClaimedMessage

Deliverer = Callable[[ClaimedMessage], None]


def simulate_send(message: ClaimedMessage) -> None:
    """Accept the message as delivered. The outbox row is the only record of the send."""
    return None
