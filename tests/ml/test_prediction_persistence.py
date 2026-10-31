"""Predictions and registry rows: provenance kept, eligibility enforced, tenants isolated."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.session import tenant_session
from app.features.definitions import FEATURE_SET_VERSION
from app.ingestion.canonical import org_id_for
from app.ml.artifact import LoadedModel
from app.ml.inference import Prediction, score_account
from app.ml.registry import mark_registered, record_prediction, register_version
from tests.ml.conftest import SeededAccount

pytestmark = pytest.mark.integration

ANCHOR = datetime(2026, 10, 1, tzinfo=UTC)
PREDICTED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def registered_versions(owner_engine: Engine) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for slug in ("acme-saas", "northwind-cloud"):
        ids[slug] = register_version(
            owner_engine,
            org_id_for(slug),
            model_version=f"persist-{uuid.uuid4().hex[:8]}",
            model_kind="logistic",
            feature_set_version=FEATURE_SET_VERSION,
            artifact_sha256="0" * 64,
            training_window={"start": "2024-01-01", "end": "2026-09-30"},
            metadata={},
        )
    return ids


def _eligible_account(seeded: list[SeededAccount], app_engine: Engine) -> SeededAccount:
    for acct in seeded:
        with tenant_session(app_engine, acct.org_id) as session:
            renewal = session.execute(
                text("SELECT renewal_date FROM accounts WHERE id = :a"), {"a": acct.account_id}
            ).scalar_one()
        if renewal is not None and ANCHOR < renewal <= ANCHOR + timedelta(days=90):
            return acct
    pytest.fail("no canonical account renews within the 90-day window at the anchor")


def test_eligible_prediction_persists_with_model_version_features_and_provenance(
    seeded: list[SeededAccount],
    app_engine: Engine,
    owner_engine: Engine,
    fitted_model: LoadedModel,
    registered_versions: dict[str, uuid.UUID],
) -> None:
    acct = _eligible_account(seeded, app_engine)
    prediction = score_account(app_engine, fitted_model, acct.org_id, acct.account_id, ANCHOR)
    assert prediction.eligible and prediction.snapshot is not None

    record_prediction(app_engine, registered_versions[acct.slug], prediction, PREDICTED_AT)

    with tenant_session(app_engine, acct.org_id) as session:
        row = session.execute(
            text(
                "SELECT model_version_id, predicted_at, cutoff_at, risk_score, feature_values, "
                "feature_provenance, eligible FROM predictions WHERE account_id = :a "
                "AND predicted_at = :p"
            ),
            {"a": acct.account_id, "p": PREDICTED_AT},
        ).one()

    assert row.model_version_id == registered_versions[acct.slug]
    assert row.predicted_at == PREDICTED_AT
    assert row.cutoff_at == ANCHOR
    assert row.eligible is True
    assert row.risk_score == pytest.approx(prediction.risk_score)
    assert set(row.feature_values) == set(prediction.snapshot.values)
    assert set(row.feature_provenance) == set(prediction.snapshot.provenance)
    assert any(p["event_ids"] for p in row.feature_provenance.values())


def test_ineligible_prediction_is_stored_with_reason_and_no_score(
    seeded: list[SeededAccount],
    app_engine: Engine,
    fitted_model: LoadedModel,
    registered_versions: dict[str, uuid.UUID],
) -> None:
    acct = next(a for a in seeded if a.slug == "acme-saas")
    prediction = score_account(
        app_engine, fitted_model, acct.org_id, acct.account_id, datetime(2024, 1, 1, tzinfo=UTC)
    )
    assert not prediction.eligible

    record_prediction(app_engine, registered_versions[acct.slug], prediction, PREDICTED_AT)

    with tenant_session(app_engine, acct.org_id) as session:
        row = session.execute(
            text(
                "SELECT eligible, risk_score, ineligible_reason FROM predictions "
                "WHERE account_id = :a AND predicted_at = :p"
            ),
            {"a": acct.account_id, "p": PREDICTED_AT},
        ).one()
    assert row.eligible is False
    assert row.risk_score is None
    assert row.ineligible_reason


def test_eligible_prediction_without_a_score_violates_the_check(
    app_engine: Engine, owner_engine: Engine, registered_versions: dict[str, uuid.UUID]
) -> None:
    acme = org_id_for("acme-saas")
    bogus = Prediction(
        org_id=acme,
        account_id=_any_acme_account(owner_engine),
        eligible=True,
        reason=None,
        renewal_date=None,
        risk_score=None,
        cutoff_at=ANCHOR,
        snapshot=None,
    )
    with pytest.raises(IntegrityError, match="ck_prediction_eligibility"):
        record_prediction(owner_engine, registered_versions["acme-saas"], bogus, PREDICTED_AT)


def _any_acme_account(owner_engine: Engine) -> uuid.UUID:
    with tenant_session(owner_engine, org_id_for("acme-saas")) as session:
        account_id: uuid.UUID = session.execute(
            text("SELECT id FROM accounts LIMIT 1")
        ).scalar_one()
    return account_id


def test_app_role_cannot_register_a_version(
    app_engine: Engine, registered_versions: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"), app_engine.begin() as conn:
        conn.execute(
            text("UPDATE model_versions SET status = 'registered' WHERE id = :i"),
            {"i": registered_versions["acme-saas"]},
        )


def test_owner_can_register_a_version_under_tenant_context(
    owner_engine: Engine, registered_versions: dict[str, uuid.UUID]
) -> None:
    acme = org_id_for("acme-saas")
    mark_registered(owner_engine, acme, registered_versions["acme-saas"])

    with tenant_session(owner_engine, acme) as session:
        status = session.execute(
            text("SELECT status FROM model_versions WHERE id = :i"),
            {"i": registered_versions["acme-saas"]},
        ).scalar_one()
    assert status == "registered"


def test_app_cannot_insert_a_prediction_for_another_tenant_under_its_own_context(
    seeded: list[SeededAccount], app_engine: Engine, registered_versions: dict[str, uuid.UUID]
) -> None:
    acme = org_id_for("acme-saas")
    northwind = next(a for a in seeded if a.slug == "northwind-cloud")
    with (
        pytest.raises(DBAPIError, match="row-level security"),
        tenant_session(app_engine, acme) as session,
    ):
        session.execute(
            text(
                "INSERT INTO predictions (id, org_id, account_id, model_version_id, "
                "feature_set_version, cutoff_at, eligible, ineligible_reason) VALUES "
                "(:i, :o, :a, :v, :f, :c, false, 'spoof')"
            ),
            {
                "i": uuid.uuid4(),
                "o": northwind.org_id,
                "a": northwind.account_id,
                "v": registered_versions["northwind-cloud"],
                "f": FEATURE_SET_VERSION,
                "c": ANCHOR,
            },
        )


def test_predictions_are_invisible_across_tenants(
    seeded: list[SeededAccount], app_engine: Engine
) -> None:
    acme = org_id_for("acme-saas")
    northwind = org_id_for("northwind-cloud")
    with tenant_session(app_engine, northwind) as session:
        count = session.execute(
            text("SELECT count(*) FROM predictions WHERE org_id = :o"), {"o": acme}
        ).scalar_one()
    assert count == 0
