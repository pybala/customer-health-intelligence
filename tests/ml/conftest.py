"""Canonical fixture loaded into the throwaway database, shared by the feature and leakage tests."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from app.db.session import tenant_session
from app.ingestion.canonical import FIXTURE_DIR, org_id_for, seed_canonical
from app.ml.artifact import LoadedModel
from app.ml.dataset import Example, build_examples, weekly_cutoffs
from app.ml.model import build_logistic_pipeline, temporal_split, to_matrix
from app.ml.train import ORG_SLUGS, TRAINING_END, TRAINING_START, load_outcomes

FEATURE_CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeededAccount:
    org_id: uuid.UUID
    account_id: uuid.UUID
    slug: str


@pytest.fixture(scope="session")
def seeded(owner_engine: Engine, app_engine: Engine) -> Iterator[list[SeededAccount]]:
    seed_canonical(owner_engine, app_engine, FIXTURE_DIR)
    accounts: list[SeededAccount] = []
    for slug in ORG_SLUGS:
        org_id = org_id_for(slug)
        with tenant_session(app_engine, org_id) as session:
            rows = session.execute(text("SELECT id FROM accounts ORDER BY id")).all()
        accounts.extend(SeededAccount(org_id, row.id, slug) for row in rows)
    yield accounts


@pytest.fixture(scope="session")
def canonical_examples(seeded: list[SeededAccount], app_engine: Engine) -> list[Example]:
    outcomes = load_outcomes(FIXTURE_DIR, {slug: org_id_for(slug) for slug in ORG_SLUGS})
    return build_examples(app_engine, outcomes, weekly_cutoffs(TRAINING_START, TRAINING_END))


@pytest.fixture(scope="session")
def fitted_model(canonical_examples: list[Example]) -> LoadedModel:
    x, y = to_matrix(temporal_split(canonical_examples).train)
    return LoadedModel(pipeline=build_logistic_pipeline().fit(x, y), metadata={})
