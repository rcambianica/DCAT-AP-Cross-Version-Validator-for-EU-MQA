import argparse
import hashlib
import logging

from mqa_cv.config import Config
from mqa_cv.db import Database
from mqa_cv.hub import HubClient
from mqa_cv.profiles import PROFILES
from mqa_cv.reporting import generate_dataset_reports, generate_summary
from mqa_cv.validator import ShaclValidatorClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _clients(cfg: Config):
    hub = HubClient(search_base_url=cfg.search_base_url, repo_base_url=cfg.repo_base_url, timeout=cfg.timeout)
    db = Database(db_path=cfg.db_path)
    db.init_schema()
    return hub, db


def discover_datasets(catalogue_id: str | None = None, limit: int | None = None) -> None:
    """Refresh the persistent ID inventory; does not fetch RDF or validate."""
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id
    hub, db = _clients(cfg)
    logger.info("Discovering dataset IDs: catalogue=%s", cfg.catalogue_id)
    ids = list(hub.iter_dataset_ids(catalogue_id=cfg.catalogue_id, page_size=cfg.page_size, limit=limit))
    inserted, total = db.add_discovered_datasets(ids, cfg.catalogue_id)
    logger.info("Discovery completed: received=%d new=%d inventory_total=%d", len(ids), inserted, total)


def _fetch_pending(cfg: Config, hub: HubClient, db: Database, limit: int | None) -> int:
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
            db.update_fetch(dataset_id, status="ok", source_url=source_url, rdf_path=str(rdf_path), sha256=digest, http_status=200, error=None)
            completed += 1
        except Exception as exc:
            logger.exception("RDF fetch failed for %s", dataset_id)
            db.update_fetch(dataset_id, status="fetch_error", source_url=f"{cfg.repo_base_url}/datasets/{dataset_id}",
                            rdf_path=str(rdf_path) if rdf_path.exists() else None, http_status=getattr(getattr(exc, "response", None), "status_code", None), error=str(exc))
    return completed


def _validate_pending(cfg: Config, db: Database, limit: int | None) -> int:
    validator = ShaclValidatorClient(endpoint=cfg.validator_url)
    rows = db.get_pending_validation(limit)
    logger.info("SHACL validation queue: %d dataset(s)", len(rows))
    completed = 0
    for idx, row in enumerate(rows, 1):
        dataset_id = row["dataset_id"]
        rdf_path = row["rdf_path"]
        logger.info("[validate %d/%d] %s", idx, len(rows), dataset_id)
        try:
            rdf_bytes = open(rdf_path, "rb").read()
            for profile in PROFILES:
                logger.info("[%s] SHACL validation: %s", dataset_id, profile)
                res = validator.validate(rdf_data=rdf_bytes, shape_model=profile)
                db.save_validation_result(dataset_id=dataset_id, validator_url=cfg.validator_url,
                    shape_model=res.shape_model, conforms=res.conforms, results=res.results,
                    violations=res.violations, warnings=res.warnings, infos=res.infos,
                    http_status=res.http_status, error=res.error)
            if db.mark_completed_if_validated(dataset_id, cfg.validator_url, list(PROFILES)):
                completed += 1
            else:
                logger.warning("Validation incomplete for %s; it remains eligible for retry", dataset_id)
        except Exception as exc:
            logger.exception("Validation failed for %s", dataset_id)
            db.update_validation_status(dataset_id, "validation_error", str(exc))
    return completed


def _generate_reports(cfg: Config, db: Database) -> None:
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with db.get_connection() as conn:
        generate_dataset_reports(conn=conn, report_dir=cfg.reports_dir)
        generate_summary(conn=conn, report_dir=cfg.reports_dir)


def run_pipeline(limit: int | None = None, catalogue_id: str | None = None, refresh_inventory: bool = True) -> None:
    """Refresh inventory (unless disabled), then incrementally fetch and validate pending IDs."""
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id
    cfg.rdf_dir.mkdir(parents=True, exist_ok=True)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    hub, db = _clients(cfg)

    logger.info("Dataset source: Hub Search catalogue='%s'", cfg.catalogue_id)
    logger.info("RDF source: Hub Repo API %s", cfg.repo_base_url)
    logger.info("SHACL validator: %s", cfg.validator_url)

    if refresh_inventory:
        logger.info("Refreshing inventory before incremental processing...")
        ids = list(hub.iter_dataset_ids(catalogue_id=cfg.catalogue_id, page_size=cfg.page_size))
        inserted, total = db.add_discovered_datasets(ids, cfg.catalogue_id)
        logger.info("Inventory refreshed: discovered=%d new=%d total=%d", len(ids), inserted, total)
    else:
        logger.info("Inventory refresh skipped; using IDs already stored in SQLite.")

    fetched = _fetch_pending(cfg, hub, db, limit)
    validated = _validate_pending(cfg, db, limit)
    logger.info("Run summary: fetched_now=%d validated_now=%d", fetched, validated)
    _generate_reports(cfg, db)
    logger.info("Pipeline completed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MQA Cross-Version Validator")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Aggiorna inventario e processa dataset in modo incrementale")
    run_parser.add_argument("--limit", type=int, default=None, help="Massimo numero di dataset per fase (fetch/validation)")
    run_parser.add_argument("--catalogue", default=None, help="Hub catalogue ID (default: dati-gov-it)")
    run_parser.add_argument("--no-discover", action="store_true", help="Non aggiorna la lista; usa solo l'inventario già presente in SQLite")

    discover_parser = subparsers.add_parser("discover", help="Aggiorna solo l'inventario degli ID, senza scaricare RDF")
    discover_parser.add_argument("--limit", type=int, default=None, help="Limita gli ID acquisiti (utile per test)")
    discover_parser.add_argument("--catalogue", default=None, help="Hub catalogue ID (default: dati-gov-it)")

    args = parser.parse_args()
    if args.command == "run":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit deve essere maggiore di zero")
        run_pipeline(limit=args.limit, catalogue_id=args.catalogue, refresh_inventory=not args.no_discover)
    elif args.command == "discover":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit deve essere maggiore di zero")
        discover_datasets(catalogue_id=args.catalogue, limit=args.limit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
