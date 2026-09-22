from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path

from mqa_cv.config import Config
from mqa_cv.db import Database
from mqa_cv.hub import HubClient
from mqa_cv.profiles import discover_profiles
from mqa_cv.reporting import generate_dataset_reports, generate_summary
from mqa_cv.validator import ShaclValidatorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    for name in (
        "search_base_url", "repo_base_url", "catalogue_id", "rdf_dir",
        "reports_dir", "db_path", "validator_url",
    ):
        value = getattr(args, name, None)
        if value is not None:
            setattr(cfg, name, Path(value) if name in {"rdf_dir", "reports_dir", "db_path"} else value)

    if getattr(args, "timeout", None) is not None:
        cfg.timeout = args.timeout
    if getattr(args, "validator_timeout", None) is not None:
        cfg.validator_timeout = args.validator_timeout
    if getattr(args, "retries", None) is not None:
        cfg.validator_retries = args.retries
    if getattr(args, "search_retries", None) is not None:
        cfg.search_retries = args.search_retries
    if getattr(args, "fetch_retries", None) is not None:
        cfg.fetch_retries = args.fetch_retries
    if getattr(args, "page_size", None) is not None:
        cfg.page_size = args.page_size
    return cfg


def _make_clients(cfg: Config):
    hub = HubClient(
        search_base_url=cfg.search_base_url,
        repo_base_url=cfg.repo_base_url,
        timeout=cfg.timeout,
        search_retries=cfg.search_retries,
        fetch_retries=cfg.fetch_retries,
    )
    validator = ShaclValidatorClient(
        endpoint=cfg.validator_url,
        timeout=cfg.validator_timeout,
        request_delay=cfg.validator_request_delay,
        retries=cfg.validator_retries,
    )
    return hub, validator


def discover(cfg: Config, limit: int | None) -> list[str]:
    db = Database(cfg.db_path)
    db.init_schema()
    hub, _ = _make_clients(cfg)
    ids = list(hub.iter_dataset_ids(cfg.catalogue_id, cfg.page_size, limit))
    logger.info("Search API returned %d dataset IDs for catalogue '%s'.", len(ids), cfg.catalogue_id)
    return ids


def fetch_one(cfg: Config, hub: HubClient, db: Database, dataset_id: str) -> bool:
    cfg.rdf_dir.mkdir(parents=True, exist_ok=True)
    safe_id = dataset_id.replace("/", "_").replace(":", "_")
    rdf_path = cfg.rdf_dir / f"{safe_id}.ttl"
    try:
        ttl_bytes, source_url, content_type = hub.fetch_dataset_rdf(dataset_id)
        rdf_path.write_bytes(ttl_bytes)
        sha256_hash = hashlib.sha256(ttl_bytes).hexdigest()
        db.save_dataset(
            dataset_id=dataset_id,
            catalogue_id=cfg.catalogue_id,
            source_url=source_url,
            rdf_path=str(rdf_path),
            sha256=sha256_hash,
            fetch_status="ok",
            fetch_http_status=200,
            fetch_error=None,
        )
        logger.info("Fetched %s (%d bytes, %s)", dataset_id, len(ttl_bytes), content_type or "unknown content-type")
        return True
    except Exception as exc:
        logger.error("Fetch error for '%s': %s", dataset_id, exc)
        db.save_dataset(
            dataset_id=dataset_id,
            catalogue_id=cfg.catalogue_id,
            source_url=f"{cfg.repo_base_url}/datasets/{dataset_id}",
            rdf_path=str(rdf_path) if rdf_path.exists() else "",
            sha256="",
            fetch_status="error",
            fetch_http_status=getattr(getattr(exc, "response", None), "status_code", 500),
            fetch_error=str(exc),
        )
        return False


def fetch(cfg: Config, limit: int | None) -> None:
    db = Database(cfg.db_path)
    db.init_schema()
    hub, _ = _make_clients(cfg)
    # Fetch only IDs currently discoverable from Search. This avoids treating an
    # arbitrary old database row as part of a new acquisition run.
    ids = list(hub.iter_dataset_ids(cfg.catalogue_id, cfg.page_size, limit))
    for idx, dataset_id in enumerate(ids, 1):
        logger.info("[%d/%d] Fetch %s", idx, len(ids), dataset_id)
        fetch_one(cfg, hub, db, dataset_id)


def validate(cfg: Config, limit: int | None, profiles: list[str], openapi: Path | None) -> None:
    db = Database(cfg.db_path)
    db.init_schema()
    _, validator = _make_clients(cfg)

    rows = db.get_connection().execute(
        "SELECT dataset_id, rdf_path FROM datasets WHERE fetch_status = 'ok' ORDER BY dataset_id LIMIT ?",
        (limit if limit is not None else -1,),
    ).fetchall()

    logger.info("Validating %d fetched dataset(s) against %d profile(s).", len(rows), len(profiles))
    for idx, row in enumerate(rows, 1):
        rdf_path = Path(row["rdf_path"])
        if not rdf_path.is_file():
            logger.warning("[%d/%d] Missing RDF file: %s", idx, len(rows), rdf_path)
            continue
        rdf_bytes = rdf_path.read_bytes()
        for profile in profiles:
            logger.info("[%d/%d] %s -> %s", idx, len(rows), row["dataset_id"], profile)
            res = validator.validate(rdf_bytes, profile)
            db.save_validation_result(
                dataset_id=row["dataset_id"],
                validator_url=cfg.validator_url,
                shape_model=res.shape_model,
                conforms=res.conforms,
                results=res.results,
                violations=res.violations,
                warnings=res.warnings,
                infos=res.infos,
                http_status=res.http_status,
                error=res.error,
            )
            state = "COMPLIANT" if res.conforms is True else "NON_COMPLIANT" if res.conforms is False else "VALIDATOR_ERROR"
            logger.info("    %s: HTTP=%s results=%d violations=%d", state, res.http_status, res.results, res.violations)


def run_pipeline(cfg: Config, limit: int | None, profiles: list[str]) -> None:
    cfg.rdf_dir.mkdir(parents=True, exist_ok=True)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.init_schema()
    hub, validator = _make_clients(cfg)

    dataset_ids = list(hub.iter_dataset_ids(cfg.catalogue_id, cfg.page_size, limit))
    logger.info("Acquisition: %d dataset ID(s).", len(dataset_ids))

    for idx, dataset_id in enumerate(dataset_ids, 1):
        logger.info("[%d/%d] Dataset %s", idx, len(dataset_ids), dataset_id)
        if not fetch_one(cfg, hub, db, dataset_id):
            continue
        row = db.get_connection().execute(
            "SELECT rdf_path FROM datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        if not row:
            continue
        rdf_bytes = Path(row["rdf_path"]).read_bytes()
        for profile in profiles:
            logger.info("[%d/%d] Validate %s -> %s", idx, len(dataset_ids), dataset_id, profile)
            res = validator.validate(rdf_bytes, profile)
            db.save_validation_result(
                dataset_id, cfg.validator_url, res.shape_model, res.conforms,
                res.results, res.violations, res.warnings, res.infos,
                res.http_status, res.error,
            )
            state = "COMPLIANT" if res.conforms is True else "NON_COMPLIANT" if res.conforms is False else "VALIDATOR_ERROR"
            logger.info("    %s: HTTP=%s results=%d violations=%d", state, res.http_status, res.results, res.violations)

    report(cfg)


def report(cfg: Config) -> None:
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    db.init_schema()
    with db.get_connection() as conn:
        generate_dataset_reports(conn, cfg.reports_dir)
        generate_summary(conn, cfg.reports_dir)
    logger.info("Reports generated in %s", cfg.reports_dir)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalogue-id", default=None)
    parser.add_argument("--search-base-url", default=None)
    parser.add_argument("--repo-base-url", default=None)
    parser.add_argument("--validator-url", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--rdf-dir", default=None)
    parser.add_argument("--reports-dir", default=None)
    parser.add_argument("--timeout", type=int, default=None, help="Hub HTTP timeout in seconds")
    parser.add_argument("--validator-timeout", type=int, default=None)
    parser.add_argument("--retries", type=int, default=None, help="SHACL validator retries")
    parser.add_argument("--search-retries", type=int, default=None)
    parser.add_argument("--fetch-retries", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=None)


def main() -> None:
    parser = argparse.ArgumentParser(description="MQA Cross-Version Validator")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Search -> fetch -> validate -> report")
    run_p.add_argument("--limit", type=int, default=None)
    run_p.add_argument("--profiles", default=None, help="Comma-separated shapeModel list")
    run_p.add_argument("--openapi", type=Path, default=None)
    add_common(run_p)

    discover_p = sub.add_parser("discover", help="Discover dataset IDs through Hub Search API")
    discover_p.add_argument("--limit", type=int, default=None)
    add_common(discover_p)

    fetch_p = sub.add_parser("fetch", help="Discover and fetch RDF through Hub Repo API")
    fetch_p.add_argument("--limit", type=int, default=None)
    add_common(fetch_p)

    validate_p = sub.add_parser("validate", help="Validate already fetched RDF files")
    validate_p.add_argument("--limit", type=int, default=None)
    validate_p.add_argument("--profiles", default=None)
    validate_p.add_argument("--all-profiles", action="store_true")
    validate_p.add_argument("--openapi", type=Path, default=None)
    add_common(validate_p)

    report_p = sub.add_parser("report", help="Regenerate reports from SQLite")
    add_common(report_p)

    args = parser.parse_args()
    cfg = _load_config(args)

    openapi = getattr(args, "openapi", None)
    if getattr(args, "profiles", None):
        profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    elif getattr(args, "all_profiles", False) or args.command == "run":
        profiles = discover_profiles(openapi)
    else:
        profiles = discover_profiles(openapi)

    if args.command == "discover":
        discover(cfg, args.limit)
    elif args.command == "fetch":
        fetch(cfg, args.limit)
    elif args.command == "validate":
        validate(cfg, args.limit, profiles, openapi)
    elif args.command == "report":
        report(cfg)
    elif args.command == "run":
        run_pipeline(cfg, args.limit, profiles)


if __name__ == "__main__":
    main()
