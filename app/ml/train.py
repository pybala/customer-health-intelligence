"""Offline training workflow. Reads ground truth for labels; never called by runtime code."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import sklearn
from sqlalchemy import Engine

from app.features.definitions import FEATURE_SET_VERSION
from app.ml.artifact import save_artifact
from app.ml.dataset import Example, RenewalOutcome, build_examples, weekly_cutoffs
from app.ml.evaluate import Scored, compare, metrics_for, slice_report
from app.ml.model import (
    build_boosting_pipeline,
    build_logistic_pipeline,
    build_prior_pipeline,
    split_boundaries,
    temporal_split,
    to_matrix,
)
from app.ml.registry import record_evaluation, register_version

MODEL_VERSION = "logreg-v1"
ORG_SLUGS = ("acme-saas", "northwind-cloud", "globex-data")
TRAINING_START = datetime(2024, 1, 1, tzinfo=UTC)
TRAINING_END = datetime(2026, 9, 30, tzinfo=UTC)
SELECTION_RULE = (
    "a learned candidate may replace the prior only if its validation PR-AUC difference from the "
    "prior is reliable with a 95% account-bootstrap CI above zero; among gated candidates within "
    "0.01 validation PR-AUC of the best, choose the lowest validation Brier, ties to logistic "
    "regression; otherwise retain the prior. Brier is uncalibrated because calibration is deferred"
)
CANDIDATES = ("prior", "logistic", "boosting")
LEARNED = ("logistic", "boosting")


def load_outcomes(fixture_dir: Path, org_ids: dict[str, UUID]) -> list[RenewalOutcome]:
    """Read renewal outcomes from ground truth. Only the offline workflow calls this."""
    outcomes: list[RenewalOutcome] = []
    for slug, org_id in org_ids.items():
        path = fixture_dir / slug / "ground_truth.ndjson"
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["kind"] != "term":
                continue
            outcomes.append(
                RenewalOutcome(
                    org_slug=slug,
                    org_id=org_id,
                    account_id=UUID(row["account_id"]),
                    renewal=datetime.fromisoformat(row["renewal"]),
                    churned=bool(row["churned"]),
                    label_observable=bool(row["label_observable"]),
                )
            )
    return outcomes


def _scored(pipeline_scores: dict[str, np.ndarray], examples: list[Example]) -> Scored:
    labels = np.array([e.label for e in examples], dtype=int)
    return Scored(
        labels=labels,
        scores=pipeline_scores,
        account_ids=np.array([str(e.account_id) for e in examples], dtype=object),
        term_ids=np.array(
            [f"{e.account_id}|{e.renewal.isoformat()}" for e in examples], dtype=object
        ),
        org_slugs=np.array([e.org_slug for e in examples], dtype=object),
        segment=np.array([e.snapshot.values["segment"] for e in examples], dtype=object),
        plan_tier=np.array([e.snapshot.values["plan_tier"] for e in examples], dtype=object),
        region=np.array([e.snapshot.values["region"] for e in examples], dtype=object),
        coverage=np.array(
            [e.snapshot.values["coverage_source_count_90d"] for e in examples], dtype=int
        ),
    )


def _predict(pipeline: Any, examples: list[Example]) -> np.ndarray:
    matrix, _ = to_matrix(examples)
    return np.asarray(pipeline.predict_proba(matrix)[:, 1], dtype=float)


def _digest(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture_manifest_digest(fixture_dir: Path) -> str:
    return hashlib.sha256((fixture_dir / "manifest.json").read_bytes()).hexdigest()


def _select(validation: dict[str, dict[str, Any]], validation_gates: dict[str, Any]) -> str:
    """Pre-registered rule (SELECTION_RULE). Uses validation only, never test."""
    gated = [
        name
        for name in LEARNED
        if validation_gates[name]["criterion_beats_baseline_pr_auc"] == "met"
    ]
    if not gated:
        return "prior"
    pr_auc = {name: float(m["pr_auc"]["value"] or 0.0) for name, m in validation.items()}
    best = max(pr_auc.values())
    near = [name for name in gated if pr_auc[name] >= best - 0.01]
    if not near:
        return "prior"
    brier = {name: float(validation[name]["brier"]["value"]) for name in near}
    lowest = min(brier.values())
    tied = sorted(name for name in near if brier[name] == lowest)
    return "logistic" if "logistic" in tied else tied[0]


def train(
    engine: Engine,
    fixture_dir: Path,
    org_ids: dict[str, UUID],
    artifact_root: Path,
    owner_engine: Engine | None = None,
) -> dict[str, Any]:
    outcomes = load_outcomes(fixture_dir, org_ids)
    cutoffs = weekly_cutoffs(TRAINING_START, TRAINING_END)
    examples = build_examples(engine, outcomes, cutoffs)
    split = temporal_split(examples)
    train_end, validation_end = split_boundaries()

    if not split.train:
        raise RuntimeError("no training examples; the fixture window is too short")

    x_train, y_train = to_matrix(split.train)
    fitted = {
        "prior": build_prior_pipeline().fit(x_train, y_train),
        "logistic": build_logistic_pipeline().fit(x_train, y_train),
        "boosting": build_boosting_pipeline().fit(x_train, y_train),
    }

    validation_scored = _scored(
        {name: _predict(model, split.validation) for name, model in fitted.items()},
        split.validation,
    )
    validation = {
        name: metrics_for(
            validation_scored.labels,
            validation_scored.scores[name],
            validation_scored.account_ids,
            validation_scored.term_ids,
        )
        for name in CANDIDATES
    }
    validation_gates = {name: compare(validation_scored, name, "prior") for name in LEARNED}
    chosen = _select(validation, validation_gates)

    selection = {
        "candidates": list(CANDIDATES),
        "selection_rule": SELECTION_RULE,
        "model_version": MODEL_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "train_end": train_end.isoformat(),
        "validation_end": validation_end.isoformat(),
        "validation": validation,
        "validation_comparisons": validation_gates,
        "selected": chosen,
    }
    selection_hash = _digest(selection)

    test_scored = _scored(
        {name: _predict(model, split.test) for name, model in fitted.items()},
        split.test,
    )
    test = {
        name: metrics_for(
            test_scored.labels,
            test_scored.scores[name],
            test_scored.account_ids,
            test_scored.term_ids,
        )
        for name in CANDIDATES
    }
    comparisons = {
        "logistic_vs_prior": compare(test_scored, "logistic", "prior"),
        "boosting_vs_prior": compare(test_scored, "boosting", "prior"),
    }
    slices = {name: slice_report(test_scored, name) for name in CANDIDATES}

    metadata: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "selection_hash": selection_hash,
        "selection_rule": SELECTION_RULE,
        "selected": chosen,
        "calibration": "deferred: too few validation churn terms to fit a calibrator",
        "counts": {
            "examples": len(examples),
            "train": len(split.train),
            "validation": len(split.validation),
            "test": len(split.test),
            "churn_terms_train": _churn_terms(split.train),
            "churn_terms_validation": _churn_terms(split.validation),
            "churn_terms_test": _churn_terms(split.test),
        },
        "splits": {
            "train_end": train_end.isoformat(),
            "validation_end": validation_end.isoformat(),
        },
        "validation": validation,
        "validation_comparisons": validation_gates,
        "test": test,
        "test_comparisons": comparisons,
        "test_slices": slices,
        "training_window": {"start": TRAINING_START.isoformat(), "end": TRAINING_END.isoformat()},
        "dataset_fixture_sha256": _fixture_manifest_digest(fixture_dir),
        "versions": {"scikit_learn": sklearn.__version__, "numpy": np.__version__},
    }
    artifact_dir = artifact_root / MODEL_VERSION
    digest = save_artifact(artifact_dir, fitted[chosen], metadata)
    if owner_engine is not None:
        _register(owner_engine, org_ids, chosen, digest, metadata, test_scored)
    return {**metadata, "artifact_sha256": digest, "artifact_dir": str(artifact_dir)}


def _churn_terms(examples: list[Example]) -> int:
    return len({(e.account_id, e.renewal) for e in examples if e.label == 1})


def _register(
    owner_engine: Engine,
    org_ids: dict[str, UUID],
    chosen: str,
    digest: str,
    metadata: dict[str, Any],
    test_scored: Scored,
) -> None:
    for slug, org_id in org_ids.items():
        version_id = register_version(
            owner_engine,
            org_id,
            model_version=MODEL_VERSION,
            model_kind=chosen,
            feature_set_version=FEATURE_SET_VERSION,
            artifact_sha256=digest,
            training_window=metadata["training_window"],
            metadata={
                "selection_hash": metadata["selection_hash"],
                "counts": metadata["counts"],
                "calibration": metadata["calibration"],
            },
        )
        mask = test_scored.org_slugs == slug
        own = np.flatnonzero(mask)
        scope_metrics: dict[str, Any] = {"examples": int(len(own))}
        if len(own):
            scope_metrics.update(
                metrics_for(
                    test_scored.labels[mask],
                    test_scored.scores[chosen][mask],
                    test_scored.account_ids[mask],
                    test_scored.term_ids[mask],
                )
            )
        record_evaluation(
            owner_engine,
            org_id,
            version_id,
            split="test",
            scope=slug,
            metrics=scope_metrics,
            selection_hash=metadata["selection_hash"],
        )
