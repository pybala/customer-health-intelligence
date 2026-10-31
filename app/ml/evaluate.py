"""Metrics with explicit undefined and unreliable markers. Offline only.

Examples are weekly cutoffs of the same renewal term, so one churned term contributes several
rows. Reliability is therefore judged on churned renewal terms, not on rows. Confidence intervals
resample accounts, because rows from one account are correlated.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

MIN_CHURN_TERMS = 30
BOOTSTRAP_RESAMPLES = 1000
RNG_SEED = 20261001
COVERAGE_BANDS = ((0, 1, "0-1 sources"), (2, 3, "2-3 sources"), (4, 5, "4-5 sources"))

Scalar = Callable[[NDArray[np.int_], NDArray[np.float64]], float | None]


@dataclass(frozen=True, slots=True)
class Scored:
    """Aligned arrays for one set of examples, all scored by every compared model."""

    labels: NDArray[np.int_]
    scores: dict[str, NDArray[np.float64]]
    account_ids: NDArray[np.object_]
    term_ids: NDArray[np.object_]
    org_slugs: NDArray[np.object_]
    segment: NDArray[np.object_]
    plan_tier: NDArray[np.object_]
    region: NDArray[np.object_]
    coverage: NDArray[np.int_]


def pr_auc(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float | None:
    if labels.sum() == 0:
        return None
    return float(average_precision_score(labels, scores))


def roc_auc(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float | None:
    if labels.sum() == 0 or labels.sum() == len(labels):
        return None
    return float(roc_auc_score(labels, scores))


def brier(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float | None:
    if len(labels) == 0:
        return None
    return float(brier_score_loss(labels, scores))


def top_k(
    labels: NDArray[np.int_], scores: NDArray[np.float64], fraction: float
) -> tuple[float | None, float | None]:
    if len(scores) == 0:
        return None, None
    k = max(1, round(len(scores) * fraction))
    order = np.argsort(-scores, kind="stable")[:k]
    hits = int(labels[order].sum())
    total = int(labels.sum())
    precision = hits / k
    recall = None if total == 0 else hits / total
    return precision, recall


def _account_groups(
    account_ids: NDArray[np.object_],
) -> tuple[NDArray[np.object_], list[NDArray[np.int_]]]:
    accounts = np.unique(account_ids)
    return accounts, [np.flatnonzero(account_ids == a) for a in accounts]


def bootstrap_metric(
    labels: NDArray[np.int_],
    scores: NDArray[np.float64],
    account_ids: NDArray[np.object_],
    metric: Scalar,
) -> tuple[float | None, float | None]:
    """Percentile 95% CI resampling accounts; each draw keeps all rows of a chosen account."""
    accounts, groups = _account_groups(account_ids)
    rng = np.random.default_rng(RNG_SEED)
    draws: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        picked = rng.integers(0, len(accounts), size=len(accounts))
        index = np.concatenate([groups[i] for i in picked])
        value = metric(labels[index], scores[index])
        if value is not None:
            draws.append(value)
    if len(draws) < BOOTSTRAP_RESAMPLES // 2:
        return None, None
    low, high = np.percentile(draws, [2.5, 97.5])
    return float(low), float(high)


def bootstrap_difference(
    labels: NDArray[np.int_],
    model_scores: NDArray[np.float64],
    baseline_scores: NDArray[np.float64],
    account_ids: NDArray[np.object_],
    metric: Scalar,
) -> dict[str, float | None]:
    """Model minus baseline on the same examples, with accounts resampled jointly for both.

    A difference interval that excludes zero is required before a comparison is reported as an
    improvement. The interval is returned as-is; it is not called significant on its own.
    """
    accounts, groups = _account_groups(account_ids)
    rng = np.random.default_rng(RNG_SEED)
    draws: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        picked = rng.integers(0, len(accounts), size=len(accounts))
        index = np.concatenate([groups[i] for i in picked])
        m = metric(labels[index], model_scores[index])
        b = metric(labels[index], baseline_scores[index])
        if m is not None and b is not None:
            draws.append(m - b)
    point_m = metric(labels, model_scores)
    point_b = metric(labels, baseline_scores)
    point = None if point_m is None or point_b is None else point_m - point_b
    if len(draws) < BOOTSTRAP_RESAMPLES // 2:
        return {"difference": point, "ci_low": None, "ci_high": None, "draws": len(draws)}
    low, high = np.percentile(draws, [2.5, 97.5])
    return {
        "difference": point,
        "ci_low": float(low),
        "ci_high": float(high),
        "draws": len(draws),
    }


def _reliable(churn_terms: int) -> bool:
    return churn_terms >= MIN_CHURN_TERMS


def _churn_terms(labels: NDArray[np.int_], term_ids: NDArray[np.object_]) -> int:
    return len({t for t, y in zip(term_ids, labels, strict=True) if y == 1})


def metrics_for(
    labels: NDArray[np.int_],
    scores: NDArray[np.float64],
    account_ids: NDArray[np.object_],
    term_ids: NDArray[np.object_],
) -> dict[str, Any]:
    terms = _churn_terms(labels, term_ids)
    result: dict[str, Any] = {
        "examples": int(len(labels)),
        "accounts": int(len(np.unique(account_ids))),
        "churn_rows": int(labels.sum()),
        "churn_terms": terms,
        "base_rate": float(labels.mean()) if len(labels) else None,
        "reliable": _reliable(terms),
    }
    for name, metric in (("pr_auc", pr_auc), ("roc_auc", roc_auc), ("brier", brier)):
        low, high = bootstrap_metric(labels, scores, account_ids, metric)
        result[name] = {"value": metric(labels, scores), "ci_low": low, "ci_high": high}
    for fraction in (0.05, 0.10, 0.20):
        precision, recall = top_k(labels, scores, fraction)
        result[f"top_{round(fraction * 100)}pct"] = {"precision": precision, "recall": recall}
    return result


def evaluate(
    labels: NDArray[np.int_],
    scores: NDArray[np.float64],
    account_ids: NDArray[np.object_],
    term_ids: NDArray[np.object_] | None = None,
) -> dict[str, Any]:
    terms = term_ids if term_ids is not None else account_ids
    return metrics_for(labels, scores, account_ids, terms)


def _slice_masks(scored: Scored) -> list[tuple[str, str, NDArray[np.bool_]]]:
    masks: list[tuple[str, str, NDArray[np.bool_]]] = []
    for slug in sorted(set(scored.org_slugs.tolist())):
        masks.append(("org", slug, scored.org_slugs == slug))
    for dimension, values in (
        ("segment", scored.segment),
        ("plan_tier", scored.plan_tier),
        ("region", scored.region),
    ):
        for value in sorted(set(values.tolist())):
            masks.append((dimension, str(value), values == value))
    for low, high, label in COVERAGE_BANDS:
        masks.append(("coverage", label, (scored.coverage >= low) & (scored.coverage <= high)))
    return masks


def slice_report(scored: Scored, model: str) -> list[dict[str, Any]]:
    """Per-slice metrics for one model. Each slice carries its own reliability flag."""
    scores = scored.scores[model]
    rows: list[dict[str, Any]] = []
    for dimension, value, mask in _slice_masks(scored):
        if not mask.any():
            continue
        labels = scored.labels[mask]
        terms = _churn_terms(labels, scored.term_ids[mask])
        entry: dict[str, Any] = {
            "dimension": dimension,
            "slice": value,
            "examples": int(mask.sum()),
            "accounts": int(len(np.unique(scored.account_ids[mask]))),
            "churn_rows": int(labels.sum()),
            "churn_terms": terms,
            "reliable": _reliable(terms),
            "pr_auc": pr_auc(labels, scores[mask]),
            "brier": brier(labels, scores[mask]),
            "top_10pct": top_k(labels, scores[mask], 0.10),
        }
        if not entry["reliable"]:
            entry["pr_auc"] = None
            entry["brier"] = None
            entry["top_10pct"] = None
            entry["note"] = f"fewer than {MIN_CHURN_TERMS} churned terms; metrics suppressed"
        rows.append(entry)
    return rows


def compare(scored: Scored, model: str, baseline: str) -> dict[str, Any]:
    """Model minus baseline on the same examples. The comparison is reported only when reliable."""
    terms = _churn_terms(scored.labels, scored.term_ids)
    out: dict[str, Any] = {
        "model": model,
        "baseline": baseline,
        "reliable": _reliable(terms),
        "churn_terms": terms,
    }
    for name, metric in (("pr_auc", pr_auc), ("brier", brier)):
        out[name] = bootstrap_difference(
            scored.labels, scored.scores[model], scored.scores[baseline], scored.account_ids, metric
        )
    improvement = (
        out["pr_auc"]["ci_low"] is not None
        and out["pr_auc"]["ci_low"] > 0
        and bool(out["reliable"])
    )
    out["criterion_beats_baseline_pr_auc"] = (
        "met" if improvement else "unmet: interval includes zero or churn terms below threshold"
    )
    return out
