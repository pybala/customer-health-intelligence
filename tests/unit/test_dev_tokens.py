import json
import uuid
from pathlib import Path

import pytest

from app.security.dev_tokens import (
    DevTokenConfigError,
    audit_actor_id,
    authenticate_dev_token,
    generate_token,
    load_dev_tokens,
)
from app.security.principal import AuthenticationError

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()
TOKEN_A = "dev_" + "a" * 30
TOKEN_B = "dev_" + "b" * 30


def _entry(label: str, token: str, org: uuid.UUID, role: str = "csm") -> dict[str, str]:
    return {"label": label, "token": token, "org_id": str(org), "role": role}


def _write(tmp_path: Path, tokens: object) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"tokens": tokens}))
    return path


def test_valid_file_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, [_entry("a", TOKEN_A, ORG_A), _entry("b", TOKEN_B, ORG_B, "readonly")])

    tokens = load_dev_tokens(path)

    assert [(t.label, t.org_id, t.role) for t in tokens] == [
        ("a", ORG_A, "csm"),
        ("b", ORG_B, "readonly"),
    ]


def test_token_resolves_to_its_own_organisation_and_role(tmp_path: Path) -> None:
    tokens = load_dev_tokens(
        _write(tmp_path, [_entry("a", TOKEN_A, ORG_A, "admin"), _entry("b", TOKEN_B, ORG_B)])
    )

    principal = authenticate_dev_token(tokens, TOKEN_B)

    assert principal.org_id == ORG_B
    assert principal.role == "csm"


@pytest.mark.parametrize(
    "presented", ["", "dev_", TOKEN_A + "x", TOKEN_A[:-1], "Bearer " + TOKEN_A]
)
def test_wrong_token_is_rejected(tmp_path: Path, presented: str) -> None:
    tokens = load_dev_tokens(_write(tmp_path, [_entry("a", TOKEN_A, ORG_A)]))

    with pytest.raises(AuthenticationError):
        authenticate_dev_token(tokens, presented)


def test_audit_actor_is_stable_and_derived_from_label_not_secret(tmp_path: Path) -> None:
    first = authenticate_dev_token(
        load_dev_tokens(_write(tmp_path, [_entry("acme-csm", TOKEN_A, ORG_A)])), TOKEN_A
    )
    rotated = authenticate_dev_token(
        load_dev_tokens(_write(tmp_path, [_entry("acme-csm", TOKEN_B, ORG_A)])), TOKEN_B
    )
    other_label = audit_actor_id("globex-csm")

    assert first.actor_id == rotated.actor_id == audit_actor_id("acme-csm")
    assert first.actor_id != other_label


def test_audit_actor_does_not_contain_the_secret() -> None:
    assert TOKEN_A not in str(audit_actor_id("acme-csm"))


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        ([], "non-empty"),
        ([_entry("a", "short", ORG_A)], "at least"),
        ([_entry("a", TOKEN_A, ORG_A, "owner")], "role"),
        ([{**_entry("a", TOKEN_A, ORG_A), "org_id": "not-a-uuid"}], "UUID"),
        ([{**_entry("a", TOKEN_A, ORG_A), "label": ""}], "label"),
        ([_entry("a", TOKEN_A, ORG_A), _entry("a", TOKEN_B, ORG_B)], "labels must be unique"),
        ([_entry("a", TOKEN_A, ORG_A), _entry("b", TOKEN_A, ORG_B)], "tokens must be unique"),
    ],
)
def test_invalid_file_contents_are_rejected(
    tmp_path: Path, tokens: list[dict[str, str]], message: str
) -> None:
    with pytest.raises(DevTokenConfigError, match=message):
        load_dev_tokens(_write(tmp_path, tokens))


def test_missing_and_malformed_files_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DevTokenConfigError, match="not found"):
        load_dev_tokens(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(DevTokenConfigError, match="not valid JSON"):
        load_dev_tokens(bad)


def test_generated_tokens_are_unique_and_long_enough() -> None:
    first, second = generate_token(), generate_token()

    assert first != second
    assert first.startswith("dev_")
    assert len(first) >= 24
