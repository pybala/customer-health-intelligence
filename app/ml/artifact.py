"""Save and load the fitted pipeline. Loading verifies SHA-256 before joblib runs.

joblib uses pickle, which can execute code on load. A file is loaded only after its digest
matches the value recorded when the project's own training workflow wrote it.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import sklearn

ARTIFACT_FILE = "model.joblib"
METADATA_FILE = "metadata.json"


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedModel:
    pipeline: Any
    metadata: dict[str, Any]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def save_artifact(directory: Path, pipeline: Any, metadata: dict[str, Any]) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / ARTIFACT_FILE
    joblib.dump(pipeline, model_path)
    digest = sha256_of(model_path)
    record = {
        **metadata,
        "artifact_sha256": digest,
        "sklearn_version": sklearn.__version__,
        "joblib_version": joblib.__version__,
    }
    (directory / METADATA_FILE).write_text(json.dumps(record, indent=2, sort_keys=True))
    return digest


def load_artifact(directory: Path, expected_sha256: str) -> LoadedModel:
    """Refuse to unpickle unless the file's digest equals the registry's recorded digest."""
    model_path = directory / ARTIFACT_FILE
    if not model_path.exists():
        raise ArtifactIntegrityError(f"{model_path} is missing")
    actual = sha256_of(model_path)
    if actual != expected_sha256:
        raise ArtifactIntegrityError(
            f"{model_path} digest {actual} does not match the registered {expected_sha256}"
        )
    metadata = json.loads((directory / METADATA_FILE).read_text(encoding="utf-8"))
    pipeline = joblib.load(model_path)
    return LoadedModel(pipeline=pipeline, metadata=metadata)
