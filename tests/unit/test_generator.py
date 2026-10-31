import ast
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.ingestion.canonical import FIXTURE_DIR
from app.ingestion.generators.config import GenerationConfig
from app.ingestion.generators.generate import (
    GenerationValidationError,
    generate,
    validate,
)

APP_INGESTION = Path(__file__).resolve().parents[2] / "app" / "ingestion"
SOURCES = ("product", "gmail", "zendesk", "salesforce", "stripe")
CONFIG = GenerationConfig()


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "manifest.json").read_text())  # type: ignore[no-any-return]


def test_canonical_counts_are_exact() -> None:
    manifest = _manifest(FIXTURE_DIR)

    assert sum(o["accounts"] for o in manifest["orgs"].values()) == 50  # type: ignore[index, misc]
    assert manifest["total_events"] == 1000


def test_per_source_counts_match_the_budget() -> None:
    manifest = _manifest(FIXTURE_DIR)
    orgs = manifest["orgs"].values()  # type: ignore[attr-defined]
    for source in SOURCES:
        emitted = sum(o["events"][source] for o in orgs)
        assert emitted == CONFIG.source_budget(source), source


def test_fixture_matches_a_fresh_generation_byte_for_byte() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        fresh = generate(CONFIG, Path(scratch))

        assert _manifest(fresh)["files"] == _manifest(FIXTURE_DIR)["files"]


def test_one_org_is_unchanged_when_the_other_is_resized() -> None:
    resized_orgs = (
        replace(CONFIG.orgs[0], accounts=CONFIG.orgs[0].accounts + 1),
        *CONFIG.orgs[1:],
    )
    with tempfile.TemporaryDirectory() as scratch:
        other = generate(replace(CONFIG, orgs=resized_orgs), Path(scratch))

        base = _manifest(FIXTURE_DIR)["files"]
        changed = _manifest(other)["files"]
        slug = CONFIG.orgs[1].slug
        assert base[f"{slug}/accounts.ndjson"] == changed[f"{slug}/accounts.ndjson"]  # type: ignore[index]


def test_different_seed_changes_the_data() -> None:
    other = GenerationConfig(seed=CONFIG.seed + 1, churn_rate_range=(0.0, 1.0))
    with tempfile.TemporaryDirectory() as scratch:
        changed = generate(other, Path(scratch))

        assert _manifest(changed)["files"] != _manifest(FIXTURE_DIR)["files"]


def test_achieved_defect_rates_are_within_tolerance_in_the_manifest() -> None:
    rows = _manifest(FIXTURE_DIR)["defect_rates"]
    assert rows
    for row in rows:  # type: ignore[attr-defined]
        assert row["denominator"] > 0, row
        assert row["within_tolerance"] is True, row
        assert abs(row["achieved_rate"] - row["configured_rate"]) <= row["tolerance"], row


def test_each_org_has_both_classes_in_the_fixture() -> None:
    manifest = _manifest(FIXTURE_DIR)
    for slug, org in manifest["orgs"].items():  # type: ignore[attr-defined]
        assert org["observable_renewals"] > 0, slug
        assert 0 < org["churn_rate"] < 1, slug


def test_validation_fails_when_a_file_is_tampered_with() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        tampered = generate(CONFIG, Path(scratch))
        target = tampered / "acme-saas" / "stripe.ndjson"
        target.write_text(target.read_text() + "\n")

        with pytest.raises(GenerationValidationError, match="checksum mismatch"):
            validate(CONFIG, tampered)


def test_validation_fails_when_the_churn_rate_is_out_of_range() -> None:
    bad = GenerationConfig(churn_rate_range=(0.99, 1.0))
    with (
        tempfile.TemporaryDirectory() as scratch,
        pytest.raises(GenerationValidationError, match="churn rate"),
    ):
        generate(bad, Path(scratch))


def test_no_real_looking_email_addresses_are_generated() -> None:
    validate(CONFIG, FIXTURE_DIR)


def test_ingestion_code_never_reads_ground_truth_or_defects() -> None:
    forbidden = ("ground_truth", "defects")
    offenders = []
    for path in APP_INGESTION.rglob("*.py"):
        if "generators" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(word in node.value for word in forbidden)
            ):
                offenders.append(f"{path.name}: {node.value!r}")
            if isinstance(node, ast.ImportFrom) and node.module and "generators" in node.module:
                offenders.append(f"{path.name}: imports {node.module}")
    assert offenders == []
