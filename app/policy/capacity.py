"""CSM review capacity. The fraction is illustrative, not a validated precision claim."""

import math


def review_capacity(eligible_accounts: int, fraction: float) -> int:
    """Accounts a CSM team can review per run. Rounds up so a small tenant keeps one slot."""
    if eligible_accounts <= 0:
        return 0
    return math.ceil(eligible_accounts * fraction)
