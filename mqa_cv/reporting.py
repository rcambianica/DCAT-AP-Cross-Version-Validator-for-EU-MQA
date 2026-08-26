from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from pathlib import Path

from .profiles import profile_label


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._~-]+", "_", value)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:120]}__{digest}"


def generate_dataset_reports(conn: sqlite3.Connection, report_dir: Path) -> Path:
    dataset_dir = report_dir / "datasets"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    datasets = conn.execute(
        """
        SELECT dataset_id, catalogue_id, source_url, rdf_path, sha256,
               fetch_status, fetch_http_status, fetch_error
        FROM datasets
        ORDER BY dataset_id
        """
    ).fetchall()

    for dataset in datasets:
        validations = conn.execute(
            """
            SELECT validator_url, shape_model, conforms, results, violations,
                   warnings, infos, http_status, error, validated_at
            FROM validations
            WHERE dataset_id = ?
            ORDER BY validator_url, shape_model
            """,
            (dataset["dataset_id"],),
        ).fetchall()

        path = dataset_dir / f"{_safe_name(dataset['dataset_id'])}.md"
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# Dataset `{dataset['dataset_id']}`\n\n")
            f.write(f"- Catalogue: `{dataset['catalogue_id']}`\n")
            f.write(f"- Fetch status: `{dataset['fetch_status']}`\n")
            f.write(f"- Fetch HTTP: `{dataset['fetch_http_status']}`\n")
            f.write(f"- SHA-256: `{dataset['sha256'] or ''}`\n")
            f.write(f"- RDF path: `{dataset['rdf_path'] or ''}`\n")
            f.write(f"- Source: `{dataset['source_url'] or ''}`\n")
            if dataset["fetch_error"]:
                f.write(f"- Fetch error: `{dataset['fetch_error']}`\n")
            f.write("\n")

            if not validations:
                f.write("No validations stored.\n")
                continue

            current_validator = None
            for row in validations:
                if row["validator_url"] != current_validator:
                    current_validator = row["validator_url"]
                    f.write(f"## Validator `{current_validator}`\n\n")
                    f.write("| shapeModel | compliant | V | W | I | results | HTTP |\n")
                    f.write("|---|---:|---:|---:|---:|---:|---:|\n")

                state = (
                    "ERROR"
                    if row["conforms"] is None
                    else ("YES" if row["conforms"] else "NO")
                )
                f.write(
                    f"| {profile_label(row['shape_model'])} (`{row['shape_model']}`) | {state} | {row['violations']} | "
                    f"{row['warnings']} | {row['infos']} | {row['results']} | "
                    f"{row['http_status']} |\n"
                )
                if row["error"]:
                    f.write(
                        f"\nError for `{row['shape_model']}` via `{current_validator}`: "
                        f"`{row['error']}`\n\n"
                    )

    return dataset_dir


def generate_summary(conn: sqlite3.Connection, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT
            validator_url,
            shape_model,
            COUNT(*) AS tested,
            SUM(CASE WHEN conforms = 1 THEN 1 ELSE 0 END) AS compliant,
            SUM(CASE WHEN conforms = 0 THEN 1 ELSE 0 END) AS non_compliant,
            SUM(CASE WHEN conforms IS NULL THEN 1 ELSE 0 END) AS errors,
            SUM(violations) AS violations,
            SUM(warnings) AS warnings,
            SUM(infos) AS infos
        FROM validations
        GROUP BY validator_url, shape_model
        ORDER BY validator_url, shape_model
        """
    ).fetchall()

    fetch = conn.execute(
        """
        SELECT
            COUNT(*) AS discovered,
            SUM(CASE WHEN fetch_status = 'ok' THEN 1 ELSE 0 END) AS fetched,
            SUM(CASE WHEN fetch_status = 'error' THEN 1 ELSE 0 END) AS fetch_errors
        FROM datasets
        """
    ).fetchone()

    csv_path = report_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "validator_url",
                "shape_model",
                "tested",
                "compliant",
                "non_compliant",
                "errors",
                "compliance_pct",
                "violations",
                "warnings",
                "infos",
            ]
        )
        for row in rows:
            denominator = row["compliant"] + row["non_compliant"]
            pct = (100.0 * row["compliant"] / denominator) if denominator else None
            writer.writerow(
                [
                    row["validator_url"],
                    row["shape_model"],
                    row["tested"],
                    row["compliant"],
                    row["non_compliant"],
                    row["errors"],
                    f"{pct:.2f}" if pct is not None else "",
                    row["violations"],
                    row["warnings"],
                    row["infos"],
                ]
            )

    md_path = report_dir / "summary.md"
    with md_path.open("w", encoding="utf-8") as f:
        f.write("# MQA Cross-Version Analysis\n\n")
        f.write("## Acquisition\n\n")
        f.write(f"- Dataset discovered: **{fetch['discovered'] or 0}**\n")
        f.write(f"- RDF fetched: **{fetch['fetched'] or 0}**\n")
        f.write(f"- Fetch errors: **{fetch['fetch_errors'] or 0}**\n\n")

        validators = []
        for row in rows:
            if row["validator_url"] not in validators:
                validators.append(row["validator_url"])

        for validator_url in validators:
            f.write(f"## Validator `{validator_url}`\n\n")
            f.write(
                "| shapeModel | tested | compliant | non compliant | errors | "
                "compliance | V | W | I |\n"
            )
            f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
            for row in rows:
                if row["validator_url"] != validator_url:
                    continue
                denominator = row["compliant"] + row["non_compliant"]
                pct = (100.0 * row["compliant"] / denominator) if denominator else None
                pct_text = f"{pct:.2f}%" if pct is not None else "n/a"
                f.write(
                    f"| {profile_label(row['shape_model'])} (`{row['shape_model']}`) | {row['tested']} | {row['compliant']} | "
                    f"{row['non_compliant']} | {row['errors']} | {pct_text} | "
                    f"{row['violations']} | {row['warnings']} | {row['infos']} |\n"
                )
            f.write("\n")

    return md_path, csv_path
