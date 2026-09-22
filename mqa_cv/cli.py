import argparse
import hashlib
import logging

from mqa_cv.config import Config
from mqa_cv.db import Database
from mqa_cv.hub import HubClient
from mqa_cv.profiles import PROFILES
from mqa_cv.reporting import generate_dataset_reports, generate_summary
from mqa_cv.validator import ShaclValidatorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def run_pipeline(
    limit: int | None = None,
    catalogue_id: str | None = None,
) -> None:
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id

    cfg.rdf_dir.mkdir(parents=True, exist_ok=True)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)

    hub = HubClient(
        search_base_url=cfg.search_base_url,
        repo_base_url=cfg.repo_base_url,
        timeout=cfg.timeout,
    )
    validator = ShaclValidatorClient(endpoint=cfg.validator_url)
    db = Database(db_path=cfg.db_path)
    db.init_schema()

    logger.info(
        "Dataset source: Piveau Hub Search catalogue='%s'",
        cfg.catalogue_id,
    )
    logger.info("RDF source: Piveau Hub Repo API at %s", cfg.repo_base_url)
    logger.info("SHACL validator: %s", cfg.validator_url)

    dataset_ids = list(
        hub.iter_dataset_ids(
            catalogue_id=cfg.catalogue_id,
            page_size=cfg.page_size,
            limit=limit,
        )
    )
    logger.info(
        "Search API returned %d dataset IDs for catalogue '%s'.",
        len(dataset_ids), cfg.catalogue_id,
    )

    for idx, dataset_id in enumerate(dataset_ids, 1):
        logger.info("[%d/%d] Processing dataset: %s", idx, len(dataset_ids), dataset_id)

        safe_id = dataset_id.replace("/", "_").replace(":", "_")
        rdf_path = cfg.rdf_dir / f"{safe_id}.ttl"

        try:
            # 1. Retrieve RDF metadata from Repo API.
            rdf_content, source_url = hub.fetch_dataset_rdf(dataset_id)
            rdf_bytes = rdf_content.encode("utf-8")
            rdf_path.write_bytes(rdf_bytes)
            sha256_hash = hashlib.sha256(rdf_bytes).hexdigest()

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

            # 2. Validate against every configured SHACL profile.
            for profile in PROFILES:
                logger.info("[%s] SHACL validation: %s", dataset_id, profile)
                res = validator.validate(
                    rdf_data=rdf_bytes,
                    shape_model=profile,
                )
                db.save_validation_result(
                    dataset_id=dataset_id,
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

        except Exception as e:
            logger.exception("Error processing dataset '%s'", dataset_id)
            db.save_dataset(
                dataset_id=dataset_id,
                catalogue_id=cfg.catalogue_id,
                source_url=f"{cfg.repo_base_url}/datasets/{dataset_id}",
                rdf_path=str(rdf_path) if rdf_path.exists() else "",
                sha256="",
                fetch_status="error",
                fetch_http_status=500,
                fetch_error=str(e),
            )

    logger.info("Generating reports...")
    with db.get_connection() as conn:
        generate_dataset_reports(conn=conn, report_dir=cfg.reports_dir)
        generate_summary(conn=conn, report_dir=cfg.reports_dir)
    logger.info("Pipeline completed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MQA Cross-Version Validator")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Esegue recupero e validazione")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument(
        "--catalogue",
        default=None,
        help="Hub catalogue ID (default: dati-gov-it)",
    )

    args = parser.parse_args()

    if args.command == "run":
        run_pipeline(limit=args.limit, catalogue_id=args.catalogue)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
