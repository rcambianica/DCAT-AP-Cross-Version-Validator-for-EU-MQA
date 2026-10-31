"""Read-only analysis and static visualisations of the validator analysis database.

The module reads analysis.db, writes CSV summaries and four publication-oriented
PNG charts. It does not modify the source database.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path
from typing import Any


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database non trovato: {db_path}")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def profile_version(profile: str) -> tuple[str, str] | None:
    """Return (normalized version, profile id) for base DCAT-AP profile IDs."""
    match = re.fullmatch(r"dcatap(\d+)", (profile or "").casefold())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 1:
        version = digits
    elif len(digits) == 2:
        version = f"{digits[0]}.{digits[1]}"
    else:
        # The profile identifier concatenates major, minor and patch digits:
        # dcatap121 -> 1.2.1; dcatap300 -> 3.0.0; dcatap1100 -> 1.1.0.
        version = f"{digits[0]}.{digits[1]}.{int(digits[2:])}"
    return version, profile


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _short_property(value: str | None) -> str:
    """Shorten common RDF namespaces for readable chart labels."""
    if not value:
        return "(percorso non specificato)"
    namespaces = (
        ("http://www.w3.org/ns/dcat#", "dcat:"),
        ("http://www.w3.org/ns/adms#", "adms:"),
        ("http://purl.org/dc/terms/", "dcterms:"),
        ("http://xmlns.com/foaf/0.1/", "foaf:"),
        ("http://www.w3.org/2004/02/skos/core#", "skos:"),
        ("http://www.w3.org/ns/dqv#", "dqv:"),
        ("http://data.europa.eu/r5r/", "r5r:"),
    )
    for namespace, prefix in namespaces:
        if value.startswith(namespace):
            return prefix + value[len(namespace):]
    return value


def _save_bar_chart(plt, output_path: Path, title: str, xlabel: str, ylabel: str) -> None:
    plt.tight_layout()
    plt.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close()


def analyze_database(db_path: Path, output_dir: Path) -> dict[str, Any]:
    """Generate CSV summaries and four static PNG charts from successful validations."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Per generare i grafici installare matplotlib e numpy: "
            "pip install matplotlib numpy"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)

    with connect_readonly(db_path) as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        fetch_counts = conn.execute(
            "SELECT fetch_status, COUNT(*) AS n FROM datasets "
            "GROUP BY fetch_status ORDER BY fetch_status"
        ).fetchall()

        # All summaries use the same population of successfully completed validations.
        validation_rows = conn.execute("""
            SELECT v.id, v.dataset_id, v.shape_model, v.conforms,
                   v.violations, v.warnings, v.infos
            FROM validations v
            JOIN datasets d ON d.dataset_id = v.dataset_id
            WHERE v.execution_status = 'validated'
              AND v.http_status = 200
              AND v.conforms IS NOT NULL
              AND (v.error IS NULL OR TRIM(v.error) = '')
            ORDER BY v.shape_model, v.dataset_id
        """).fetchall()

        profile_stats: dict[str, dict[str, Any]] = {}
        by_dataset_version: dict[tuple[str, str], int] = {}
        for row in validation_rows:
            profile = row["shape_model"] or "(shape model non specificato)"
            stat = profile_stats.setdefault(profile, {
                "profile": profile, "validated": 0, "conforming": 0,
                "non_conforming": 0, "violations": 0, "warnings": 0, "infos": 0,
            })
            stat["validated"] += 1
            stat["conforming"] += int(row["conforms"] == 1)
            stat["non_conforming"] += int(row["conforms"] == 0)
            stat["violations"] += row["violations"] or 0
            stat["warnings"] += row["warnings"] or 0
            stat["infos"] += row["infos"] or 0

            parsed = profile_version(profile)
            if parsed:
                version, _ = parsed
                by_dataset_version[(row["dataset_id"], version)] = int(row["conforms"])

        profile_rows: list[dict[str, Any]] = []
        for stat in sorted(profile_stats.values(), key=lambda item: item["profile"]):
            stat["conformance_pct"] = round(
                100 * stat["conforming"] / stat["validated"], 2
            ) if stat["validated"] else 0
            stat["violation_rate_per_100"] = round(
                100 * stat["violations"] / stat["validated"], 2
            ) if stat["validated"] else 0
            stat["warning_rate_per_100"] = round(
                100 * stat["warnings"] / stat["validated"], 2
            ) if stat["validated"] else 0
            profile_rows.append(stat)

        # Category key: RDF result path + SHACL constraint component.
        # Message/source_shape are deliberately not grouping keys because they
        # can contain dataset-specific RDF terms or generated blank-node IDs.
        issue_rows = conn.execute("""
            SELECT v.shape_model AS profile,
                   vr.severity,
                   vr.result_path,
                   vr.source_constraint_component AS constraint_component,
                   COUNT(*) AS occurrences,
                   COUNT(DISTINCT vr.validation_id) AS affected_validations,
                   COUNT(DISTINCT v.dataset_id) AS affected_datasets,
                   MIN(vr.message) AS example_message
            FROM validation_results vr
            JOIN validations v ON v.id = vr.validation_id
            WHERE v.execution_status = 'validated'
              AND v.http_status = 200
              AND v.conforms IS NOT NULL
              AND (v.error IS NULL OR TRIM(v.error) = '')
              AND vr.severity IN ('Violation', 'Warning')
            GROUP BY v.shape_model, vr.severity, vr.result_path,
                     vr.source_constraint_component
            ORDER BY v.shape_model, vr.severity, occurrences DESC
        """).fetchall()

        property_rows = conn.execute("""
            SELECT vr.result_path,
                   COUNT(*) AS occurrences,
                   COUNT(DISTINCT vr.validation_id) AS affected_validations,
                   COUNT(DISTINCT v.dataset_id) AS affected_datasets
            FROM validation_results vr
            JOIN validations v ON v.id = vr.validation_id
            WHERE v.execution_status = 'validated'
              AND v.http_status = 200
              AND v.conforms IS NOT NULL
              AND (v.error IS NULL OR TRIM(v.error) = '')
              AND vr.severity = 'Violation'
            GROUP BY vr.result_path
            ORDER BY occurrences DESC
        """).fetchall()

        component_rows = conn.execute("""
            SELECT vr.source_constraint_component AS constraint_component,
                   vr.severity,
                   COUNT(*) AS occurrences
            FROM validation_results vr
            JOIN validations v ON v.id = vr.validation_id
            WHERE v.execution_status = 'validated'
              AND v.http_status = 200
              AND v.conforms IS NOT NULL
              AND (v.error IS NULL OR TRIM(v.error) = '')
              AND vr.severity IN ('Violation', 'Warning')
            GROUP BY vr.source_constraint_component, vr.severity
            ORDER BY occurrences DESC
        """).fetchall()

        # Count result rows by profile and severity; rates are normalized by
        # the number of successful validations for that profile.
        profile_issue_rows = conn.execute("""
            SELECT v.shape_model AS profile, vr.severity, COUNT(*) AS occurrences
            FROM validation_results vr
            JOIN validations v ON v.id = vr.validation_id
            WHERE v.execution_status = 'validated'
              AND v.http_status = 200
              AND v.conforms IS NOT NULL
              AND (v.error IS NULL OR TRIM(v.error) = '')
              AND vr.severity IN ('Violation', 'Warning')
            GROUP BY v.shape_model, vr.severity
        """).fetchall()

    issue_columns = [
        "profile", "severity", "result_path", "constraint_component",
        "occurrences", "affected_validations", "affected_datasets", "example_message",
    ]
    issue_records = [dict(zip(issue_columns, row)) for row in issue_rows]
    property_records = [dict(row) for row in property_rows]
    component_records = [dict(row) for row in component_rows]

    _write_csv(
        output_dir / "profile_summary.csv",
        ["profile", "validated", "conforming", "non_conforming", "conformance_pct",
         "violations", "warnings", "infos", "violation_rate_per_100", "warning_rate_per_100"],
        profile_rows,
    )
    _write_csv(output_dir / "shacl_issues.csv", issue_columns, issue_records)
    _write_csv(
        output_dir / "violations_by_property.csv",
        ["result_path", "occurrences", "affected_validations", "affected_datasets"],
        property_records,
    )
    _write_csv(
        output_dir / "shacl_components.csv",
        ["constraint_component", "severity", "occurrences"],
        component_records,
    )

    # 1. Violations by RDF property: top 15 properties, all shape models combined.
    chart_property_counts: dict[str, int] = {}
    for row in property_records:
        raw_path = row["result_path"]
        if raw_path and str(raw_path).startswith(("http://", "https://", "urn:")):
            label = _short_property(str(raw_path))
        else:
            # SHACL property paths may be represented by blank nodes. Their
            # generated identifiers are not meaningful categories for a chart.
            label = "Percorso SHACL complesso (blank node/non-IRI)"
        chart_property_counts[label] = chart_property_counts.get(label, 0) + int(row["occurrences"])
    top_properties = sorted(
        ({"label": label, "occurrences": count} for label, count in chart_property_counts.items()),
        key=lambda row: row["occurrences"], reverse=True
    )[:15]
    if top_properties:
        labels = [row["label"] for row in reversed(top_properties)]
        values = [int(row["occurrences"]) for row in reversed(top_properties)]
        fig, ax = plt.subplots(figsize=(11, max(5.5, 0.38 * len(labels))))
        ax.barh(labels, values)
        ax.set_xlabel("Numero di violazioni")
        ax.set_ylabel("Proprietà RDF (result_path)")
        ax.set_title("Distribuzione delle violazioni per proprietà RDF")
        ax.grid(axis="x", linestyle=":", alpha=0.45)
        ax.set_axisbelow(True)
        _save_bar_chart(plt, output_dir / "violations_by_rdf_property.png", "", "", "")

    # 2. SHACL constraint components, split by severity.
    component_names = sorted({
        str(row["constraint_component"] or "(non specificato)")
        for row in component_records
    })
    if component_names:
        severities = ("Violation", "Warning")
        values_by_severity = {
            severity: {
                str(row["constraint_component"] or "(non specificato)"): int(row["occurrences"])
                for row in component_records if row["severity"] == severity
            }
            for severity in severities
        }
        totals = {
            name: sum(values_by_severity[severity].get(name, 0) for severity in severities)
            for name in component_names
        }
        component_names.sort(key=lambda name: totals[name])
        y = np.arange(len(component_names))
        fig, ax = plt.subplots(figsize=(11, max(5.5, 0.42 * len(component_names))))
        left = np.zeros(len(component_names))
        for severity in severities:
            vals = np.array([values_by_severity[severity].get(name, 0) for name in component_names])
            ax.barh(y, vals, left=left, label=severity)
            left += vals
        ax.set_yticks(y, component_names)
        ax.set_xlabel("Numero di risultati SHACL")
        ax.set_ylabel("Constraint component")
        ax.set_title("Distribuzione dei risultati per componente SHACL")
        ax.legend(title="Severità")
        ax.grid(axis="x", linestyle=":", alpha=0.45)
        ax.set_axisbelow(True)
        _save_bar_chart(plt, output_dir / "shacl_component_distribution.png", "", "", "")

    # 3. Shape-model comparison: result occurrences per 100 successful validations.
    if profile_rows:
        counts = {
            (row["profile"], row["severity"]): int(row["occurrences"])
            for row in profile_issue_rows
        }
        labels = [row["profile"] for row in profile_rows]
        denominator = {row["profile"]: int(row["validated"]) for row in profile_rows}
        violation_rates = [
            100 * counts.get((profile, "Violation"), 0) / denominator[profile]
            if denominator[profile] else 0 for profile in labels
        ]
        warning_rates = [
            100 * counts.get((profile, "Warning"), 0) / denominator[profile]
            if denominator[profile] else 0 for profile in labels
        ]
        x = np.arange(len(labels))
        width = 0.38
        fig, ax = plt.subplots(figsize=(max(11, len(labels) * 0.7), 6.5))
        ax.bar(x - width / 2, violation_rates, width, label="Violation")
        ax.bar(x + width / 2, warning_rates, width, label="Warning")
        ax.set_xticks(x, labels, rotation=65, ha="right")
        ax.set_ylabel("Risultati ogni 100 validazioni")
        ax.set_xlabel("Shape model")
        ax.set_title("Confronto tra shape model")
        ax.legend(title="Severità")
        ax.grid(axis="y", linestyle=":", alpha=0.45)
        ax.set_axisbelow(True)
        _save_bar_chart(plt, output_dir / "shape_model_comparison.png", "", "", "")

    # 4. DCAT-AP version comparison. Compare base profiles only (e.g. dcatap300),
    # avoiding the aggregation of different SHACL conformance levels. Since
    # conformance can be uniformly false in the observed sample, compare the
    # normalized number of Violation/Warning result rows per 100 validations.
    version_profile_rows: dict[str, dict[str, Any]] = {}
    for row in profile_rows:
        parsed = profile_version(row["profile"])
        if not parsed:
            continue
        version, _ = parsed
        target = version_profile_rows.setdefault(version, {
            "version": version, "validated": 0, "violations": 0, "warnings": 0,
        })
        target["validated"] += int(row["validated"])
        target["violations"] += int(row["violations"])
        target["warnings"] += int(row["warnings"])

    version_summary = []
    for version in sorted(version_profile_rows, key=_version_key):
        row = version_profile_rows[version]
        n = row["validated"]
        version_summary.append({
            "version": version,
            "validated": n,
            "violation_occurrences": row["violations"],
            "warning_occurrences": row["warnings"],
            "violations_per_100_validations": round(100 * row["violations"] / n, 2) if n else 0,
            "warnings_per_100_validations": round(100 * row["warnings"] / n, 2) if n else 0,
        })
    _write_csv(
        output_dir / "dcatap_version_comparison.csv",
        ["version", "validated", "violation_occurrences", "warning_occurrences",
         "violations_per_100_validations", "warnings_per_100_validations"],
        version_summary,
    )
    if version_summary:
        labels = [row["version"] for row in version_summary]
        violation_rates = [row["violations_per_100_validations"] for row in version_summary]
        warning_rates = [row["warnings_per_100_validations"] for row in version_summary]
        x = np.arange(len(labels))
        width = 0.38
        fig, ax = plt.subplots(figsize=(max(9, len(labels) * 0.85), 6))
        ax.bar(x - width / 2, violation_rates, width, label="Violation")
        ax.bar(x + width / 2, warning_rates, width, label="Warning")
        ax.set_xticks(x, labels)
        ax.set_ylabel("Risultati ogni 100 validazioni")
        ax.set_xlabel("Versione DCAT-AP (shape model base)")
        ax.set_title("Confronto dei risultati SHACL tra versioni DCAT-AP")
        ax.legend(title="Severità")
        ax.grid(axis="y", linestyle=":", alpha=0.45)
        ax.set_axisbelow(True)
        _save_bar_chart(plt, output_dir / "dcatap_version_comparison.png", "", "", "")

    return {
        "datasets_in_inventory": dataset_count,
        "fetch_status": {row[0]: row[1] for row in fetch_counts},
        "successful_validation_rows": len(validation_rows),
        "profiles": len(profile_rows),
        "issue_categories": len(issue_records),
        "charts_generated": sum([
            bool(top_properties), bool(component_names), bool(profile_rows), bool(version_summary)
        ]),
        "output_dir": str(output_dir.resolve()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analisi statistica read-only del database MQA"
    )
    parser.add_argument("--db", type=Path, default=Path("analysis.db"),
                        help="Percorso di analysis.db")
    parser.add_argument("--out", type=Path, default=Path("analysis-output"),
                        help="Directory output CSV/PNG")
    args = parser.parse_args()
    summary = analyze_database(args.db, args.out)
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
