"""Suppression rules applied before any outreach is proposed and again before any send.

Legal hold always suppresses. An open critical escalation suppresses automated outreach. A
successful contact suppresses repeat contact for the cooldown window. The send-time check
matters because a hold or escalation can be recorded after a human has approved an action.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

CONTACT_COOLDOWN = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class SuppressionFacts:
    legal_hold: bool
    critical_escalation_open: bool
    last_successful_contact_at: datetime | None


def suppression_reason(facts: SuppressionFacts, now: datetime) -> str | None:
    if facts.legal_hold:
        return "legal_hold"
    if facts.critical_escalation_open:
        return "critical_escalation"
    last = facts.last_successful_contact_at
    if last is not None and now - last < CONTACT_COOLDOWN:
        return "recent_contact"
    return None
