import json
import stat
import uuid
from pathlib import Path

import pytest

from app.cli import main
from app.security.dev_tokens import load_dev_tokens

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()


@pytest.fixture
def tokens_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "dev_tokens.json"
    monkeypatch.setenv("DEV_TOKENS_FILE", str(path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://chi_app:pw@localhost:5432/chi")
    monkeypatch.setenv("API_KEY_PEPPER", "cli-test-pepper-" + "z" * 32)
    return path


def test_init_writes_loadable_owner_only_file(
    tokens_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "init-dev-tokens",
            "--token",
            f"acme-csm:{ORG_A}:csm",
            "--token",
            f"globex-ro:{ORG_B}:readonly",
        ]
    )

    tokens = load_dev_tokens(tokens_path)
    assert code == 0
    assert [(t.label, t.org_id, t.role) for t in tokens] == [
        ("acme-csm", ORG_A, "csm"),
        ("globex-ro", ORG_B, "readonly"),
    ]
    assert stat.S_IMODE(tokens_path.stat().st_mode) == 0o600
    assert all(t.token not in capsys.readouterr().out for t in tokens)


def test_init_refuses_to_overwrite_without_force(tokens_path: Path) -> None:
    assert main(["init-dev-tokens", "--token", f"a:{ORG_A}:csm"]) == 0
    before = tokens_path.read_text()

    assert main(["init-dev-tokens", "--token", f"b:{ORG_B}:csm"]) == 1
    assert tokens_path.read_text() == before

    assert main(["init-dev-tokens", "--force", "--token", f"b:{ORG_B}:csm"]) == 0
    assert json.loads(tokens_path.read_text())["tokens"][0]["label"] == "b"


@pytest.mark.parametrize(
    "spec", [f"a:{ORG_A}:owner", "a:not-a-uuid:csm", f":{ORG_A}:csm", "just-a-label"]
)
def test_init_rejects_bad_specs_and_writes_nothing(tokens_path: Path, spec: str) -> None:
    assert main(["init-dev-tokens", "--token", spec]) == 2
    assert not tokens_path.exists()
