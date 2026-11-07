import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote

from src.config import Config
from src.db import Database
from src.hub import HubClient
from src.profiles import (
    ProfileSource,
    discover_profiles,
    ordered_validation_groups,
    ordered_validation_plan,
)
from src.validator import ShaclValidatorClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _clients(cfg: Config):
    hub = HubClient(
        search_base_url=cfg.search_base_url,
        repo_base_url=cfg.repo_base_url,
        timeout=cfg.timeout,
    )
    db = Database(db_path=cfg.db_path, report_store_path=cfg.report_store_path)
    db.init_schema()
    return hub, db


def list_categories(catalogue_id: str | None = None) -> None:
    """Print category IDs and labels discovered dynamically from Hub Search."""
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id
    hub = HubClient(
        search_base_url=cfg.search_base_url,
        repo_base_url=cfg.repo_base_url,
        timeout=cfg.timeout,
    )
    categories = hub.get_categories(cfg.catalogue_id)
    print("ID\tDATASET COUNT\tCATEGORY")
    for category in categories:
        print(f"{category['id']}\t{category['count']}\t{category['label']}")


def discover_datasets(
    catalogue_id: str | None = None,
    category_id: str | list[str] | None = None,
    limit: int | None = None,
) -> None:
    """Discover dataset IDs in one request, optionally restricted to categories."""
    cfg = Config()
    if catalogue_id:
        cfg.catalogue_id = catalogue_id

    hub, db = _clients(cfg)
    requested_categories = []
    if isinstance(category_id, str):
        requested_categories = [part.strip() for part in category_id.split(",") if part.strip()]
    elif category_id:
        requested_categories = [part.strip() for value in category_id for part in value.split(",") if part.strip()]

    selected_categories = []
    if requested_categories:
        available = hub.get_categories(cfg.catalogue_id)
        for requested in requested_categories:
            category = next(
                (item for item in available
                 if item["id"].casefold() == requested.casefold()
                 or item["label"].casefold() == requested.casefold()),
                None,
            )
            if category is None:
                choices = ", ".join(f"{item['id']} ({item['label']})" for item in available)
                raise SystemExit(f"Categoria non riconosciuta: {requested}. Disponibili: {choices}")
            if category["id"] not in [item["id"] for item in selected_categories]:
                selected_categories.append(category)

        selected_ids = [item["id"] for item in selected_categories]
        category_count = sum(item["count"] for item in selected_categories)
        logger.info(
            "Discovering dataset IDs in one request: catalogue=%s categories=%s",
            cfg.catalogue_id,
            ", ".join(f"{item['id']} ({item['label']})" for item in selected_categories),
        )
        ids = hub.get_dataset_ids_by_categories(
            cfg.catalogue_id,
            selected_ids,
            limit=limit,
            category_count=category_count,
        )
        selected_category_id = ",".join(selected_ids)
        category_label = ",".join(selected_ids)
    else:
        logger.info("Discovering dataset IDs in one request: catalogue=%s category=all", cfg.catalogue_id)
        all_ids = hub.get_dataset_ids(cfg.catalogue_id)
        ids = all_ids[:limit] if limit is not None else all_ids
        selected_category_id = None
        category_label = "all"

    inserted, total = db.add_discovered_datasets(
        ids, cfg.catalogue_id, category_id=selected_category_id
    )
    logger.info(
        "Discovery completed: category=%s received=%d new=%d inventory_total=%d",
        category_label, len(ids), inserted, total,
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
                source_url=f"{cfg.repo_base_url}/resources/{quote(dataset_id, safe='-~')}",
                rdf_path=str(rdf_path) if rdf_path.exists() else None,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                error=str(exc),
            )

    logger.info("Fetch completed: successful=%d failed=%d", completed, len(rows) - completed)
    return completed


def _load_validation_profiles(cfg: Config) -> tuple[ProfileSource, list[list[str]]]:
    source = discover_profiles(cfg.validator_openapi_path)
    groups = ordered_validation_groups(source.profiles)
    plan = ordered_validation_plan(source.profiles)

    logger.info(
        "Validation profile source: %s",
        source.source,
    )
    logger.info("Validation profiles (%d): %s", len(plan), ", ".join(plan))
    for group in groups:
        logger.info("Validation chain: %s", " -> ".join(group))
    return source, groups


def _save_result(
    db: Database, dataset_id: str, validator_url: str, result, execution_status: str,
) -> bool:
    try:
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
            report_parsed=result.report_parsed,
            report_format=result.report_format,
        )
        return True
    except Exception:
        logger.exception(
            "[%s] profile %s: errore nel salvataggio dei metadati/report",
            dataset_id, result.shape_model,
        )
        return False


def _save_skipped(db: Database, dataset_id: str, validator_url: str, profile: str, prerequisite: str) -> None:
    from src.validator import ValidationResult

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
        logger.debug("[%s] SHACL validation: %s", dataset_id, profile)
        result = validator.validate(rdf_data=rdf_bytes, shape_model=profile)

        if result.http_status == 200 and result.conforms is not None and not result.error:
            if not _save_result(db, dataset_id, validator_url, result, "validated"):
                return False
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


def run_validation(limit: int | None = None, short_circuit: bool = False) -> int:
    """Validate fetched RDF; run every profile unless short-circuit is enabled."""
    cfg = Config()
    if os.environ.get("MQA_SHACL_VALIDATOR_URL"):
        cfg.validator_url = os.environ["MQA_SHACL_VALIDATOR_URL"]

    _, db = _clients(cfg)
    _, groups = _load_validation_profiles(cfg)
    profiles = [profile for group in groups for profile in group]
    validator = ShaclValidatorClient(
        endpoint=cfg.validator_url,
        timeout=cfg.validator_timeout,
    )

    rows = db.get_pending_validation(
        cfg.validator_url, profiles, limit, allow_skipped=short_circuit
    )
    mode = "short-circuit" if short_circuit else "full-validation"
    logger.info(
        "SHACL validation queue: %d dataset(s); execution=sequential; mode=%s",
        len(rows), mode,
    )
    completed = 0
    started = time.monotonic()

    for idx, row in enumerate(rows, 1):
        dataset_id = row["dataset_id"]
        rdf_path = row["rdf_path"]
        logger.info("[run %d/%d] %s", idx, len(rows), dataset_id)
        db.update_validation_status(dataset_id, "validating", None)

        try:
            with open(rdf_path, "rb") as f:
                rdf_bytes = f.read()

            all_operations_terminal = True
            if short_circuit:
                for chain in groups:
                    if not _run_chain(db, validator, dataset_id, cfg.validator_url, rdf_bytes, chain):
                        all_operations_terminal = False
                        # A technical error is not non-conformity. Continue with other
                        # independent profile chains so the run preserves useful results.
                        continue
            else:
                for profile in profiles:
                    logger.debug("[%s] SHACL validation: %s", dataset_id, profile)
                    result = validator.validate(rdf_data=rdf_bytes, shape_model=profile)
                    if result.http_status == 200 and result.conforms is not None and not result.error:
                        if not _save_result(db, dataset_id, cfg.validator_url, result, "validated"):
                            all_operations_terminal = False
                    else:
                        all_operations_terminal = False
                        _save_result(db, dataset_id, cfg.validator_url, result, "error")
                        logger.error(
                            "[%s] profile %s failed as a validator operation: %s",
                            dataset_id,
                            profile,
                            result.error or f"HTTP {result.http_status}; conforms={result.conforms}",
                        )

            if all_operations_terminal and db.mark_completed_if_validated(
                dataset_id, cfg.validator_url, profiles, allow_skipped=short_circuit
            ):
                completed += 1
            else:
                db.update_validation_status(
                    dataset_id,
                    "validation_error",
                    "One or more profile validation operations failed; retry required",
                )
                logger.warning("Validation incomplete for %s; eligible for retry", dataset_id)
        except Exception as exc:
            logger.exception("Validation failed for %s", dataset_id)
            db.update_validation_status(dataset_id, "validation_error", str(exc))

        if idx % 10 == 0 or idx == len(rows):
            elapsed = max(time.monotonic() - started, 0.001)
            rate = idx / elapsed
            remaining = len(rows) - idx
            eta = remaining / rate if rate else 0
            logger.info(
                "Validation progress: %d/%d; completed=%d; elapsed=%.1f min; ETA=%.1f min",
                idx, len(rows), completed, elapsed / 60, eta / 60,
            )

    elapsed = time.monotonic() - started
    logger.info(
        "Validation completed: successful=%d failed/incomplete=%d; "
        "execution=sequential; mode=%s; elapsed=%.1f min",
        completed, len(rows) - completed, mode, elapsed / 60,
    )
    return completed


def _rdf_term_json(term):
    from rdflib import BNode, Literal, URIRef

    if isinstance(term, URIRef):
        value = {"term_type": "uri", "value": str(term)}
    elif isinstance(term, BNode):
        value = {"term_type": "bnode", "value": str(term)}
    elif isinstance(term, Literal):
        value = {
            "term_type": "literal",
            "value": str(term),
            "datatype": str(term.datatype) if term.datatype else None,
            "language": term.language,
        }
    else:
        value = {"term_type": type(term).__name__, "value": str(term)}
    return value


def _local_iri_name(value):
    return str(value).rsplit("#", 1)[-1].rsplit("/", 1)[-1] if value else None


def show_report_details(
    dataset_id: str | None = None, profile: str | None = None,
    severity: str | None = None, limit: int = 50, raw_report: bool = False,
) -> int:
    """Read report data from Oxigraph; SQLite is used only for report metadata."""
    from rdflib import RDF
    from rdflib.namespace import SH

    cfg = Config()
    db = Database(db_path=cfg.db_path, report_store_path=cfg.report_store_path)
    db.init_schema()
    if raw_report and (not dataset_id or not profile):
        raise SystemExit("--raw-report richiede sia --dataset-id sia --profile")

    validations = db.get_validation_report_rows(
        successful_only=True, dataset_id=dataset_id, profile=profile
    )
    if not validations:
        raise SystemExit("Nessun report SHACL corrispondente trovato nel triplestore")

    store = db.get_report_store(read_only=True)
    if raw_report:
        validation = validations[0]
        body = store.dump_report(validation["report_graph_iri"], validation["report_format"] or "turtle")
        sys.stdout.buffer.write(body)
        return 1

    payload = []
    max_results = max(1, min(limit, 10000))
    for validation in validations:
        graph = store.get_report_graph(validation["report_graph_iri"])
        nodes = sorted(graph.subjects(RDF.type, SH.ValidationResult), key=lambda node: node.n3())
        for node in nodes:
            severities = list(graph.objects(node, SH.resultSeverity))
            result_severity = _local_iri_name(severities[0]) if severities else None
            if severity and (result_severity or "").casefold() != severity.casefold():
                continue
            properties = {}
            for predicate, obj in graph.predicate_objects(node):
                properties.setdefault(str(predicate), []).append(_rdf_term_json(obj))
            for values in properties.values():
                values.sort(key=lambda item: (
                    item.get("value", ""), item.get("datatype") or "", item.get("language") or ""
                ))

            def first_value(predicate):
                values = list(graph.objects(node, predicate))
                return _rdf_term_json(values[0]) if values else None

            components = list(graph.objects(node, SH.sourceConstraintComponent))
            messages = sorted(str(value) for value in graph.objects(node, SH.resultMessage))
            payload.append({
                "validation_id": validation["id"],
                "dataset_id": validation["dataset_id"],
                "shape_model": validation["shape_model"],
                "conforms": validation["conforms"],
                "severity": result_severity,
                "focus_node": first_value(SH.focusNode),
                "result_path": first_value(SH.resultPath),
                "value": first_value(SH.value),
                "source_shape": first_value(SH.sourceShape),
                "source_constraint_component": _local_iri_name(components[0]) if components else None,
                "message": messages,
                "properties": properties,
            })
            if len(payload) >= max_results:
                break
        if len(payload) >= max_results:
            break

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("Displayed %d individual SHACL result(s)", len(payload))
    return len(payload)



def run_sparql(query: str, output_format: str = "tsv") -> None:
    """Execute a read-only SPARQL query against the PyOxigraph store."""
    from src.report_store import ValidationReportStore

    cfg = Config()
    store = ValidationReportStore(cfg.report_store_path, read_only=True)
    result = store.query(query)
    if hasattr(result, "variables"):
        variables = [str(variable) for variable in result.variables]
        rows = [
            {name: (str(solution[variable]) if solution[variable] is not None else None)
             for name, variable in zip(variables, result.variables)}
            for solution in result
        ]
        if output_format == "json":
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print("\t".join(variables))
            for row in rows:
                print("\t".join((row[name] or "").replace("\t", " ").replace("\n", " ") for name in variables))
    elif type(result).__name__ == "QueryBoolean":
        print("true" if bool(result) else "false")
    else:
        raise SystemExit("Il comando sparql supporta query SELECT e ASK; per CONSTRUCT usare Oxigraph Server/YASGUI.")

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DCAT-AP Cross-Version Validator for EU MQA: cross-version validation using the EU MQA SHACL validator"
    )
    subparsers = parser.add_subparsers(dest="command")

    discover_parser = subparsers.add_parser(
        "discover",
        help="Recupera solo gli ID dei dataset tramite Hub Search e li salva nell'inventario SQLite",
    )
    discover_parser.add_argument(
        "--limit", type=int, default=None,
        help="Numero massimo di dataset da inserire",
    )
    discover_parser.add_argument(
        "--catalogue", default=None,
        help="Hub catalogue ID (default: dati-gov-it)",
    )
    discover_parser.add_argument(
        "--category", action="append", default=None,
        help="Categoria Hub Search (ripetibile; ID come ENVI o etichetta come Ambiente; accetta anche valori separati da virgola)",
    )

    categories_parser = subparsers.add_parser(
        "categories", help="Elenca dinamicamente le categorie disponibili nel catalogo",
    )
    categories_parser.add_argument(
        "--catalogue", default=None, help="Hub catalogue ID (default: dati-gov-it)",
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
    run_parser.add_argument(
        "--short-circuit",
        action="store_true",
        help="Abilita l'interruzione dei livelli successivi quando un livello precedente non è conforme",
    )

    report_parser = subparsers.add_parser(
        "reports",
        help="Interroga i report SHACL memorizzati nel triplestore e stampa JSON su stdout",
    )
    report_parser.add_argument("--dataset-id", default=None, help="Filtra per ID del dataset")
    report_parser.add_argument("--profile", default=None, help="Filtra per shapeModel, ad esempio dcatap301level1")
    report_parser.add_argument("--severity", choices=["Violation", "Warning", "Info"], default=None, help="Filtra per severità SHACL")
    report_parser.add_argument("--limit", type=int, default=50, help="Numero massimo di ValidationResult mostrati (default: 50, massimo: 10000)")
    report_parser.add_argument("--raw-report", action="store_true", help="Serializza il report dal triplestore su stdout; richiede --dataset-id e --profile")

    sparql_parser = subparsers.add_parser(
        "sparql", help="Esegue una query SPARQL SELECT/ASK sul triplestore (sola lettura)",
    )
    sparql_source = sparql_parser.add_mutually_exclusive_group(required=True)
    sparql_source.add_argument("--query-file", type=str, help="File .sparql da eseguire")
    sparql_source.add_argument("--query", type=str, help="Query SPARQL passata direttamente")
    sparql_parser.add_argument("--format", choices=["tsv", "json"], default="tsv", help="Formato dell'output SELECT (default: tsv)")

    args = parser.parse_args()

    if args.command in {"discover", "fetch", "run", "reports"}:
        if args.limit is not None and args.limit < 1:
            parser.error("--limit deve essere maggiore di zero")

    if args.command == "discover":
        discover_datasets(catalogue_id=args.catalogue, category_id=args.category, limit=args.limit)
    elif args.command == "categories":
        list_categories(catalogue_id=args.catalogue)
    elif args.command == "fetch":
        fetch_datasets(limit=args.limit)
    elif args.command == "run":
        run_validation(limit=args.limit, short_circuit=args.short_circuit)
    elif args.command == "reports":
        show_report_details(args.dataset_id, args.profile, args.severity, args.limit, args.raw_report)
    elif args.command == "sparql":
        query = Path(args.query_file).read_text(encoding="utf-8") if args.query_file else args.query
        run_sparql(query, args.format)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
