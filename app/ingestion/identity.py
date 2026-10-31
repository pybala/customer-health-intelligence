"""Identity resolution for one organisation. Pure logic over an in-memory index.

Order: exact source key, then domain, then fuzzy name. A confident match yields `matched`.
A conflict between two signals yields `ambiguous`. No usable match yields `unmatched`.
Ambiguous and unmatched records keep their event and are never guessed into an account.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from uuid import UUID

from app.ingestion.adapters.base import IdentityHints

FUZZY_THRESHOLD = 0.88
FUZZY_MARGIN = 0.05
LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "group",
    "grp",
    "co",
    "company",
    "corp",
}
FREEMAIL_DOMAINS = {"freemail.example.test"}


def normalise_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str  # matched | unmatched | ambiguous
    account_id: UUID | None
    method: str | None
    confidence: float
    reason: str | None = None


@dataclass
class IdentityIndex:
    exact: dict[tuple[str, str], UUID] = field(default_factory=dict)
    domains: dict[str, UUID] = field(default_factory=dict)
    names: dict[str, UUID] = field(default_factory=dict)
    salesforce_owner: dict[str, UUID] = field(default_factory=dict)

    def add(self, source: str, key: str, account_id: UUID, method: str) -> None:
        if method == "domain":
            self.domains[key.lower()] = account_id
        else:
            self.exact[(source, key)] = account_id
        if source == "salesforce" and method == "exact_id":
            self.salesforce_owner[key] = account_id

    def add_name(self, canonical_name: str, account_id: UUID) -> None:
        self.names[normalise_name(canonical_name)] = account_id


def _fuzzy(index: IdentityIndex, name: str) -> tuple[UUID | None, float, bool]:
    target = normalise_name(name)
    if target in index.names:
        return index.names[target], 1.0, False
    scored = sorted(
        ((SequenceMatcher(None, target, known).ratio(), acc) for known, acc in index.names.items()),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] < FUZZY_THRESHOLD:
        return None, scored[0][0] if scored else 0.0, False
    if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_MARGIN:
        return None, scored[0][0], True
    return scored[0][1], scored[0][0], False


def resolve(index: IdentityIndex, source: str, hints: IdentityHints) -> Resolution:
    if hints.exact_key is not None:
        account = index.exact.get((source, hints.exact_key))
        if account is not None:
            conflict = _cross_ref_conflict(index, account, hints)
            if conflict:
                return Resolution("ambiguous", None, None, 0.0, conflict)
            return Resolution("matched", account, "exact_id", 1.0)

    if hints.domain is not None and hints.domain not in FREEMAIL_DOMAINS:
        account = index.domains.get(hints.domain)
        if account is not None:
            return Resolution("matched", account, "domain", 0.95)
        return Resolution("unmatched", None, None, 0.0, f"unknown domain {hints.domain}")

    if hints.domain in FREEMAIL_DOMAINS:
        return Resolution("ambiguous", None, None, 0.0, "personal or shared mail domain")

    if hints.name is not None:
        account, score, tie = _fuzzy(index, hints.name)
        if tie:
            return Resolution("ambiguous", None, None, score, "name matches several accounts")
        if account is not None:
            return Resolution("matched", account, "fuzzy_name", round(score, 3))
        return Resolution("unmatched", None, None, round(score, 3), "name below threshold")

    return Resolution("unmatched", None, None, 0.0, "no identity signal")


def _cross_ref_conflict(index: IdentityIndex, account: UUID, hints: IdentityHints) -> str | None:
    if hints.cross_ref is None or hints.cross_ref_source != "salesforce":
        return None
    owner = index.salesforce_owner.get(hints.cross_ref)
    if owner is not None and owner != account:
        return "source key and CRM reference point to different accounts"
    return None
