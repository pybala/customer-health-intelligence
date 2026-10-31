import hashlib
import hmac
import secrets
from dataclasses import dataclass

PREFIX_BYTES = 9
SECRET_BYTES = 32
KEY_SCHEME = "chi"


@dataclass(frozen=True)
class IssuedKey:
    plaintext: str
    prefix: str
    key_hash: str


def hash_secret(pepper: str, secret: str) -> str:
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def issue_key(pepper: str) -> IssuedKey:
    prefix = f"{KEY_SCHEME}_{secrets.token_urlsafe(PREFIX_BYTES)}"
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return IssuedKey(
        plaintext=f"{prefix}.{secret}",
        prefix=prefix,
        key_hash=hash_secret(pepper, secret),
    )


def split_token(token: str) -> tuple[str, str] | None:
    prefix, sep, secret = token.partition(".")
    if not sep or not prefix.startswith(f"{KEY_SCHEME}_") or not secret:
        return None
    return prefix, secret


def verify_secret(pepper: str, secret: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(pepper, secret), expected_hash)
