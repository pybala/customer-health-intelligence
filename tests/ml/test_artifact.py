"""An artifact loads only when its digest matches the registry record; tampered files fail."""

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sklearn.dummy import DummyClassifier

from app.ml.artifact import ARTIFACT_FILE, ArtifactIntegrityError, load_artifact, save_artifact


@pytest.fixture
def saved(tmp_path: Path) -> tuple[Path, str]:
    model = DummyClassifier(strategy="prior").fit(np.zeros((4, 1)), np.array([0, 1, 0, 0]))
    digest = save_artifact(tmp_path / "m1", model, {"model_version": "m1"})
    return tmp_path / "m1", digest


def test_artifact_round_trips_and_predicts_the_same(saved: tuple[Path, str]) -> None:
    directory, digest = saved
    loaded = load_artifact(directory, digest)

    x = np.zeros((2, 1))
    assert np.allclose(loaded.pipeline.predict_proba(x), [[0.75, 0.25], [0.75, 0.25]])
    assert loaded.metadata["model_version"] == "m1"
    assert loaded.metadata["artifact_sha256"] == digest


def test_tampered_artifact_is_refused_before_loading(saved: tuple[Path, str]) -> None:
    directory, digest = saved
    with (directory / ARTIFACT_FILE).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="does not match"):
        load_artifact(directory, digest)


def test_wrong_registry_digest_is_refused(saved: tuple[Path, str]) -> None:
    directory, _ = saved
    with pytest.raises(ArtifactIntegrityError):
        load_artifact(directory, "0" * 64)


def test_missing_artifact_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        load_artifact(tmp_path / "absent", "0" * 64)


def test_metadata_records_library_versions(saved: tuple[Path, str]) -> None:
    directory, _ = saved
    record = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))

    assert record["sklearn_version"]
    assert record["joblib_version"]
    assert record["artifact_sha256"]


def test_eligibility_rules_are_not_green_for_missing_or_out_of_window_renewals() -> None:
    from app.ml.inference import eligibility

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    assert eligibility(None, cutoff) == (False, "no renewal date on record")
    assert eligibility(datetime(2025, 12, 1, tzinfo=UTC), cutoff)[0] is False
    assert eligibility(datetime(2026, 6, 1, tzinfo=UTC), cutoff)[0] is False
    assert eligibility(datetime(2026, 2, 1, tzinfo=UTC), cutoff) == (True, None)
    assert eligibility(datetime(2026, 4, 1, tzinfo=UTC), cutoff) == (True, None)
