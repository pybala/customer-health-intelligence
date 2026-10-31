"""The one canonical dataset: fixed organisation IDs, checksum-verified load, idempotent seeding."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.db.session import tenant_session
from app.ingestion.pipeline import IngestReport, ingest_org

FIXTURE_DIR = Path("fixtures/canonical")
ORG_NAMESPACE = uuid.UUID("3c0a6b2e-8f4d-4e0a-9a1b-5d7c2e9f1a60")
INGEST_NOW = datetime(2026, 10, 1, tzinfo=UTC)


class FixtureError(RuntimeError):
    pass


def org_id_for(slug: str) -> uuid.UUID:
    return uuid.uuid5(ORG_NAMESPACE, slug)


RUNTIME_FILE_SUFFIXES = (
    "accounts.ndjson",
    "salesforce.ndjson",
    "gmail.ndjson",
    "zendesk.ndjson",
    "product.ndjson",
    "stripe.ndjson",
)


def verify_fixtures(root: Path) -> None:
    """Raise FixtureError if an ingested file is missing or differs from its manifest checksum.

    Ground-truth and defect files are deliberately not read here: the seed path must never
    open them (see docs/ml-design.md section 4).
    """
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FixtureError(f"{manifest_path} is missing. Run: python -m app.cli generate-fixtures")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if not name.endswith(RUNTIME_FILE_SUFFIXES):
            continue
        path = root / name
        if not path.exists():
            raise FixtureError(f"fixture {name} is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise FixtureError(f"fixture {name} does not match its manifest checksum")


def _org_names(root: Path) -> dict[str, str]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return {slug: str(details["display_name"]) for slug, details in manifest["orgs"].items()}


def ensure_organizations(owner_engine: Engine, names: dict[str, str]) -> dict[str, uuid.UUID]:
    """Create the demo organisations as the owner. RLS hides rows unless the tenant is set.

    A slug already held by a different organisation ID is a conflict, not a duplicate of this
    one, so it is reported rather than reused or overwritten.
    """
    ids: dict[str, uuid.UUID] = {}
    for slug, display_name in names.items():
        org_id = org_id_for(slug)
        with tenant_session(owner_engine, org_id) as session:
            present = session.execute(
                text("SELECT 1 FROM organizations WHERE id = :i"), {"i": org_id}
            ).first()
            if present is None:
                try:
                    with session.begin_nested():
                        session.execute(
                            text(
                                "INSERT INTO organizations (id, slug, display_name) "
                                "VALUES (:i, :s, :n)"
                            ),
                            {"i": org_id, "s": slug, "n": display_name},
                        )
                except IntegrityError as exc:
                    raise FixtureError(
                        f"slug {slug!r} is already used by another organisation; "
                        "remove or rename that row before seeding"
                    ) from exc
        ids[slug] = org_id
    return ids


def seed_canonical(
    owner_engine: Engine, app_engine: Engine, root: Path = FIXTURE_DIR
) -> dict[str, IngestReport]:
    """Load the canonical fixtures for every demo organisation. Re-running adds no rows."""
    verify_fixtures(root)
    ids = ensure_organizations(owner_engine, _org_names(root))
    return {
        slug: ingest_org(app_engine, org_id, root / slug, INGEST_NOW)
        for slug, org_id in ids.items()
    }


__all__ = [
    "FIXTURE_DIR",
    "FixtureError",
    "ensure_organizations",
    "org_id_for",
    "seed_canonical",
    "verify_fixtures",
]
