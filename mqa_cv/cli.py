from __future__ import annotations

import argparse
from pathlib import Path

from .config import Config
from .db import connect
from .hub import HubClient, save_rdf, sha256_bytes
from .profiles import discover_profiles, level1_profiles
from .reporting import generate_dataset_reports, generate_summary
from .validator import ShaclValidatorClient


def build_config(args) -> Config:
    openapi_path = Path(args.openapi) if args.openapi else None
    return Config(
        hub_base_url=args.hub_base_url,
        catalogue_id=args.catalogue,
        validator_url=args.validator,
        openapi_path=openapi_path,
        db_path=Path(args.db),
        rdf_dir=Path(args.rdf_dir),
        report_dir=Path(args.report_dir),
        request_timeout=args.timeout,
        page_size=args.page_size,
        request_delay=args.request_delay,
        request_retries=args.retries,
    )


def discover(cfg: Config, limit: int | None) -> None:
    client = HubClient(cfg.hub_base_url, cfg.request_timeout)
    conn = connect(cfg.db_path)
    count = 0
    try:
        for dataset_id in client.iter_dataset_ids(
            cfg.catalogue_id,
            page_size=cfg.page_size,
            limit=limit,
        ):
            conn.execute(
                """
                INSERT INTO datasets(dataset_id, catalogue_id)
                VALUES (?, ?)
                ON CONFLICT(dataset_id) DO NOTHING
                """,
                (dataset_id, cfg.catalogue_id),
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Dataset discovered in this run: {count}")


def fetch(cfg: Config, retry_errors: bool = False) -> None:
    client = HubClient(cfg.hub_base_url, cfg.request_timeout)
    conn = connect(cfg.db_path)

    condition = "rdf_path IS NULL"
    if not retry_errors:
        condition += " AND (fetch_status IS NULL OR fetch_status != 'error')"

    rows = conn.execute(
        f"SELECT dataset_id FROM datasets WHERE {condition} ORDER BY dataset_id"
    ).fetchall()

    for idx, row in enumerate(rows, start=1):
        dataset_id = row["dataset_id"]
        try:
            status, data = client.fetch_turtle(dataset_id)
            path = save_rdf(cfg.rdf_dir, dataset_id, data)
            conn.execute(
                """
                UPDATE datasets
                SET source_url = ?, rdf_path = ?, sha256 = ?, fetch_status = 'ok',
                    fetch_http_status = ?, fetch_error = NULL, fetched_at = CURRENT_TIMESTAMP
                WHERE dataset_id = ?
                """,
                (
                    f"{cfg.hub_base_url}/datasets/{dataset_id}",
                    str(path),
                    sha256_bytes(data),
                    status,
                    dataset_id,
                ),
            )
            print(f"[{idx}/{len(rows)}] fetched {dataset_id}")
        except Exception as exc:
            conn.execute(
                """
                UPDATE datasets
                SET fetch_status = 'error', fetch_error = ?, fetched_at = CURRENT_TIMESTAMP
                WHERE dataset_id = ?
                """,
                (str(exc), dataset_id),
            )
            print(f"[{idx}/{len(rows)}] ERROR {dataset_id}: {exc}")
        conn.commit()

    conn.close()


def validate(
    cfg: Config,
    all_profiles: bool,
    revalidate: bool = False,
    explicit_profiles: str | None = None,
) -> None:
    profiles = discover_profiles(cfg.openapi_path)
    if explicit_profiles:
        requested = [p.strip() for p in explicit_profiles.split(",") if p.strip()]
        unknown = [p for p in requested if p not in profiles]
        if unknown:
            raise SystemExit(f"Unknown profile(s): {', '.join(unknown)}")
        profiles = requested
    elif not all_profiles:
        profiles = level1_profiles(profiles)

    if not profiles:
        raise SystemExit("No DCAT-AP profiles selected")

    print(f"Validator: {cfg.validator_url}")
    print("Profiles:")
    for profile in profiles:
        print(f"  - {profile}")

    validator = ShaclValidatorClient(
        cfg.validator_url,
        cfg.request_timeout,
        request_delay=cfg.request_delay,
        retries=cfg.request_retries,
    )
    conn = connect(cfg.db_path)
    datasets = conn.execute(
        "SELECT dataset_id, rdf_path FROM datasets WHERE fetch_status = 'ok' ORDER BY dataset_id"
    ).fetchall()

    total = len(datasets) * len(profiles)
    current = 0

    for dataset in datasets:
        rdf_data = Path(dataset["rdf_path"]).read_bytes()
        for profile in profiles:
            current += 1
            if not revalidate:
                exists = conn.execute(
                    """
                    SELECT 1 FROM validations
                    WHERE dataset_id = ? AND shape_model = ? AND validator_url = ?
                    """,
                    (dataset["dataset_id"], profile, cfg.validator_url),
                ).fetchone()
                if exists:
                    continue

            result = validator.validate(rdf_data, profile)
            conn.execute(
                """
                INSERT INTO validations(
                    dataset_id, shape_model, validator_url, conforms, results,
                    violations, warnings, infos, http_status, error, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(dataset_id, shape_model, validator_url) DO UPDATE SET
                    conforms = excluded.conforms,
                    results = excluded.results,
                    violations = excluded.violations,
                    warnings = excluded.warnings,
                    infos = excluded.infos,
                    http_status = excluded.http_status,
                    error = excluded.error,
                    validated_at = CURRENT_TIMESTAMP
                """,
                (
                    dataset["dataset_id"], profile, cfg.validator_url,
                    None if result.conforms is None else int(result.conforms),
                    result.results, result.violations, result.warnings, result.infos,
                    result.http_status, result.error,
                ),
            )
            conn.commit()
            state = "ERR" if result.conforms is None else ("YES" if result.conforms else "NO")
            print(
                f"[{current}/{total}] {dataset['dataset_id']} {profile}: {state} "
                f"(V={result.violations}, W={result.warnings}, I={result.infos})"
            )

    conn.close()


def report(cfg: Config) -> None:
    conn = connect(cfg.db_path)
    dataset_dir = generate_dataset_reports(conn, cfg.report_dir)
    md_path, csv_path = generate_summary(conn, cfg.report_dir)
    conn.close()
    print(f"Dataset reports: {dataset_dir}")
    print(f"Markdown report: {md_path}")
    print(f"CSV report:      {csv_path}")


def add_common(parser: argparse.ArgumentParser) -> None:
    defaults = Config()
    parser.add_argument("--hub-base-url", default=defaults.hub_base_url)
    parser.add_argument("--catalogue", default=defaults.catalogue_id)
    parser.add_argument(
        "--validator",
        default=defaults.validator_url,
        help=(
            "SHACL validation endpoint. Default: public data.europa.eu API. "
            "For a local instance use http://127.0.0.1:8080/shacl/validation/report"
        ),
    )
    parser.add_argument(
        "--openapi",
        default=None,
        help=(
            "Optional local openapi-shacl.yaml used to discover profiles. "
            "If omitted, built-in profiles are used."
        ),
    )
    parser.add_argument("--db", default=str(defaults.db_path))
    parser.add_argument("--rdf-dir", default=str(defaults.rdf_dir))
    parser.add_argument("--report-dir", default=str(defaults.report_dir))
    parser.add_argument("--timeout", type=int, default=defaults.request_timeout)
    parser.add_argument("--page-size", type=int, default=defaults.page_size)
    parser.add_argument(
        "--request-delay",
        type=float,
        default=defaults.request_delay,
        help="Seconds between SHACL API requests (default: 0.25 for public service)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=defaults.request_retries,
        help="Retries for 429/5xx responses",
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="mqa-cross-version")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover")
    add_common(p_discover)
    p_discover.add_argument("--limit", type=int)

    p_fetch = sub.add_parser("fetch")
    add_common(p_fetch)
    p_fetch.add_argument("--retry-errors", action="store_true")

    p_validate = sub.add_parser("validate")
    add_common(p_validate)
    p_validate.add_argument(
        "--all-profiles",
        action="store_true",
        help="Validate all profiles instead of the default cross-version selection",
    )
    p_validate.add_argument(
        "--profiles",
        help="Comma-separated explicit shapeModel list, e.g. dcatap300level1,dcatap301level1",
    )
    p_validate.add_argument("--revalidate", action="store_true")

    p_report = sub.add_parser("report")
    add_common(p_report)

    p_run = sub.add_parser("run")
    add_common(p_run)
    p_run.add_argument("--limit", type=int, default=10)
    p_run.add_argument("--all-profiles", action="store_true")
    p_run.add_argument(
        "--profiles",
        help="Comma-separated explicit shapeModel list",
    )

    args = parser.parse_args()
    cfg = build_config(args)

    if args.command == "discover":
        discover(cfg, args.limit)
    elif args.command == "fetch":
        fetch(cfg, args.retry_errors)
    elif args.command == "validate":
        validate(cfg, args.all_profiles, args.revalidate, args.profiles)
    elif args.command == "report":
        report(cfg)
    elif args.command == "run":
        discover(cfg, args.limit)
        fetch(cfg)
        validate(cfg, args.all_profiles, explicit_profiles=args.profiles)
        report(cfg)


if __name__ == "__main__":
    main()
