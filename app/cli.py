import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from app.config import Settings
from app.db.session import make_engine, tenant_session
from app.security.api_keys import issue_key
from app.security.dev_tokens import ROLES, generate_token

OPENAPI_PATH = Path("artifacts/openapi.json")


def _owner_engine() -> Engine:
    settings = Settings()  # type: ignore[call-arg]
    if settings.migration_database_url is None:
        raise SystemExit("MIGRATION_DATABASE_URL is required for operator commands")
    return make_engine(settings.migration_database_url)


def create_org(slug: str, display_name: str) -> UUID:
    engine = _owner_engine()
    org_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, slug, display_name) VALUES (:id, :slug, :name)"),
            {"id": org_id, "slug": slug, "name": display_name},
        )
    engine.dispose()
    return org_id


def issue_api_key(org_id: UUID, role: str) -> str:
    settings = Settings()  # type: ignore[call-arg]
    engine = _owner_engine()
    issued = issue_key(settings.api_key_pepper.get_secret_value())
    with tenant_session(engine, org_id) as session:
        session.execute(
            text(
                "INSERT INTO api_keys (id, org_id, key_prefix, key_hash, role) "
                "VALUES (:id, :org, :prefix, :hash, :role)"
            ),
            {
                "id": uuid4(),
                "org": org_id,
                "prefix": issued.prefix,
                "hash": issued.key_hash,
                "role": role,
            },
        )
    engine.dispose()
    return issued.plaintext


def init_dev_tokens(specs: list[str], path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"{path} already exists; pass --force to overwrite it.", file=sys.stderr)
        return 1
    entries = []
    for spec in specs:
        label, _, rest = spec.partition(":")
        org, _, role = rest.partition(":")
        try:
            org_id = UUID(org)
        except ValueError:
            print(f"Bad --token '{spec}': expected LABEL:ORG_ID:ROLE", file=sys.stderr)
            return 2
        if not label or role not in ROLES:
            print(f"Bad --token '{spec}': role must be one of {sorted(ROLES)}", file=sys.stderr)
            return 2
        entries.append(
            {"label": label, "token": generate_token(), "org_id": str(org_id), "role": role}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps({"tokens": entries}, indent=2) + "\n")
    print(f"Wrote {len(entries)} dev tokens to {path} (mode 600). Do not commit this file.")
    return 0


def export_openapi(check: bool) -> int:
    from app.api.main import create_app

    app = create_app(Settings())  # type: ignore[call-arg]
    rendered = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if check:
        if not OPENAPI_PATH.exists() or OPENAPI_PATH.read_text() != rendered:
            print(f"{OPENAPI_PATH} is out of date. Run: make export-openapi", file=sys.stderr)
            return 1
        print(f"{OPENAPI_PATH} matches the application.")
        return 0
    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(rendered)
    print(f"Wrote {OPENAPI_PATH}")
    return 0


def generate_fixtures(out_dir: Path) -> int:
    from app.ingestion.generators.config import GenerationConfig
    from app.ingestion.generators.generate import generate

    root = generate(GenerationConfig(), out_dir)
    print(f"Wrote and validated the canonical fixtures in {root}")
    return 0


def seed(fixture_dir: Path) -> int:
    from app.ingestion.canonical import FixtureError, seed_canonical

    settings = Settings()  # type: ignore[call-arg]
    owner = _owner_engine()
    app_engine = make_engine(settings.database_url)
    try:
        reports = seed_canonical(owner, app_engine, fixture_dir)
    except FixtureError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        owner.dispose()
        app_engine.dispose()
    for slug, report in reports.items():
        print(
            json.dumps(
                {
                    "org": slug,
                    "accounts_inserted": report.accounts_inserted,
                    "raw_inserted": report.raw_inserted,
                    "events_inserted": report.events_inserted,
                    "issues": dict(report.issues),
                },
                sort_keys=True,
            )
        )
    return 0


def train_model(fixture_dir: Path, artifact_root: Path) -> int:
    from app.ingestion.canonical import org_id_for
    from app.ml.train import ORG_SLUGS, train

    settings = Settings()  # type: ignore[call-arg]
    owner = _owner_engine()
    app_engine = make_engine(settings.database_url)
    try:
        result = train(
            app_engine,
            fixture_dir,
            {slug: org_id_for(slug) for slug in ORG_SLUGS},
            artifact_root,
            owner_engine=owner,
        )
    finally:
        owner.dispose()
        app_engine.dispose()
    summary = {
        "model_version": result["model_version"],
        "selected": result["selected"],
        "counts": result["counts"],
        "artifact_sha256": result["artifact_sha256"],
        "artifact_dir": result["artifact_dir"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def register_model(model_version: str, org_slug: str | None) -> int:
    from app.ingestion.canonical import org_id_for
    from app.ml.registry import mark_registered
    from app.ml.train import ORG_SLUGS

    owner = _owner_engine()
    slugs = [org_slug] if org_slug else list(ORG_SLUGS)
    try:
        for slug in slugs:
            org_id = org_id_for(slug)
            with tenant_session(owner, org_id) as session:
                row = session.execute(
                    text("SELECT id FROM model_versions WHERE model_version = :v AND org_id = :o"),
                    {"v": model_version, "o": org_id},
                ).first()
            if row is None:
                print(f"No candidate {model_version} for {slug}", file=sys.stderr)
                return 1
            mark_registered(owner, org_id, row.id)
            print(f"Registered {model_version} for {slug}")
    finally:
        owner.dispose()
    return 0


def run_worker_once(mode: str, artifact_root: Path) -> int:
    from datetime import UTC, datetime

    from app.worker.run import RunOptions, run_once
    from app.worker.startup import WorkerConfigError, validate_worker_orgs

    settings = Settings()  # type: ignore[call-arg]
    app_engine = make_engine(settings.database_url)
    try:
        try:
            org_ids = validate_worker_orgs(app_engine, settings.worker_org_ids)
        except WorkerConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        options = RunOptions(
            mode=mode,
            artifact_root=artifact_root,
            capacity_fraction=settings.review_capacity_fraction,
            worker_id=f"cli-{uuid4().hex[:12]}",
        )
        results = run_once(app_engine, org_ids, options, datetime.now(UTC))
    finally:
        app_engine.dispose()
    for result in results:
        print(
            json.dumps(
                {
                    "org_id": str(result.org_id),
                    "status": result.status,
                    "counts": result.counts,
                    "error": result.error,
                },
                sort_keys=True,
            )
        )
    return 0 if all(r.status in ("succeeded", "skipped_locked") for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chi")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-org", help="Create a tenant (operator, owner role)")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)

    issue = sub.add_parser("issue-key", help="Issue an API key for a tenant (operator)")
    issue.add_argument("--org-id", required=True, type=UUID)
    issue.add_argument("--role", choices=["csm", "admin", "readonly"], default="csm")

    dev = sub.add_parser("init-dev-tokens", help="Write the local dev token file (demo only)")
    dev.add_argument("--token", action="append", required=True, metavar="LABEL:ORG_ID:ROLE")
    dev.add_argument("--force", action="store_true")

    fixtures = sub.add_parser("generate-fixtures", help="Regenerate the canonical fixture files")
    fixtures.add_argument("--out", type=Path, default=Path("fixtures/canonical"))

    seed_cmd = sub.add_parser("seed", help="Load the canonical fixtures (idempotent)")
    seed_cmd.add_argument("--fixtures", type=Path, default=Path("fixtures/canonical"))

    export = sub.add_parser("export-openapi", help="Write or check artifacts/openapi.json")
    export.add_argument("--check", action="store_true")

    train_cmd = sub.add_parser("train", help="Train and save the offline model (operator)")
    train_cmd.add_argument("--fixtures", type=Path, default=Path("fixtures/canonical"))
    train_cmd.add_argument("--artifacts", type=Path, default=Path("artifacts/models"))

    register = sub.add_parser("register", help="Mark a candidate model as registered (operator)")
    register.add_argument("--model-version", required=True)
    register.add_argument("--org", default=None, help="One org slug; default is all demo orgs")

    worker = sub.add_parser(
        "run-worker-once", help="Run the daily worker once (dry-run by default)"
    )
    worker.add_argument("--mode", choices=["dry_run", "simulated"], default="dry_run")
    worker.add_argument("--artifacts", type=Path, default=Path("artifacts/models"))

    args = parser.parse_args(argv)
    if args.command == "create-org":
        print(create_org(args.slug, args.name))
        return 0
    if args.command == "issue-key":
        print(issue_api_key(args.org_id, args.role))
        return 0
    if args.command == "init-dev-tokens":
        path = Settings().dev_tokens_file  # type: ignore[call-arg]
        return init_dev_tokens(args.token, path, args.force)
    if args.command == "generate-fixtures":
        return generate_fixtures(args.out)
    if args.command == "seed":
        return seed(args.fixtures)
    if args.command == "train":
        return train_model(args.fixtures, args.artifacts)
    if args.command == "register":
        return register_model(args.model_version, args.org)
    if args.command == "run-worker-once":
        return run_worker_once(args.mode, args.artifacts)
    return export_openapi(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
