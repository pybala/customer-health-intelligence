from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, text

from app.security.api_keys import split_token, verify_secret


class AuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    org_id: UUID
    actor_id: UUID  # api_keys.id, or a UUID derived from a dev-token label; goes to audit_events
    role: str


def authenticate(engine: Engine, pepper: str, token: str) -> Principal:
    parts = split_token(token)
    if parts is None:
        raise AuthenticationError
    prefix, secret = parts

    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.api_key_prefix', :prefix, true)"), {"prefix": prefix}
        )
        row = (
            conn.execute(
                text(
                    "SELECT id, org_id, key_hash, role, revoked_at "
                    "FROM api_keys WHERE key_prefix = :prefix"
                ),
                {"prefix": prefix},
            )
            .mappings()
            .first()
        )
        if row is None or row["revoked_at"] is not None:
            raise AuthenticationError
        if not verify_secret(pepper, secret, row["key_hash"]):
            raise AuthenticationError

        conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(row["org_id"])},
        )
        conn.execute(
            text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"),
            {"id": row["id"]},
        )

    return Principal(org_id=row["org_id"], actor_id=row["id"], role=row["role"])
