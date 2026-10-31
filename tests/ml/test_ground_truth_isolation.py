"""Ground truth stays out of every runtime path. Only offline train and evaluate code may read it.

The scan is static: it parses each module and fails on any reference to the ground-truth file,
the generator package, or the defect log. Offline CLI code is the only exception.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PACKAGES = ("app/ingestion", "app/features", "app/api", "app/worker", "app/dashboard")
OFFLINE_MODULES = {"app/cli.py", "app/ml/train.py", "app/ml/evaluate.py"}
FORBIDDEN_TEXT = ("ground_truth", "defects.ndjson", "defects")
FORBIDDEN_MODULE_PARTS = ("generators",)


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for package in RUNTIME_PACKAGES:
        base = ROOT / package
        if base.exists():
            files.extend(
                p
                for p in sorted(base.rglob("*.py"))
                if "generators" not in p.relative_to(base).parts
            )
    return files


def _offences(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(word in node.value for word in FORBIDDEN_TEXT)
        ):
            found.append(f"{rel}:{node.lineno} string {node.value!r}")
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and any(part in node.module.split(".") for part in FORBIDDEN_MODULE_PARTS)
        ):
            found.append(f"{rel}:{node.lineno} imports {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(part in alias.name.split(".") for part in FORBIDDEN_MODULE_PARTS):
                    found.append(f"{rel}:{node.lineno} imports {alias.name}")
    return found


def test_runtime_packages_never_reference_ground_truth_or_generators() -> None:
    offences: list[str] = []
    for path in _runtime_files():
        offences.extend(_offences(path))
    assert offences == []


def test_only_offline_modules_mention_ground_truth_outside_the_generators() -> None:
    readers = []
    for path in (ROOT / "app").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in OFFLINE_MODULES or rel.startswith("app/ingestion/generators/"):
            continue
        if "ground_truth" in path.read_text(encoding="utf-8"):
            readers.append(rel)
    assert readers == [], f"unexpected ground-truth readers: {readers}"


def test_seed_path_does_not_hash_ground_truth_or_defect_files() -> None:
    from app.ingestion.canonical import RUNTIME_FILE_SUFFIXES

    assert not any(s.startswith(("ground_truth", "defects")) for s in RUNTIME_FILE_SUFFIXES)
