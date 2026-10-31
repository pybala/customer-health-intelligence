"""Candidate models and the temporal split. Preprocessing and estimator form one pipeline."""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

from app.features.definitions import EVENT_FEATURES
from app.ml.dataset import Example

CATEGORICAL = ("segment", "plan_tier", "region")
NUMERIC = (*(spec.name for spec in EVENT_FEATURES), "tenure_months")
COLUMNS = (*NUMERIC, *CATEGORICAL)

DATA_ANCHOR = datetime.fromisoformat("2026-10-01T00:00:00+00:00")


@dataclass(frozen=True, slots=True)
class Split:
    train: list[Example]
    validation: list[Example]
    test: list[Example]


def split_boundaries() -> tuple[datetime, datetime]:
    """Renewal-date boundaries for the design's month windows (months 1-36 end at the anchor).

    Train covers renewals in months 4-24, validation 25-30, test 31-36, counted back from the
    anchor. The history starts 36 months before the anchor, so month 1 begins there.
    """
    history_start = DATA_ANCHOR - timedelta(days=36 * 30.4375)
    train_end = (history_start + timedelta(days=24 * 30.4375)).replace(hour=0, minute=0, second=0)
    validation_end = (history_start + timedelta(days=30 * 30.4375)).replace(
        hour=0, minute=0, second=0
    )
    return train_end, validation_end


def temporal_split(examples: list[Example]) -> Split:
    train_end, validation_end = split_boundaries()
    return Split(
        train=[e for e in examples if e.renewal < train_end],
        validation=[e for e in examples if train_end <= e.renewal < validation_end],
        test=[e for e in examples if e.renewal >= validation_end],
    )


def to_matrix(examples: list[Example]) -> tuple[NDArray[np.object_], NDArray[np.int_]]:
    rows = [[e.snapshot.values[name] for name in COLUMNS] for e in examples]
    matrix = np.array(rows, dtype=object)
    labels = np.array([e.label for e in examples], dtype=int)
    return matrix, labels


def build_logistic_pipeline() -> Pipeline:
    width = len(NUMERIC)
    preprocess = ColumnTransformer(
        [
            (
                "numeric",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scale", StandardScaler()),
                    ]
                ),
                list(range(width)),
            ),
            (
                "categorical",
                OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=1),
                list(range(width, width + len(CATEGORICAL))),
            ),
        ]
    )
    return Pipeline(
        [
            ("preprocess", preprocess),
            (
                "model",
                LogisticRegression(C=1.0, class_weight="balanced", max_iter=1000, random_state=0),
            ),
        ]
    )


def build_prior_pipeline() -> Pipeline:
    return Pipeline([("model", DummyClassifier(strategy="prior"))])


def build_boosting_pipeline() -> Pipeline:
    """Fixed, shallow configuration from ml-design section 5; not tuned against any split."""
    preprocess = ColumnTransformer(
        [
            ("numeric", "passthrough", list(range(len(NUMERIC)))),
            (
                "categorical",
                OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1),
                list(range(len(NUMERIC), len(NUMERIC) + len(CATEGORICAL))),
            ),
        ]
    )
    return Pipeline(
        [
            ("preprocess", preprocess),
            (
                "model",
                HistGradientBoostingClassifier(
                    max_depth=3,
                    max_iter=100,
                    learning_rate=0.05,
                    class_weight="balanced",
                    random_state=0,
                ),
            ),
        ]
    )
