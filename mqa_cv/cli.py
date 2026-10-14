import argparse
import gzip
import hashlib
import json
import logging
import os
import sys

from mqa_cv.config import Config
from mqa_cv.db import Database
from mqa_cv.hub import HubClient
from mqa_cv.profiles import (
    ProfileSource,
    discover_profiles,
    ordered_validation_groups,
    ordered_validation_plan,
)
from mqa_cv.validator import ShaclValidatorClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _clients(cfg: Config):
    hub = HubClient(
        search_base_url=cfg.search_base_url,
        repo_base_url=cfg.repo_base_url,
        timeout=cfg.timeout,
    )
    db = Database(db_path=cfg.db_path)
    db.init_schema()
    return hub, db


def discover_datasets(catalogue_id: str | None = None, limit: int | None = None) -> None:
    """Discover dataset IDs only. No RDF is downloaded and nothing is validated."""
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id

    hub, db = _clients(cfg)
    logger.info("Discovering dataset IDs: catalogue=%s", cfg.catalogue_id)
    all_ids = hub.get_dataset_ids(cfg.catalogue_id)
    ids = all_ids[:limit] if limit is not None else all_ids
    inserted, total = db.add_discovered_datasets(ids, cfg.catalogue_id)
    logger.info(
        "Discovery completed: received=%d new=%d inventory_total=%d",
        len(ids), inserted, total,
    )


def fetch_datasets(limit: int | None = None) -> int:
    """Fetch RDF for datasets already present in the local inventory."""
    cfg = Config()
    cfg.rdf_dir.mkdir(parents=True, exist_ok=True)
    hub, db = _clients(cfg)

    rows = db.get_pending_fetch(limit)
    logger.info("RDF acquisition queue: %d dataset(s)", len(rows))
    completed = 0

    for idx, row in enumerate(rows, 1):
        dataset_id = row["dataset_id"]
        safe_id = dataset_id.replace("/", "_").replace(":", "_")
        rdf_path = cfg.rdf_dir / f"{safe_id}.ttl"
        logger.info("[fetch %d/%d] %s", idx, len(rows), dataset_id)

        try:
            content, source_url = hub.fetch_dataset_rdf(dataset_id)
            data = content.encode("utf-8")
            rdf_path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            db.update_fetch(
                dataset_id,
                status="ok",
                source_url=source_url,
                rdf_path=str(rdf_path),
                sha256=digest,
                http_status=200,
                error=None,
            )
            completed += 1
        except Exception as exc:
            logger.exception("RDF fetch failed for %s", dataset_id)
            db.update_fetch(
                dataset_id,
                status="fetch_error",
                source_url=f"{cfg.repo_base_url}/datasets/{dataset_id}",
                rdf_path=str(rdf_path) if rdf_path.exists() else None,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                error=str(exc),
            )

    logger.info("Fetch completed: successful=%d failed=%d", completed, len(rows) - completed)
    return completed


def _load_validation_profiles(cfg: Config) -> tuple[ProfileSource, list[list[str]]]:
    source = discover_profiles(
        cfg.validator_openapi_path,
        validator_endpoint=cfg.validator_url,
        openapi_url=cfg.validator_openapi_url,
        timeout=min(5.0, float(cfg.validator_timeout)),
    )
    groups = ordered_validation_groups(source.profiles)
    plan = ordered_validation_plan(source.profiles)

    logger.info(
        "Validation profile source: %s%s",
        source.source,
        " [BUILT-IN FALLBACK]" if source.fallback else "",
    )
    logger.info("Validation profiles (%d): %s", len(plan), ", ".join(plan))
    for group in groups:
        logger.info("Validation chain: %s", " -> ".join(group))
    return source, groups


def _save_result(
    db: Database, dataset_id: str, validator_url: str, result, execution_status: str,
) -> None:
    db.save_validation_result(
        dataset_id=dataset_id,
        validator_url=validator_url,
        shape_model=result.shape_model,
        conforms=result.conforms,
        results=result.results,
        violations=result.violations,
        warnings=result.warnings,
        infos=result.infos,
        http_status=result.http_status,
        error=result.error,
        execution_status=execution_status,
        report_body=result.report_body,
        report_content_type=result.report_content_type,
        report_parsed=result.report_metadata_json is not None,
        report_metadata_json=result.report_metadata_json,
        validation_details=result.validation_details,
    )


def _save_skipped(db: Database, dataset_id: str, validator_url: str, profile: str, prerequisite: str) -> None:
    from mqa_cv.validator import ValidationResult

    result = ValidationResult(
        shape_model=profile,
        conforms=None,
        results=0,
        violations=0,
        warnings=0,
        infos=0,
        http_status=None,
        error=(
            f"SKIPPED: prerequisite profile {prerequisite} returned conforms=false; "
            "higher-level validation was not executed"
        ),
    )
    _save_result(db, dataset_id, validator_url, result, "skipped")


def _run_chain(
    db: Database,
    validator: ShaclValidatorClient,
    dataset_id: str,
    validator_url: str,
    rdf_bytes: bytes,
    chain: list[str],
) -> bool:
    """Run a Level 1 -> 2 -> 3 -> base chain with short-circuiting.

    Returns True when the chain reached a terminal state: either the last
    applicable profile was validated, or a higher level was intentionally
    skipped because a prerequisite was non-conformant. Returns False if a
    validator/network/report error prevents deciding the prerequisite.
    """
    for index, profile in enumerate(chain):
        logger.info("[%s] SHACL validation: %s", dataset_id, profile)
        result = validator.validate(rdf_data=rdf_bytes, shape_model=profile)

        if result.http_status == 200 and result.conforms is not None and not result.error:
            _save_result(db, dataset_id, validator_url, result, "validated")
        else:
            _save_result(db, dataset_id, validator_url, result, "error")
            logger.error(
                "[%s] profile %s failed as a validator operation: %s",
                dataset_id,
                profile,
                result.error or f"HTTP {result.http_status}; conforms={result.conforms}",
            )
            return False

        if result.conforms is False:
            # The Level 1/2/3/base chain is monotonic for MQA's shape levels:
            # higher levels add constraints. Do not execute impossible-to-pass
            # continuations once the current prerequisite is non-conformant.
            for skipped_profile in chain[index + 1 :]:
                _save_skipped(db, dataset_id, validator_url, skipped_profile, profile)
                logger.info(
                    "[%s] skipping %s because %s is non-conformant",
                    dataset_id,
                    skipped_profile,
                    profile,
                )
            return True

    return True


def run_validation(limit: int | None = None) -> int:
    """Validate already-fetched RDF using the local SHACL validator only."""
    cfg = Config()
    if os.environ.get("MQA_SHACL_VALIDATOR_URL"):
        cfg.validator_url = os.environ["MQA_SHACL_VALIDATOR_URL"]
    _, db = _clients(cfg)
    _, groups = _load_validation_profiles(cfg)
    profiles = [profile for group in groups for profile in group]

    validator = ShaclValidatorClient(endpoint=cfg.validator_url, timeout=cfg.validator_timeout)
    rows = db.get_pending_validation(cfg.validator_url, profiles, limit)
    logger.info("SHACL validation queue: %d dataset(s)", len(rows))
    completed = 0

    for idx, row in enumerate(rows, 1):
        dataset_id = row["dataset_id"]
        rdf_path = row["rdf_path"]
        logger.info("[run %d/%d] %s", idx, len(rows), dataset_id)

        db.update_validation_status(dataset_id, "validating", None)

        try:
            with open(rdf_path, "rb") as f:
                rdf_bytes = f.read()

            all_chains_terminal = True
            for chain in groups:
                if not _run_chain(db, validator, dataset_id, cfg.validator_url, rdf_bytes, chain):
                    all_chains_terminal = False
                    # An operation error is not equivalent to non-conformity;
                    # continue with independent DCAT versions so other results
                    # are still collected for the experiment.
                    continue

            if all_chains_terminal and db.mark_completed_if_validated(dataset_id, cfg.validator_url, profiles):
                completed += 1
            else:
                db.update_validation_status(
                    dataset_id,
                    "validation_error",
                    "One or more profile validation operations failed; retry required",
                )
                logger.warning(
                    "Validation incomplete for %s; it remains eligible for retry",
                    dataset_id,
                )
        except Exception as exc:
            logger.exception("Validation failed for %s", dataset_id)
            db.update_validation_status(dataset_id, "validation_error", str(exc))

    logger.info("Validation completed: successful=%d failed/incomplete=%d", completed, len(rows) - completed)
    return completed


def show_report_details(
    dataset_id: str | None = None, profile: str | None = None,
    severity: str | None = None, limit: int = 50, raw_report: bool = False,
) -> int:
    """Print indexed result details or the original decompressed report to stdout."""
    cfg = Config()
    db = Database(db_path=cfg.db_path)
    db.init_schema()
    if raw_report:
        if not dataset_id or not profile:
            raise SystemExit("--raw-report richiede sia --dataset-id sia --profile")
        row = db.get_raw_report(dataset_id, profile)
        if row is None or row["report_body_gzip"] is None:
            raise SystemExit(f"Nessun report salvato per dataset={dataset_id}, profile={profile}")
        sys.stdout.buffer.write(gzip.decompress(row["report_body_gzip"]))
        return 1

    rows = db.get_validation_details(dataset_id, profile, severity, limit)
    payload = []
    for row in rows:
        item = dict(row)
        try:
            item["properties"] = json.loads(item.pop("properties_json"))
        except (TypeError, json.JSONDecodeError):
            item["properties"] = {}
        payload.append(item)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("Displayed %d individual SHACL result(s)", len(payload))
    return len(payload)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MQA Cross-Version Validator: discovery, RDF acquisition and SHACL validation as separate commands"
    )
    subparsers = parser.add_subparsers(dest="command")

    discover_parser = subparsers.add_parser(
        "discover",
        help="Recupera solo gli ID dei dataset tramite Hub Search e li salva nell'inventario SQLite",
    )
    discover_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Limita gli ID persistiti dopo il recupero completo dall'API "
            "(utile per test; non riduce il tempo della richiesta HTTP)"
        ),
    )
    discover_parser.add_argument(
        "--catalogue",
        default=None,
        help="Hub catalogue ID (default: dati-gov-it)",
    )

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Scarica esclusivamente i dataset già presenti nell'inventario",
    )
    fetch_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Numero massimo di dataset RDF da scaricare",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Valida esclusivamente i dataset RDF già scaricati tramite il validator SHACL locale",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Numero massimo di dataset da validare",
    )

    report_parser = subparsers.add_parser(
        "reports",
        help="Interroga i singoli risultati SHACL memorizzati nel database e stampa JSON su stdout",
    )
    report_parser.add_argument("--dataset-id", default=None, help="Filtra per ID del dataset")
    report_parser.add_argument("--profile", default=None, help="Filtra per shapeModel, ad esempio dcatap301level1")
    report_parser.add_argument("--severity", choices=["Violation", "Warning", "Info"], default=None, help="Filtra per severità SHACL")
    report_parser.add_argument("--limit", type=int, default=50, help="Numero massimo di ValidationResult mostrati (default: 50, massimo: 10000)")
    report_parser.add_argument("--raw-report", action="store_true", help="Stampa il corpo originale del report decompresso; richiede --dataset-id e --profile")

    args = parser.parse_args()

    if args.command in {"discover", "fetch", "run", "reports"}:
        if args.limit is not None and args.limit < 1:
            parser.error("--limit deve essere maggiore di zero")

    if args.command == "discover":
        discover_datasets(catalogue_id=args.catalogue, limit=args.limit)
    elif args.command == "fetch":
        fetch_datasets(limit=args.limit)
    elif args.command == "run":
        run_validation(limit=args.limit)
    elif args.command == "reports":
        show_report_details(args.dataset_id, args.profile, args.severity, args.limit, args.raw_report)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
