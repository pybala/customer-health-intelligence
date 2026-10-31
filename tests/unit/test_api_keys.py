import pytest

from app.security.api_keys import hash_secret, issue_key, split_token, verify_secret

PEPPER = "unit-test-pepper-" + "y" * 32


def test_issued_key_has_prefix_and_secret_and_verifies() -> None:
    issued = issue_key(PEPPER)

    parsed = split_token(issued.plaintext)

    assert parsed == (issued.prefix, issued.plaintext.split(".", 1)[1])
    assert verify_secret(PEPPER, parsed[1], issued.key_hash)


def test_plaintext_secret_is_not_stored_in_hash() -> None:
    issued = issue_key(PEPPER)
    secret = issued.plaintext.split(".", 1)[1]

    assert secret not in issued.key_hash
    assert issued.key_hash != secret


def test_wrong_pepper_fails_verification() -> None:
    issued = issue_key(PEPPER)
    secret = issued.plaintext.split(".", 1)[1]

    assert not verify_secret("a-different-pepper-" + "z" * 32, secret, issued.key_hash)


def test_hash_is_keyed_by_pepper() -> None:
    assert hash_secret(PEPPER, "same") != hash_secret("other-" + "p" * 32, "same")


@pytest.mark.parametrize(
    "token",
    ["", "no-separator", "wrong_prefix.secret", "chi_prefixonly.", "chi_abc."],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert split_token(token) is None
