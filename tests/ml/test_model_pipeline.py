"""Model pipeline on the canonical fixture: temporal split, determinism, eligibility, isolation.

Uses the shared seeded fixture and the real ingestion path. No separate dataset is created.
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

import app.features.builder as builder
import app.ml.inference as inference
from app.db.session import tenant_session
from app.features.builder import load_snapshot
from app.ingestion.canonical import FIXTURE_DIR, org_id_for
from app.ml.artifact import LoadedModel
from app.ml.dataset import Example, RenewalOutcome
from app.ml.evaluate import evaluate
from app.ml.inference import score_account
from app.ml.model import (
    CATEGORICAL,
    COLUMNS,
    NUMERIC,
    build_logistic_pipeline,
    split_boundaries,
    temporal_split,
    to_matrix,
)
from app.ml.train import load_outcomes
from tests.ml.conftest import SeededAccount

pytestmark = pytest.mark.integration

ANCHOR = datetime(2026, 10, 1, tzinfo=UTC)
ORG_SLUGS = ("acme-saas", "northwind-cloud", "globex-data")


@pytest.fixture(scope="module")
def outcomes() -> list[RenewalOutcome]:
    return load_outcomes(FIXTURE_DIR, {slug: org_id_for(slug) for slug in ORG_SLUGS})


@pytest.fixture(scope="module")
def examples(canonical_examples: list[Example]) -> list[Example]:
    return canonical_examples


def _renewal(engine: Engine, acct: SeededAccount) -> datetime | None:
    with tenant_session(engine, acct.org_id) as session:
        renewal: datetime | None = session.execute(
            text("SELECT renewal_date FROM accounts WHERE id = :a"), {"a": acct.account_id}
        ).scalar_one()
    return renewal


def _in_window(renewal: datetime | None) -> bool:
    return renewal is not None and ANCHOR < renewal <= ANCHOR + timedelta(days=90)


def _floats(values: list[object]) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def test_examples_come_only_from_observable_outcomes(
    examples: list[Example], outcomes: list[RenewalOutcome]
) -> None:
    observable = {(o.account_id, o.renewal) for o in outcomes if o.label_observable}
    censored = {(o.account_id, o.renewal) for o in outcomes if not o.label_observable}
    used = {(e.account_id, e.renewal) for e in examples}

    assert used
    assert censored
    assert used <= observable
    assert not (used & censored)


def test_each_example_renewal_falls_inside_its_cutoff_horizon(examples: list[Example]) -> None:
    assert examples
    for e in examples:
        assert e.cutoff < e.renewal <= e.cutoff + timedelta(days=90)


def test_split_is_by_renewal_date_with_no_overlap(examples: list[Example]) -> None:
    split = temporal_split(examples)
    train_end, validation_end = split_boundaries()

    assert split.train and split.validation and split.test
    assert max(e.renewal for e in split.train) < train_end
    assert all(train_end <= e.renewal < validation_end for e in split.validation)
    assert all(e.renewal >= validation_end for e in split.test)


def test_example_features_read_only_events_visible_at_their_cutoff(
    examples: list[Example], app_engine: Engine
) -> None:
    for e in examples[:: max(1, len(examples) // 40)]:
        ids = sorted({i for p in e.snapshot.provenance.values() for i in p.event_ids})
        if not ids:
            continue
        with tenant_session(app_engine, e.org_id) as session:
            rows = session.execute(
                text(
                    "SELECT event_time, ingested_at FROM source_events "
                    "WHERE org_id = :o AND id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"o": e.org_id, "ids": ids},
            ).all()
        assert len(rows) == len(ids)
        assert all(r.event_time <= e.cutoff and r.ingested_at <= e.cutoff for r in rows)


def test_feature_snapshots_are_identical_across_reads(
    examples: list[Example], app_engine: Engine
) -> None:
    for e in examples[:: max(1, len(examples) // 10)]:
        again = load_snapshot(app_engine, e.org_id, e.account_id, e.cutoff)
        assert again.values == e.snapshot.values
        assert again.provenance == e.snapshot.provenance


def test_fitted_predictions_are_deterministic(examples: list[Example]) -> None:
    x, y = to_matrix(temporal_split(examples).train)

    first = build_logistic_pipeline().fit(x, y)
    second = build_logistic_pipeline().fit(x, y)

    assert np.allclose(first.predict_proba(x), second.predict_proba(x), rtol=0, atol=1e-9)


def test_imputation_statistics_come_from_training_rows_only(examples: list[Example]) -> None:
    split = temporal_split(examples)
    x_train, y_train = to_matrix(split.train)
    pipeline = build_logistic_pipeline().fit(x_train, y_train)
    imputer = (
        pipeline.named_steps["preprocess"].named_transformers_["numeric"].named_steps["impute"]
    )

    differing = []
    for i, name in enumerate(NUMERIC):
        train_median = np.nanmedian(_floats([e.snapshot.values[name] for e in split.train]))
        all_median = np.nanmedian(_floats([e.snapshot.values[name] for e in examples]))
        if np.isfinite(train_median) and np.isfinite(all_median) and train_median != all_median:
            differing.append((i, train_median))

    assert differing, "fixture gives no column where train-only and full medians differ"
    for i, train_median in differing:
        assert imputer.statistics_[i] == pytest.approx(train_median)


def test_scaler_statistics_come_from_training_rows_only(examples: list[Example]) -> None:
    split = temporal_split(examples)
    x_train, y_train = to_matrix(split.train)
    pipeline = build_logistic_pipeline().fit(x_train, y_train)
    numeric = pipeline.named_steps["preprocess"].named_transformers_["numeric"]
    scaler = numeric.named_steps["scale"]
    imputer = numeric.named_steps["impute"]

    filled_train = imputer.transform(_numeric_block(split.train))
    train_std = filled_train.std(axis=0)
    # scikit-learn sets the scale of a constant column to 1 to avoid dividing by zero.
    expected_scale = np.where(train_std == 0.0, 1.0, train_std)
    assert scaler.mean_ == pytest.approx(filled_train.mean(axis=0))
    assert scaler.scale_ == pytest.approx(expected_scale)

    full_mean = imputer.transform(_numeric_block(examples)).mean(axis=0)
    assert not np.allclose(scaler.mean_, full_mean), "fixture gives no train/full difference"


def test_one_hot_categories_are_learned_from_training_rows_only(examples: list[Example]) -> None:
    split = temporal_split(examples)
    x_train, y_train = to_matrix(split.train)
    pipeline = build_logistic_pipeline().fit(x_train, y_train)
    encoder = pipeline.named_steps["preprocess"].named_transformers_["categorical"]

    for column, categories in zip(CATEGORICAL, encoder.categories_, strict=True):
        train_values = {e.snapshot.values[column] for e in split.train}
        assert set(categories) == train_values, column


def test_feature_denylist_is_absent_from_model_inputs() -> None:
    banned = {field.name for field in dataclasses.fields(RenewalOutcome)}
    banned |= {"renewal_date", "cancel_at", "org_id", "organization_id", "slug", "label"}
    assert set(COLUMNS).isdisjoint(banned), set(COLUMNS) & banned


def test_fitted_coefficients_are_reproducible_within_1e9(examples: list[Example]) -> None:
    x, y = to_matrix(temporal_split(examples).train)

    first = build_logistic_pipeline().fit(x, y).named_steps["model"]
    second = build_logistic_pipeline().fit(x, y).named_steps["model"]

    assert np.allclose(first.coef_, second.coef_, rtol=0, atol=1e-9)
    assert np.allclose(first.intercept_, second.intercept_, rtol=0, atol=1e-9)


def _numeric_block(examples: list[Example]) -> np.ndarray:
    return np.array(
        [
            [
                np.nan if e.snapshot.values[n] is None else float(e.snapshot.values[n])
                for n in NUMERIC
            ]
            for e in examples
        ],
        dtype=float,
    )


def test_small_churn_counts_are_flagged_unreliable() -> None:
    labels = np.array([0, 0, 1, 0], dtype=int)
    scores = np.array([0.1, 0.2, 0.9, 0.3])
    accounts = np.array(list("abcd"), dtype=object)

    result = evaluate(labels, scores, accounts)

    assert result["reliable"] is False
    assert result["churn_rows"] == 1
    assert result["churn_terms"] == 1
    assert result["top_10pct"] == {"precision": 1.0, "recall": 1.0}


def test_undefined_metrics_are_none_without_churn() -> None:
    labels = np.zeros(5, dtype=int)
    scores = np.linspace(0, 1, 5)
    accounts = np.array(list("abcde"), dtype=object)

    result = evaluate(labels, scores, accounts)

    assert result["pr_auc"]["value"] is None
    assert result["roc_auc"]["value"] is None
    assert result["top_10pct"]["recall"] is None


def test_accounts_outside_the_renewal_window_are_not_eligible_not_green(
    seeded: list[SeededAccount], app_engine: Engine, fitted_model: LoadedModel
) -> None:
    eligible_count = 0
    ineligible_count = 0
    for acct in seeded:
        renewal = _renewal(app_engine, acct)
        prediction = score_account(app_engine, fitted_model, acct.org_id, acct.account_id, ANCHOR)

        assert prediction.eligible is _in_window(renewal)
        if prediction.eligible:
            assert prediction.risk_score is not None
            assert 0.0 <= prediction.risk_score <= 1.0
            assert prediction.snapshot is not None
            eligible_count += 1
        else:
            assert prediction.risk_score is None
            assert prediction.snapshot is None
            assert prediction.reason
            if renewal is None:
                assert prediction.reason == "no renewal date on record"
            ineligible_count += 1

    assert eligible_count > 0
    assert ineligible_count > 0


def test_scoring_the_same_account_twice_gives_the_same_result(
    seeded: list[SeededAccount], app_engine: Engine, fitted_model: LoadedModel
) -> None:
    acct = next(a for a in seeded if _in_window(_renewal(app_engine, a)))

    first = score_account(app_engine, fitted_model, acct.org_id, acct.account_id, ANCHOR)
    second = score_account(app_engine, fitted_model, acct.org_id, acct.account_id, ANCHOR)

    assert first.risk_score == second.risk_score
    assert first.snapshot is not None and second.snapshot is not None
    assert first.snapshot.values == second.snapshot.values


def test_scoring_path_has_no_ground_truth_reference() -> None:
    for module in (inference, builder):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("ground_truth", "load_outcomes", "app.ml.train", "defects"):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"


def _insert_model_version(owner_engine: Engine, org_id: uuid.UUID) -> uuid.UUID:
    version_id = uuid.uuid4()
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO model_versions (id, org_id, model_version, model_kind, "
                "feature_set_version, artifact_sha256, training_window, metadata) VALUES "
                "(:i, :o, :v, 'logistic', 'features-v1', :h, CAST(:w AS jsonb), "
                "CAST(:m AS jsonb))"
            ),
            {
                "i": version_id,
                "o": org_id,
                "v": f"iso-{uuid.uuid4().hex[:12]}",
                "h": "0" * 64,
                "w": "{}",
                "m": "{}",
            },
        )
    return version_id


def _insert_evaluation(owner_engine: Engine, org_id: uuid.UUID, version_id: uuid.UUID) -> None:
    with tenant_session(owner_engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO model_evaluations (id, org_id, model_version_id, split, scope, "
                "metrics, selection_hash) VALUES (:i, :o, :v, 'validation', 'overall', "
                "CAST(:m AS jsonb), :h)"
            ),
            {"i": uuid.uuid4(), "o": org_id, "v": version_id, "m": "{}", "h": "0" * 64},
        )


def test_model_metadata_is_invisible_to_other_organisations(
    owner_engine: Engine, app_engine: Engine
) -> None:
    acme = org_id_for("acme-saas")
    northwind = org_id_for("northwind-cloud")
    version_id = _insert_model_version(owner_engine, acme)
    _insert_evaluation(owner_engine, acme, version_id)

    with tenant_session(app_engine, acme) as session:
        own_versions = session.execute(
            text("SELECT count(*) FROM model_versions WHERE id = :i"), {"i": version_id}
        ).scalar_one()
        own_evaluations = session.execute(
            text("SELECT count(*) FROM model_evaluations WHERE model_version_id = :i"),
            {"i": version_id},
        ).scalar_one()
    with tenant_session(app_engine, northwind) as session:
        other_versions = session.execute(
            text("SELECT count(*) FROM model_versions WHERE id = :i"), {"i": version_id}
        ).scalar_one()
        other_evaluations = session.execute(
            text("SELECT count(*) FROM model_evaluations WHERE model_version_id = :i"),
            {"i": version_id},
        ).scalar_one()

    assert (own_versions, own_evaluations) == (1, 1)
    assert (other_versions, other_evaluations) == (0, 0)


def test_cross_tenant_model_version_insert_is_rejected(owner_engine: Engine) -> None:
    acme = org_id_for("acme-saas")
    northwind = org_id_for("northwind-cloud")

    with (
        pytest.raises(DBAPIError, match="row-level security"),
        tenant_session(owner_engine, acme) as session,
    ):
        session.execute(
            text(
                "INSERT INTO model_versions (id, org_id, model_version, model_kind, "
                "feature_set_version, artifact_sha256, training_window, metadata) VALUES "
                "(:i, :o, :v, 'logistic', 'features-v1', :h, CAST(:w AS jsonb), "
                "CAST(:m AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "o": northwind,
                "v": f"xtenant-{uuid.uuid4().hex[:12]}",
                "h": "0" * 64,
                "w": "{}",
                "m": "{}",
            },
        )


def test_app_role_cannot_write_model_versions(app_engine: Engine) -> None:
    acme = org_id_for("acme-saas")

    with pytest.raises(DBAPIError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_versions (id, org_id, model_version, model_kind, "
                "feature_set_version, artifact_sha256, training_window, metadata) VALUES "
                "(:i, :o, :v, 'logistic', 'features-v1', :h, '{}'::jsonb, '{}'::jsonb)"
            ),
            {
                "i": uuid.uuid4(),
                "o": acme,
                "v": f"app-write-{uuid.uuid4().hex[:12]}",
                "h": "0" * 64,
            },
        )
