"""Development-token authentication for the local assessment and demo.

NOT PRODUCTION-READY. Tokens are stored in plaintext in a local, gitignored JSON file, are
not hashed, cannot be revoked individually, and identify a label, not a person. The
organisation always comes from the token file, never from the client.
"""

import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.security.principal import AuthenticationError, Principal

ROLES = frozenset({"csm", "admin", "readonly"})
DEV_VALUE_PREFIX = "dev_"
MIN_TOKEN_LENGTH = 24
# Fixed namespace so a token label always maps to the same audit identity.
AUDIT_NAMESPACE = uuid.UUID("5d0c3f7e-9a41-4a5e-8f0b-6c1d2e3f4a5b")


class DevTokenConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DevToken:
    label: str
    token: str
    org_id: uuid.UUID
    role: str


def audit_actor_id(label: str) -> uuid.UUID:
    """Stable UUID for a token label. It is derived from the label, never from the secret."""
    return uuid.uuid5(AUDIT_NAMESPACE, f"dev-token:{label}")


def generate_token() -> str:
    return DEV_VALUE_PREFIX + secrets.token_urlsafe(24)


def load_dev_tokens(path: Path) -> tuple[DevToken, ...]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise DevTokenConfigError(
            f"Dev token file {path} not found. Create it with: python -m app.cli init-dev-tokens"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DevTokenConfigError(f"Dev token file {path} is not valid JSON") from exc

    entries = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise DevTokenConfigError(f"Dev token file {path} must contain a non-empty 'tokens' list")

    tokens: list[DevToken] = []
    for index, entry in enumerate(entries):
        tokens.append(_parse_entry(index, entry))
    _reject_duplicates(tokens)
    return tuple(tokens)


def _parse_entry(index: int, entry: object) -> DevToken:
    if not isinstance(entry, dict):
        raise DevTokenConfigError(f"tokens[{index}] must be an object")
    label, token, org_id, role = (entry.get(k) for k in ("label", "token", "org_id", "role"))
    if not isinstance(label, str) or not label:
        raise DevTokenConfigError(f"tokens[{index}] needs a non-empty label")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise DevTokenConfigError(
            f"tokens[{index}] ({label}): token must be at least {MIN_TOKEN_LENGTH} characters"
        )
    if role not in ROLES:
        raise DevTokenConfigError(f"tokens[{index}] ({label}): role must be one of {sorted(ROLES)}")
    try:
        parsed_org = uuid.UUID(str(org_id))
    except ValueError as exc:
        raise DevTokenConfigError(f"tokens[{index}] ({label}): org_id is not a UUID") from exc
    return DevToken(label=label, token=token, org_id=parsed_org, role=role)


def _reject_duplicates(tokens: list[DevToken]) -> None:
    labels = [t.label for t in tokens]
    secrets_seen = [t.token for t in tokens]
    if len(set(labels)) != len(labels):
        raise DevTokenConfigError("Dev token labels must be unique")
    if len(set(secrets_seen)) != len(secrets_seen):
        raise DevTokenConfigError("Dev tokens must be unique")


def authenticate_dev_token(tokens: tuple[DevToken, ...], presented: str) -> Principal:
    # Compare against every entry so timing does not reveal which token matched.
    match: DevToken | None = None
    for candidate in tokens:
        if hmac.compare_digest(candidate.token.encode(), presented.encode()):
            match = candidate
    if match is None:
        raise AuthenticationError
    return Principal(
        org_id=match.org_id,
        actor_id=audit_actor_id(match.label),
        role=match.role,
    )
