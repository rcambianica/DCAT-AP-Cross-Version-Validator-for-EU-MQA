"""Read-only quantitative and qualitative analysis of EU MQA validations.

The module deliberately separates its data sources:

* Quantitative analysis reads only SQLite (``datasets`` and ``validations``).
* Qualitative analysis reads only the PyOxigraph validation catalog and the
  named graphs containing complete SHACL validation reports.

No analysis result is written back to either source database. CSV, JSON and PNG
files are generated as derived artifacts. Individual ``sh:ValidationResult``
records exist only in memory and in the exported analysis CSV.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

SH = "http://www.w3.org/ns/shacl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_FIRST = RDF + "first"
RDF_REST = RDF + "rest"
RDF_NIL = RDF + "nil"
RDF_TYPE = RDF + "type"

MQA = "urn:mqa:vocab:"
CATALOG_GRAPH_IRI = "urn:mqa:validation-catalog"

SEVERITIES = ("Violation", "Warning", "Info")
LEVEL_ORDER = {"all": 0, "level1": 1, "level2": 2, "level3": 3, "unknown": 4}

# Canonical order shared by every analysis output. Measurements remain paired
# with their ShapeModel; chart labels are never sorted independently.
PROFILE_ORDER = (
    "dcatap11", "dcatap12", "dcatap121", "dcatap201",
    "dcatap210level1", "dcatap210",
    "dcatap211level1", "dcatap211level2", "dcatap211level3", "dcatap211",
    "dcatap300level1", "dcatap300level2", "dcatap300level3", "dcatap300",
    "dcatap301level1", "dcatap301level2", "dcatap301level3", "dcatap301",
)
PROFILE_ORDER_INDEX = {profile: index for index, profile in enumerate(PROFILE_ORDER)}

# Ordered profile chains used for the five comparative charts. Each adjacent
# pair is compared on the same dataset and against the same validator endpoint.
COMPARISON_SEQUENCES: dict[str, tuple[str, ...]] = {
    "dcatap301": (
        "dcatap301level1", "dcatap301level2", "dcatap301level3", "dcatap301",
    ),
    "dcatap300": (
        "dcatap300level1", "dcatap300level2", "dcatap300level3", "dcatap300",
    ),
    "dcatap211": (
        "dcatap211level1", "dcatap211level2", "dcatap211level3", "dcatap211",
    ),
    "dcatap210": ("dcatap210level1", "dcatap210"),
    "legacy": ("dcatap11", "dcatap12", "dcatap121", "dcatap201"),
}

COMPARISON_TITLES = {
    "dcatap301": "DCAT-AP 3.0.1",
    "dcatap300": "DCAT-AP 3.0.0",
    "dcatap211": "DCAT-AP 2.1.1",
    "dcatap210": "DCAT-AP 2.1.0",
    "legacy": "DCAT-AP 1.1 → 2.0.1",
}


class AnalysisError(RuntimeError):
    """Raised when an analysis source does not match the expected schema."""


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open SQLite without allowing analysis to mutate the source database."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Database SQLite non trovato: {path}")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_shape_model(shape_model: str | None) -> tuple[str, str] | None:
    """Return ``(version, level)`` for MQA IDs such as dcatap301level2.

    A profile without a ``levelN`` suffix is the complete *All shapes*
    configuration, not a base configuration.
    """
    match = re.fullmatch(r"dcatap(\d+)(?:level([123]))?", (shape_model or "").casefold())
    if not match:
        return None
    digits, level_number = match.groups()
    if len(digits) == 1:
        version = digits
    elif len(digits) == 2:
        version = f"{digits[0]}.{digits[1]}"
    else:
        version = f"{digits[0]}.{digits[1]}.{int(digits[2:])}"
    level = f"level{level_number}" if level_number else "all"
    return version, level


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(headers), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _profile_sort_key(shape_model: str) -> tuple[Any, ...]:
    """Sort known ShapeModels in the canonical cross-version order."""
    if shape_model in PROFILE_ORDER_INDEX:
        return (0, PROFILE_ORDER_INDEX[shape_model])
    parsed = parse_shape_model(shape_model)
    if parsed is None:
        return (2, (), LEVEL_ORDER["unknown"], shape_model)
    version, level = parsed
    return (1, _version_key(version), LEVEL_ORDER.get(level, 99), shape_model)


def _read_analysis_query(relative_path: str) -> str:
    """Read a version-controlled SQL/SPARQL query shipped with the project."""
    query_path = Path(__file__).resolve().parent.parent / "queries" / relative_path
    if not query_path.is_file():
        # Wheel installations place the declared data-files under sys.prefix.
        query_path = Path(sys.prefix) / "queries" / relative_path
    try:
        return query_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalysisError(f"Query di analisi non leggibile: {query_path}") from exc


def _load_quantitative_data(
    db_path: Path | str,
    *,
    catalogue_id: str | None = None,
    category_id: str | None = None,
) -> dict[str, Any]:
    """Load all quantitative inputs exclusively from SQLite."""
    with connect_readonly(db_path) as conn:
        dataset_columns = {row[1] for row in conn.execute("PRAGMA table_info(datasets)")}
        validation_columns = {row[1] for row in conn.execute("PRAGMA table_info(validations)")}
        required_dataset = {
            "dataset_id", "catalogue_id", "category_id", "fetch_status", "fetch_http_status", "fetch_error",
        }
        required_validation = {
            "id", "dataset_id", "validator_url", "shape_model", "conforms", "results",
            "violations", "warnings", "infos", "http_status", "error", "execution_status",
            "validated_at", "report_status",
        }
        missing = sorted(required_dataset - dataset_columns) + sorted(required_validation - validation_columns)
        if missing:
            raise AnalysisError("Schema SQLite non compatibile; colonne mancanti: " + ", ".join(missing))

        query_params = {
            "catalogue_id": catalogue_id,
            "category_id": category_id,
        }

        datasets = conn.execute(
            _read_analysis_query("SQL/03-datasets-in-scope.sql"),
            query_params,
        ).fetchall()

        # Keep all operational states for diagnostics. These rows are never
        # interpreted as SHACL non-conformity.
        operational_rows = conn.execute(
            _read_analysis_query("SQL/04-validation-operational-status.sql"),
            query_params,
        ).fetchall()

        # Only completed HTTP 200 validations with a definite SHACL outcome
        # and no execution error enter quantitative outcome statistics.
        validations = conn.execute(
            _read_analysis_query("SQL/05-successful-validations.sql"),
            query_params,
        ).fetchall()

        total_validation_rows = conn.execute(
            _read_analysis_query("SQL/06-total-validation-rows.sql"),
            query_params,
        ).fetchone()[0]

    return {
        "datasets": [dict(row) for row in datasets],
        "validations": [dict(row) for row in validations],
        "operational_rows": [dict(row) for row in operational_rows],
        "total_validation_rows": int(total_validation_rows),
    }


def _quantitative_profile_rows(validations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in validations:
        groups[str(row["shape_model"] or "(shape model non specificato)")].append(row)

    output: list[dict[str, Any]] = []
    for profile in sorted(groups, key=_profile_sort_key):
        rows = groups[profile]
        parsed = parse_shape_model(profile)
        version, level = parsed if parsed else (None, "unknown")
        violations = [int(row["violations"] or 0) for row in rows]
        warnings = [int(row["warnings"] or 0) for row in rows]
        infos = [int(row["infos"] or 0) for row in rows]
        results = [int(row["results"] or 0) for row in rows]
        distinct_datasets = len({row["dataset_id"] for row in rows})
        affected_v = len({row["dataset_id"] for row in rows if int(row["violations"] or 0) > 0})
        affected_w = len({row["dataset_id"] for row in rows if int(row["warnings"] or 0) > 0})
        affected_i = len({row["dataset_id"] for row in rows if int(row["infos"] or 0) > 0})
        # Use the validator's explicit conforms flag to classify outcomes.
        # A non-conforming validation is then split by the presence/absence
        # of Violation-severity results. Warning and Info-only outcomes belong
        # to the non-conforming/no-Violation category.
        conforming = sum(int(int(row["conforms"]) == 1) for row in rows)
        nonconforming_with_violations = sum(
            int(int(row["conforms"]) == 0 and int(row["violations"] or 0) > 0)
            for row in rows
        )
        nonconforming_without_violations = sum(
            int(int(row["conforms"]) == 0 and int(row["violations"] or 0) == 0)
            for row in rows
        )

        # Keep the severity-only counts as diagnostics in the CSV. They are
        # distinct from the three mutually exclusive outcome categories.
        with_violations = sum(int(int(row["violations"] or 0) > 0) for row in rows)
        without_violations_findings = sum(
            int(int(row["violations"] or 0) == 0 and (int(row["warnings"] or 0) + int(row["infos"] or 0)) > 0)
            for row in rows
        )
        no_findings = sum(
            int(int(row["violations"] or 0) == 0 and int(row["warnings"] or 0) == 0 and int(row["infos"] or 0) == 0)
            for row in rows
        )
        n = len(rows)
        output.append({
            "shape_model": profile,
            "dcatap_version": version or "",
            "shape_level": level,
            "validations": n,
            "datasets": distinct_datasets,
            "conforming_validations": conforming,
            "conformity_pct": round(100 * conforming / n, 2) if n else 0.0,
            "nonconforming_with_violations": nonconforming_with_violations,
            "nonconforming_with_violations_pct": round(100 * nonconforming_with_violations / n, 2) if n else 0.0,
            "nonconforming_without_violations": nonconforming_without_violations,
            "nonconforming_without_violations_pct": round(100 * nonconforming_without_violations / n, 2) if n else 0.0,
            "with_violations": with_violations,
            "without_violations_with_findings": without_violations_findings,
            "no_findings": no_findings,
            "total_results": sum(results),
            "total_violations": sum(violations),
            "total_warnings": sum(warnings),
            "total_infos": sum(infos),
            "datasets_with_violations": affected_v,
            "datasets_with_violations_pct": round(100 * affected_v / distinct_datasets, 2) if distinct_datasets else 0.0,
            "datasets_with_warnings": affected_w,
            "datasets_with_warnings_pct": round(100 * affected_w / distinct_datasets, 2) if distinct_datasets else 0.0,
            "datasets_with_infos": affected_i,
            "datasets_with_infos_pct": round(100 * affected_i / distinct_datasets, 2) if distinct_datasets else 0.0,
            "avg_violations_per_validation": round(sum(violations) / n, 3) if n else 0.0,
            "median_violations_per_validation": median(violations) if n else 0,
            "max_violations_per_validation": max(violations, default=0),
            "avg_warnings_per_validation": round(sum(warnings) / n, 3) if n else 0.0,
            "median_warnings_per_validation": median(warnings) if n else 0,
            "max_warnings_per_validation": max(warnings, default=0),
            "no_findings_pct": round(100 * no_findings / n, 2) if n else 0.0,
            "findings_without_violations_pct": round(100 * without_violations_findings / n, 2) if n else 0.0,
            "with_violations_pct": round(100 * with_violations / n, 2) if n else 0.0,
        })
    return output


def _build_paired_comparisons(validations: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keyed: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    versions_by_level: dict[str, set[str]] = defaultdict(set)
    for row in validations:
        profile = str(row["shape_model"] or "")
        parsed = parse_shape_model(profile)
        if parsed is None:
            continue
        version, level = parsed
        key = (str(row["dataset_id"]), str(row["validator_url"]), level, version)
        keyed[key] = row
        versions_by_level[level].add(version)

    pairs: list[dict[str, Any]] = []
    summary_groups: dict[tuple[str, str, str], Counter] = defaultdict(Counter)
    for level, versions_set in versions_by_level.items():
        versions = sorted(versions_set, key=_version_key)
        for older, newer in zip(versions, versions[1:]):
            old_rows = [
                (key, row) for key, row in keyed.items()
                if key[2] == level and key[3] == older
            ]
            for (dataset_id, endpoint, _, _), old_row in old_rows:
                new_row = keyed.get((dataset_id, endpoint, level, newer))
                if new_row is None:
                    continue
                delta_v = int(new_row["violations"] or 0) - int(old_row["violations"] or 0)
                delta_w = int(new_row["warnings"] or 0) - int(old_row["warnings"] or 0)
                pairs.append({
                    "dataset_id": dataset_id,
                    "validator_url": endpoint,
                    "level": level,
                    "from_version": older,
                    "to_version": newer,
                    "from_profile": old_row["shape_model"],
                    "to_profile": new_row["shape_model"],
                    "from_violations": int(old_row["violations"] or 0),
                    "to_violations": int(new_row["violations"] or 0),
                    "delta_violations": delta_v,
                    "from_warnings": int(old_row["warnings"] or 0),
                    "to_warnings": int(new_row["warnings"] or 0),
                    "delta_warnings": delta_w,
                })
                group = summary_groups[(level, older, newer)]
                group["paired_datasets"] += 1
                group[f"violations_{'increased' if delta_v > 0 else 'decreased' if delta_v < 0 else 'unchanged'}"] += 1
                group[f"warnings_{'increased' if delta_w > 0 else 'decreased' if delta_w < 0 else 'unchanged'}"] += 1

    summaries = []
    for (level, older, newer), counts in sorted(
        summary_groups.items(), key=lambda item: (LEVEL_ORDER.get(item[0][0], 99), _version_key(item[0][1]), _version_key(item[0][2]))
    ):
        summaries.append({
            "level": level,
            "from_version": older,
            "to_version": newer,
            **{name: counts[name] for name in (
                "paired_datasets", "violations_increased", "violations_decreased", "violations_unchanged",
                "warnings_increased", "warnings_decreased", "warnings_unchanged",
            )},
        })
    return pairs, summaries


def _build_sequence_comparison_summaries(
    validations: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Summarize paired changes along the five explicitly requested profiles.

    Pairing is performed by (dataset_id, validator_url), so each transition
    compares the same dataset validated by the same endpoint.
    """
    by_profile: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = defaultdict(dict)
    for row in validations:
        profile = str(row["shape_model"] or "")
        if profile:
            key = (str(row["dataset_id"]), str(row["validator_url"] or ""))
            by_profile[profile][key] = row

    output: dict[str, list[dict[str, Any]]] = {}
    for sequence_name, profiles in COMPARISON_SEQUENCES.items():
        transitions: list[dict[str, Any]] = []
        for older, newer in zip(profiles, profiles[1:]):
            old_rows = by_profile.get(older, {})
            new_rows = by_profile.get(newer, {})
            shared_keys = old_rows.keys() & new_rows.keys()
            counts = Counter()
            for key in shared_keys:
                old_row = old_rows[key]
                new_row = new_rows[key]
                delta_v = int(new_row["violations"] or 0) - int(old_row["violations"] or 0)
                delta_w = int(new_row["warnings"] or 0) - int(old_row["warnings"] or 0)
                counts[f"violations_{'increased' if delta_v > 0 else 'decreased' if delta_v < 0 else 'unchanged'}"] += 1
                counts[f"warnings_{'increased' if delta_w > 0 else 'decreased' if delta_w < 0 else 'unchanged'}"] += 1

            transitions.append({
                "from_profile": older,
                "to_profile": newer,
                "paired_datasets": len(shared_keys),
                **{name: counts[name] for name in (
                    "violations_increased", "violations_decreased", "violations_unchanged",
                    "warnings_increased", "warnings_decreased", "warnings_unchanged",
                )},
            })
        output[sequence_name] = transitions
    return output


def _build_sequence_profile_count_rows(
    validations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate total Violation and Warning findings for each requested profile.

    Counts are sums over successfully completed validation rows. The number of
    validations and distinct datasets is included alongside each total so that
    differences in sample coverage remain visible when interpreting the plots.
    Profiles with no successful validations are retained with empty totals.
    """
    by_profile: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in validations:
        profile = str(row["shape_model"] or "")
        if profile:
            by_profile[profile].append(row)

    output: list[dict[str, Any]] = []
    for sequence_name, profiles in COMPARISON_SEQUENCES.items():
        for position, profile in enumerate(profiles, start=1):
            rows = by_profile.get(profile, [])
            output.append({
                "sequence": sequence_name,
                "sequence_title": COMPARISON_TITLES[sequence_name],
                "sequence_position": position,
                "shape_model": profile,
                "validation_count": len(rows),
                "dataset_count": len({str(row["dataset_id"]) for row in rows}),
                "total_violations": sum(int(row["violations"] or 0) for row in rows) if rows else "",
                "total_warnings": sum(int(row["warnings"] or 0) for row in rows) if rows else "",
            })
    return output


def _generate_quantitative_charts(
    output_dir: Path,
    profile_rows: Sequence[Mapping[str, Any]],
    validations: Sequence[Mapping[str, Any]],
) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("Per generare i grafici installare matplotlib e numpy") from exc

    generated: list[str] = []
    # Enforce the canonical order at the plotting boundary as well, so every
    # chart keeps each ShapeModel paired with its own measurements even when
    # this helper is called with externally assembled rows.
    profile_rows = sorted(
        profile_rows,
        key=lambda row: _profile_sort_key(str(row["shape_model"])),
    )
    # Remove the previous chart names so an existing output directory cannot
    # retain obsolete bar charts next to the replacement line charts.
    for sequence_name in COMPARISON_SEQUENCES:
        obsolete_chart = output_dir / f"paired_version_changes_{sequence_name}.png"
        if obsolete_chart.exists():
            obsolete_chart.unlink()

    if profile_rows:
        labels = [row["shape_model"] for row in profile_rows]
        fig, ax = plt.subplots(figsize=(max(12, len(labels) * 0.65), 7))
        bottom = np.zeros(len(labels))
        for key, label, color in (
            ("conformity_pct", "Conforme", "#2E7D32"),
            ("nonconforming_with_violations_pct", "Non conforme con almeno una violazione", "#C62828"),
            ("nonconforming_without_violations_pct", "Non conforme senza violazioni", "#F9A825"),
        ):
            values = np.asarray([row[key] for row in profile_rows], dtype=float)
            ax.bar(labels, values, bottom=bottom, label=label, color=color)
            bottom += values
        ax.set_ylim(0, 100)
        ax.set_ylabel("Validazioni (%)")
        ax.set_xlabel("Shape model")
        ax.set_title("Esiti delle validazioni per shape model")
        ax.tick_params(axis="x", rotation=65)
        ax.legend()
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        ax.set_axisbelow(True)
        fig.tight_layout()
        name = "validation_outcomes.png"
        fig.savefig(output_dir / name, dpi=200, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)

        fig, ax = plt.subplots(figsize=(max(12, len(labels) * 0.65), 7))
        x = np.arange(len(labels))
        width = 0.26
        for offset, key, label, color in (
            (-width, "total_violations", "Violation", "#C62828"),
            (0, "total_warnings", "Warning", "#EF6C00"),
            (width, "total_infos", "Info", "#F9A825"),
        ):
            ax.bar(x + offset, [row[key] for row in profile_rows], width, label=label, color=color)
        ax.set_xticks(x, labels, rotation=65, ha="right")
        ax.set_ylabel("Risultati SHACL")
        ax.set_title("Numero complessivo di risultati per severità e shape model")
        ax.legend()
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        ax.set_axisbelow(True)
        fig.tight_layout()
        name = "severity_totals.png"
        fig.savefig(output_dir / name, dpi=200, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)

    # This chart is intentionally unchanged: it shows the per-validation
    # distributions of Violation and Warning counts for each shape model.
    if validations and profile_rows:
        labels = [row["shape_model"] for row in profile_rows]
        fig, axes = plt.subplots(2, 1, figsize=(max(12, len(labels) * 0.7), 10), sharex=True)
        for ax, field, title in (
            (axes[0], "violations", "Violation per validazione"),
            (axes[1], "warnings", "Warning per validazione"),
        ):
            data = [[int(row[field] or 0) for row in validations if row["shape_model"] == label] for label in labels]
            # Matplotlib renamed ``labels`` to ``tick_labels`` in 3.9.
            import inspect
            boxplot_parameters = inspect.signature(ax.boxplot).parameters
            label_argument = "tick_labels" if "tick_labels" in boxplot_parameters else "labels"
            ax.boxplot(data, showfliers=True, **{label_argument: labels})
            ax.set_ylabel("Risultati per dataset")
            ax.set_title(title)
            ax.grid(axis="y", linestyle=":", alpha=0.4)
            ax.set_axisbelow(True)
        axes[1].tick_params(axis="x", rotation=65)
        fig.suptitle("Distribuzione dei risultati SHACL tra dataset")
        fig.tight_layout()
        name = "result_distributions.png"
        fig.savefig(output_dir / name, dpi=200, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)

    # Generate one two-panel line chart for each explicitly requested profile
    # sequence. The y values are total findings across the successful SQLite
    # validation rows for each profile; sample sizes are shown on the x ticks.
    sequence_rows = _build_sequence_profile_count_rows(validations)
    for sequence_name, profiles in COMPARISON_SEQUENCES.items():
        rows = [row for row in sequence_rows if row["sequence"] == sequence_name]
        if not any(row["validation_count"] for row in rows):
            continue

        x = np.arange(len(profiles))
        labels = [
            f"{row['shape_model']}\n(n={row['validation_count']})"
            for row in rows
        ]
        fig, axes = plt.subplots(2, 1, figsize=(max(11, len(profiles) * 3.0), 9), sharex=True)

        for ax, field, title, color, legend_label in (
            (axes[0], "total_violations", "Numero di violazioni", "#C62828", "Violazioni"),
            (axes[1], "total_warnings", "Numero di warning", "#EF6C00", "Warning"),
        ):
            values = [
                float(row[field]) if row["validation_count"] else np.nan
                for row in rows
            ]
            ax.plot(
                x, values, color=color, marker="o", markersize=7,
                linewidth=2, label=legend_label,
            )
            ax.set_ylabel(title)
            ax.set_title(title)
            ax.set_ylim(bottom=0)
            ax.legend(loc="best")
            ax.grid(axis="y", linestyle=":", alpha=0.4)
            ax.set_axisbelow(True)

        axes[1].set_xticks(x, labels, rotation=0, ha="center")
        axes[1].set_xlabel("ShapeModel DCAT-AP (n = validazioni incluse)")
        fig.suptitle(f"Conteggio di violazioni e warning – {COMPARISON_TITLES[sequence_name]}")
        fig.tight_layout()
        name = f"profile_severity_trends_{sequence_name}.png"
        fig.savefig(output_dir / name, dpi=200, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)

    return generated


def analyze_quantitative(
    db_path: Path | str,
    output_dir: Path | str,
    *,
    catalogue_id: str | None = None,
    category_id: str | None = None,
    charts: bool = True,
) -> dict[str, Any]:
    """Generate quantitative statistics using SQLite only."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = _load_quantitative_data(db_path, catalogue_id=catalogue_id, category_id=category_id)
    datasets = data["datasets"]
    validations = data["validations"]

    profile_rows = _quantitative_profile_rows(validations)
    pairs, paired_summary = _build_paired_comparisons(validations)

    validation_distribution = []
    for row in validations:
        parsed = parse_shape_model(row["shape_model"])
        version, level = parsed if parsed else ("", "unknown")
        validation_distribution.append({
            "validation_id": row["id"],
            "dataset_id": row["dataset_id"],
            "validator_url": row["validator_url"],
            "shape_model": row["shape_model"],
            "dcatap_version": version,
            "shape_level": level,
            "violations": int(row["violations"] or 0),
            "warnings": int(row["warnings"] or 0),
            "infos": int(row["infos"] or 0),
            "results": int(row["results"] or 0),
            "conforms": bool(row["conforms"]),
            "validated_at": row["validated_at"],
            "report_status": row["report_status"],
        })

    fetch_counts = Counter(str(row["fetch_status"] or "unknown") for row in datasets)
    dataset_status_rows = [
        {"fetch_status": status, "datasets": count}
        for status, count in sorted(fetch_counts.items())
    ]
    validation_status_rows = data["operational_rows"]

    _write_csv(out / "dataset_fetch_status.csv", ["fetch_status", "datasets"], dataset_status_rows)
    _write_csv(out / "validation_execution_status.csv", ["execution_status", "http_status", "error", "count"], validation_status_rows)
    _write_csv(out / "profile_summary.csv", [
        "shape_model", "dcatap_version", "shape_level", "validations", "datasets", "conforming_validations",
        "conformity_pct", "nonconforming_with_violations", "nonconforming_with_violations_pct",
        "nonconforming_without_violations", "nonconforming_without_violations_pct",
        "with_violations", "without_violations_with_findings", "no_findings",
        "total_results", "total_violations", "total_warnings", "total_infos",
        "datasets_with_violations", "datasets_with_violations_pct", "datasets_with_warnings",
        "datasets_with_warnings_pct", "datasets_with_infos", "datasets_with_infos_pct",
        "avg_violations_per_validation", "median_violations_per_validation", "max_violations_per_validation",
        "avg_warnings_per_validation", "median_warnings_per_validation", "max_warnings_per_validation",
        "no_findings_pct", "findings_without_violations_pct", "with_violations_pct",
    ], profile_rows)
    _write_csv(out / "validation_distribution.csv", [
        "validation_id", "dataset_id", "validator_url", "shape_model", "dcatap_version", "shape_level",
        "violations", "warnings", "infos", "results", "conforms", "validated_at", "report_status",
    ], validation_distribution)
    sequence_profile_counts = _build_sequence_profile_count_rows(validations)
    _write_csv(out / "sequence_profile_counts.csv", [
        "sequence", "sequence_title", "sequence_position", "shape_model",
        "validation_count", "dataset_count", "total_violations", "total_warnings",
    ], sequence_profile_counts)
    _write_csv(out / "paired_version_deltas.csv", [
        "dataset_id", "validator_url", "level", "from_version", "to_version", "from_profile", "to_profile",
        "from_violations", "to_violations", "delta_violations", "from_warnings", "to_warnings", "delta_warnings",
    ], pairs)
    _write_csv(out / "paired_version_summary.csv", [
        "level", "from_version", "to_version", "paired_datasets", "violations_increased",
        "violations_decreased", "violations_unchanged", "warnings_increased", "warnings_decreased",
        "warnings_unchanged",
    ], paired_summary)

    charts_generated = _generate_quantitative_charts(out, profile_rows, validation_distribution) if charts else []
    summary = {
        "source": "SQLite",
        "database": str(Path(db_path).resolve()),
        "catalogue_filter": catalogue_id,
        "category_filter": category_id,
        "datasets_in_scope": len(datasets),
        "fetch_status": dict(fetch_counts),
        "validation_rows_in_scope": data["total_validation_rows"],
        "successful_validations_included": len(validations),
        "validation_rows_excluded": data["total_validation_rows"] - len(validations),
        "shape_models": len(profile_rows),
        "paired_comparisons": len(pairs),
        "charts_generated": charts_generated,
        "output_dir": str(out.resolve()),
    }
    _write_json(out / "quantitative_summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# Qualitative analysis: all report metadata and report contents come from
# PyOxigraph. SQLite is intentionally not imported or consulted in this part.
# ---------------------------------------------------------------------------


def _query_rows(report_store, sparql: str) -> list[dict[str, Any]]:
    result = report_store.query(sparql)
    if not hasattr(result, "variables"):
        raise AnalysisError("La query SPARQL del catalogo non ha restituito una SELECT")
    variables = list(result.variables)
    rows: list[dict[str, Any]] = []
    for solution in result:
        row: dict[str, Any] = {}
        for variable in variables:
            name = str(variable)
            if name.startswith("?"):
                name = name[1:]
            try:
                value = solution[variable]
            except (KeyError, TypeError):
                value = solution[name]
            row[name] = value
        rows.append(row)
    return rows


def _term_string(term: Any) -> str | None:
    """Return an RDF term's lexical/value form, not its RDF serialization.

    PyOxigraph's ``str(term)`` is N-Triples syntax (for example,
    ``<urn:example:graph>`` or ``"dcatap301level1"``). Catalog values are
    consumed as plain strings and graph IRIs are passed back to Oxigraph, so
    using that serialization would corrupt both IRIs and literals. RDFLib
    terms are supported as well: URIRefs and BNodes fall back to ``str`` while
    Literals expose their Python value through ``.value``.
    """
    if term is None:
        return None
    value = getattr(term, "value", None)
    if value is not None:
        return str(value)
    return str(term)


def _term_n3(term: Any) -> str | None:
    if term is None:
        return None
    n3 = getattr(term, "n3", None)
    return n3() if callable(n3) else str(term)


def _short_iri(value: str) -> str:
    namespaces = (
        ("http://www.w3.org/ns/dcat#", "dcat:"),
        ("http://www.w3.org/ns/adms#", "adms:"),
        ("http://purl.org/dc/terms/", "dcterms:"),
        ("http://xmlns.com/foaf/0.1/", "foaf:"),
        ("http://www.w3.org/2004/02/skos/core#", "skos:"),
        ("http://www.w3.org/ns/dqv#", "dqv:"),
        ("http://data.europa.eu/r5r/", "r5r:"),
        ("http://www.w3.org/2002/07/owl#", "owl:"),
        (RDF, "rdf:"),
        (SH, "sh:"),
    )
    for namespace, prefix in namespaces:
        if value.startswith(namespace):
            return prefix + value[len(namespace):]
    return value


def _local_name(term: Any) -> str | None:
    if term is None:
        return None
    value = str(term)
    if "#" in value:
        return value.rsplit("#", 1)[-1]
    return value.rsplit("/", 1)[-1]


def _rdf_list(graph, head, *, max_items: int = 100) -> list[Any] | None:
    from rdflib import URIRef

    items: list[Any] = []
    current = head
    seen: set[Any] = set()
    nil = URIRef(RDF_NIL)
    while current != nil:
        if current in seen or len(items) >= max_items:
            return None
        seen.add(current)
        first = list(graph.objects(current, URIRef(RDF_FIRST)))
        rest = list(graph.objects(current, URIRef(RDF_REST)))
        if len(first) != 1 or len(rest) != 1:
            return None
        items.append(first[0])
        current = rest[0]
    return items


def _path_expression(graph, node: Any, *, depth: int = 0, seen: set[Any] | None = None) -> str:
    from rdflib import BNode, URIRef

    if depth > 12:
        return "<path-depth-limit>"
    if isinstance(node, URIRef):
        return _short_iri(str(node))
    if not isinstance(node, BNode):
        return str(node)
    seen = set() if seen is None else seen
    if node in seen:
        return "<cyclic-path>"
    seen = seen | {node}

    def single(predicate: str):
        values = list(graph.objects(node, URIRef(SH + predicate)))
        return values[0] if len(values) == 1 else None

    inverse = single("inversePath")
    if inverse is not None:
        return f"inverse({_path_expression(graph, inverse, depth=depth + 1, seen=seen)})"
    for predicate, operator in (("zeroOrMorePath", "*"), ("oneOrMorePath", "+"), ("zeroOrOnePath", "?")):
        child = single(predicate)
        if child is not None:
            return f"({_path_expression(graph, child, depth=depth + 1, seen=seen)}){operator}"
    alternative = single("alternativePath")
    if alternative is not None:
        members = _rdf_list(graph, alternative)
        if members is not None:
            return "(" + " | ".join(_path_expression(graph, item, depth=depth + 1, seen=seen) for item in members) + ")"
    # A blank-node sequence path is itself an RDF list.
    members = _rdf_list(graph, node)
    if members is not None:
        return "/".join(_path_expression(graph, item, depth=depth + 1, seen=seen) for item in members)
    return "<complex-path-unresolved>"


def _resolve_path(graph, path_terms: Sequence[Any]) -> tuple[str, str]:
    from rdflib import BNode, URIRef

    if not path_terms:
        return "missing", "(sh:resultPath assente)"
    if len(path_terms) > 1:
        values = [_resolve_path(graph, [term])[1] for term in path_terms]
        return "multiple", " | ".join(sorted(set(values)))
    term = path_terms[0]
    if isinstance(term, URIRef):
        return "simple_iri", _short_iri(str(term))
    if isinstance(term, BNode):
        expression = _path_expression(graph, term)
        return ("complex_unresolved" if expression == "<complex-path-unresolved>" else "complex_resolved", expression)
    return "unclassified", _term_string(term) or "(percorso non classificabile)"


def _values(graph, subject, predicate):
    from rdflib import URIRef
    return list(graph.objects(subject, URIRef(predicate)))


def _normalise_message(message: str | None) -> str:
    """Conservative signature for exact/near-exact message grouping.

    This does not attempt semantic anonymisation. URI/value normalisation is a
    separate concern for the later embedding pipeline.
    """
    return " ".join((message or "").casefold().split())


def _extract_qualitative_results(report: Mapping[str, Any], graph) -> list[dict[str, Any]]:
    from rdflib import RDF
    from rdflib.namespace import SH as SH_NS

    rows: list[dict[str, Any]] = []
    result_nodes = sorted(set(graph.subjects(RDF.type, SH_NS.ValidationResult)), key=str)
    for result_node in result_nodes:
        severity_terms = sorted(_values(graph, result_node, SH + "resultSeverity"), key=str)
        severity = _local_name(severity_terms[0]) if severity_terms else "Unspecified"
        if severity not in {*SEVERITIES, "Unspecified"}:
            severity = "Unknown"

        path_type, path_label = _resolve_path(graph, _values(graph, result_node, SH + "resultPath"))
        components = sorted(_values(graph, result_node, SH + "sourceConstraintComponent"), key=str)
        messages = sorted({str(term) for term in _values(graph, result_node, SH + "resultMessage")})
        focus_nodes = sorted({_term_n3(term) or "" for term in _values(graph, result_node, SH + "focusNode")})
        values = sorted({_term_n3(term) or "" for term in _values(graph, result_node, SH + "value")})
        source_shapes = sorted({_term_n3(term) or "" for term in _values(graph, result_node, SH + "sourceShape")})
        source_constraints = sorted({_term_n3(term) or "" for term in _values(graph, result_node, SH + "sourceConstraint")})
        message = " || ".join(messages)
        component_iris = sorted({str(term) for term in components})
        component_names = sorted({_local_name(term) or "" for term in components})
        embedding_text = "\n".join(part for part in (
            "SHACL component: " + ", ".join(component_names) if component_names else "",
            "RDF path: " + path_label if path_label else "",
            "Validation message: " + message if message else "",
        ) if part)

        rows.append({
            "report_graph_iri": report["report_graph_iri"],
            "validation_id": report.get("validation_id", ""),
            "dataset_id": report.get("dataset_id", ""),
            "shape_model": report.get("shape_model", ""),
            "report_sha256": report.get("report_sha256", ""),
            # This node identifier is meaningful only inside this report graph.
            "result_node": str(result_node),
            "severity": severity,
            "source_constraint_component": " | ".join(component_names),
            "source_constraint_component_iri": " | ".join(component_iris),
            "path_type": path_type,
            "path": path_label,
            "message": message,
            "message_signature": _normalise_message(message),
            "focus_nodes": json.dumps(focus_nodes, ensure_ascii=False),
            "values": json.dumps(values, ensure_ascii=False),
            "source_shapes": json.dumps(source_shapes, ensure_ascii=False),
            "source_constraints": json.dumps(source_constraints, ensure_ascii=False),
            # Suitable as input text for a later embedding step. Dataset IDs,
            # shape model, severity, focus nodes and literal values are excluded.
            "embedding_text": embedding_text,
        })
    return rows


def _load_report_catalog(report_store) -> list[dict[str, Any]]:
    query = _read_analysis_query("SPARQL/10-analysis-report-catalog.sparql")
    rows = _query_rows(report_store, query)
    # Group by graph IRI to avoid multiplying a report if catalog metadata has
    # accidentally been duplicated for a multi-valued metadata property.
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        graph_term = row.get("reportGraph")
        if graph_term is None:
            continue
        graph_iri = _term_string(graph_term)
        if not graph_iri:
            continue
        unique[graph_iri] = {
            "report_graph_iri": graph_iri,
            "validation_id": _term_string(row.get("validationId")) or "",
            "dataset_id": _term_string(row.get("datasetId")) or "",
            "shape_model": _term_string(row.get("shapeModel")) or "",
            "report_sha256": _term_string(row.get("reportSha256")) or "",
            "report_triples": _term_string(row.get("reportTriples")) or "",
            "stored_at": _term_string(row.get("storedAt")) or "",
        }
    return sorted(unique.values(), key=lambda item: (item["shape_model"], item["dataset_id"], item["validation_id"]))


def load_qualitative_results(
    report_store,
    *,
    shape_models: Sequence[str] | None = None,
    severities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Extract individual SHACL findings from Oxigraph for analysis consumers.

    This is the integration boundary for downstream semantic clustering: rows
    contain stable report metadata and the normalized ``embedding_text`` field,
    while the SHACL result node identifier remains scoped to its report graph.
    The function does not write to the store or to the filesystem.
    """
    reports = _load_report_catalog(report_store)
    selected_models = set(shape_models or [])
    selected_severities = set(severities or SEVERITIES)
    invalid_severities = selected_severities - set(SEVERITIES)
    if invalid_severities:
        raise ValueError("Severità non valide: " + ", ".join(sorted(invalid_severities)))
    if selected_models:
        reports = [row for row in reports if row["shape_model"] in selected_models]

    result_rows: list[dict[str, Any]] = []
    failed_graphs: list[dict[str, str]] = []
    for report in reports:
        try:
            graph = report_store.get_report_graph(report["report_graph_iri"])
        except Exception as exc:
            failed_graphs.append({"report_graph_iri": report["report_graph_iri"], "error": str(exc)})
            continue
        for row in _extract_qualitative_results(report, graph):
            if row["severity"] in selected_severities:
                result_rows.append(row)

    return {
        "reports": reports,
        "results": result_rows,
        "failed_graphs": failed_graphs,
        "filters": {
            "shape_models": sorted(selected_models),
            "severities": sorted(selected_severities),
        },
    }


def _aggregate_qualitative_rows(result_rows: Sequence[Mapping[str, Any]], reports: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    profile_reports: dict[str, set[str]] = defaultdict(set)
    profile_datasets: dict[str, set[str]] = defaultdict(set)
    for report in reports:
        profile_reports[report["shape_model"]].add(report["report_graph_iri"])
        profile_datasets[report["shape_model"]].add(report["dataset_id"])

    component_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    path_groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    message_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    focus_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    structural_groups: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)

    for row in result_rows:
        profile = str(row["shape_model"])
        severity = str(row["severity"])
        component = row["source_constraint_component"] or "(componente non specificata)"
        component_groups[(profile, severity, component)].append(row)
        path_groups[(profile, severity, row["path_type"], row["path"])].append(row)
        message_groups[(profile, severity, row["message_signature"])].append(row)
        try:
            focus_nodes = json.loads(row["focus_nodes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            focus_nodes = []
        for focus_node in focus_nodes:
            focus_groups[(profile, severity, focus_node)].append(row)
        structural_groups[(profile, severity, component, row["path_type"], row["path"])].append(row)

    profile_summary: list[dict[str, Any]] = []
    all_profiles = sorted(set(profile_reports) | {str(row["shape_model"]) for row in result_rows}, key=_profile_sort_key)
    for profile in all_profiles:
        records = [row for row in result_rows if row["shape_model"] == profile]
        counts = Counter(row["severity"] for row in records)
        profile_summary.append({
            "shape_model": profile,
            "reports": len(profile_reports.get(profile, set())),
            "datasets": len(profile_datasets.get(profile, set())),
            "validation_results": len(records),
            "violations": counts["Violation"],
            "warnings": counts["Warning"],
            "infos": counts["Info"],
            "unspecified_or_unknown": counts["Unspecified"] + counts["Unknown"],
        })

    component_summary = []
    for (profile, severity, component), rows in sorted(component_groups.items()):
        component_summary.append({
            "shape_model": profile,
            "severity": severity,
            "constraint_component": component,
            "occurrences": len(rows),
            "affected_datasets": len({row["dataset_id"] for row in rows}),
            "affected_reports": len({row["report_graph_iri"] for row in rows}),
            "example_paths": " || ".join(sorted({row["path"] for row in rows})[:5]),
            "example_messages": " || ".join(sorted({row["message"] for row in rows if row["message"]})[:5]),
        })

    path_summary = []
    for (profile, severity, path_type, path), rows in sorted(path_groups.items()):
        path_summary.append({
            "shape_model": profile,
            "severity": severity,
            "path_type": path_type,
            "path": path,
            "occurrences": len(rows),
            "affected_datasets": len({row["dataset_id"] for row in rows}),
            "affected_reports": len({row["report_graph_iri"] for row in rows}),
            "constraint_components": " | ".join(sorted({row["source_constraint_component"] for row in rows if row["source_constraint_component"]})),
            "example_messages": " || ".join(sorted({row["message"] for row in rows if row["message"]})[:5]),
        })

    message_summary = []
    for (profile, severity, signature), rows in sorted(message_groups.items()):
        message_summary.append({
            "shape_model": profile,
            "severity": severity,
            "message_signature": signature,
            "occurrences": len(rows),
            "affected_datasets": len({row["dataset_id"] for row in rows}),
            "constraint_components": " | ".join(sorted({row["source_constraint_component"] for row in rows if row["source_constraint_component"]})),
            "paths": " | ".join(sorted({row["path"] for row in rows})),
        })

    focus_summary = []
    for (profile, severity, focus_node), rows in sorted(focus_groups.items()):
        focus_summary.append({
            "shape_model": profile,
            "severity": severity,
            "focus_node": focus_node,
            "occurrences": len(rows),
            "affected_datasets": len({row["dataset_id"] for row in rows}),
            "constraint_components": " | ".join(sorted({row["source_constraint_component"] for row in rows if row["source_constraint_component"]})),
            "paths": " | ".join(sorted({row["path"] for row in rows})),
            "example_messages": " || ".join(sorted({row["message"] for row in rows if row["message"]})[:5]),
        })

    structural_summary = []
    for (profile, severity, component, path_type, path), rows in sorted(structural_groups.items()):
        structural_summary.append({
            "shape_model": profile,
            "severity": severity,
            "constraint_component": component,
            "path_type": path_type,
            "path": path,
            "occurrences": len(rows),
            "affected_datasets": len({row["dataset_id"] for row in rows}),
            "affected_reports": len({row["report_graph_iri"] for row in rows}),
            "message_signatures": len({row["message_signature"] for row in rows if row["message_signature"]}),
            "example_messages": " || ".join(sorted({row["message"] for row in rows if row["message"]})[:5]),
        })

    return {
        "profile_summary": profile_summary,
        "component_summary": component_summary,
        "path_summary": path_summary,
        "message_summary": message_summary,
        "focus_summary": focus_summary,
        "structural_summary": structural_summary,
    }


def analyze_qualitative(
    report_store_path: Path | str,
    output_dir: Path | str,
    *,
    report_store=None,
    shape_models: Sequence[str] | None = None,
    severities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Extract and summarize qualitative SHACL findings using PyOxigraph only."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if report_store is None:
        from src.report_store import ValidationReportStore
        report_store = ValidationReportStore(report_store_path, read_only=True)

    extracted = load_qualitative_results(
        report_store, shape_models=shape_models, severities=severities
    )
    reports = extracted["reports"]
    result_rows = extracted["results"]
    failed_graphs = extracted["failed_graphs"]
    selected_models = extracted["filters"]["shape_models"]
    selected_severities = extracted["filters"]["severities"]
    aggregates = _aggregate_qualitative_rows(result_rows, reports)
    report_headers = ["report_graph_iri", "validation_id", "dataset_id", "shape_model", "report_sha256", "report_triples", "stored_at"]
    result_headers = [
        "report_graph_iri", "validation_id", "dataset_id", "shape_model", "report_sha256", "result_node",
        "severity", "source_constraint_component", "source_constraint_component_iri", "path_type", "path",
        "message", "message_signature", "focus_nodes", "values", "source_shapes", "source_constraints", "embedding_text",
    ]
    _write_csv(out / "qualitative_report_inventory.csv", report_headers, reports)
    _write_csv(out / "qualitative_results.csv", result_headers, result_rows)
    _write_csv(out / "qualitative_profile_summary.csv", [
        "shape_model", "reports", "datasets", "validation_results", "violations", "warnings", "infos", "unspecified_or_unknown",
    ], aggregates["profile_summary"])
    _write_csv(out / "qualitative_components.csv", [
        "shape_model", "severity", "constraint_component", "occurrences", "affected_datasets", "affected_reports",
        "example_paths", "example_messages",
    ], aggregates["component_summary"])
    _write_csv(out / "qualitative_paths.csv", [
        "shape_model", "severity", "path_type", "path", "occurrences", "affected_datasets", "affected_reports",
        "constraint_components", "example_messages",
    ], aggregates["path_summary"])
    _write_csv(out / "qualitative_messages.csv", [
        "shape_model", "severity", "message_signature", "occurrences", "affected_datasets", "constraint_components", "paths",
    ], aggregates["message_summary"])
    _write_csv(out / "qualitative_focus_nodes.csv", [
        "shape_model", "severity", "focus_node", "occurrences", "affected_datasets", "constraint_components", "paths", "example_messages",
    ], aggregates["focus_summary"])
    _write_csv(out / "qualitative_structural_patterns.csv", [
        "shape_model", "severity", "constraint_component", "path_type", "path", "occurrences",
        "affected_datasets", "affected_reports", "message_signatures", "example_messages",
    ], aggregates["structural_summary"])
    _write_csv(out / "qualitative_report_errors.csv", ["report_graph_iri", "error"], failed_graphs)

    summary = {
        "source": "PyOxigraph",
        "store": str(Path(report_store_path).resolve()),
        "catalogued_reports_selected": len(reports),
        "reports_read": len(reports) - len(failed_graphs),
        "reports_unreadable": len(failed_graphs),
        "shape_models": len(aggregates["profile_summary"]),
        "results_included": len(result_rows),
        "severity_filter": selected_severities,
        "shape_model_filter": selected_models,
        "output_dir": str(out.resolve()),
    }
    _write_json(out / "qualitative_summary.json", summary)
    return summary


def analyze(
    *,
    mode: str,
    db_path: Path | str,
    report_store_path: Path | str,
    output_dir: Path | str,
    catalogue_id: str | None = None,
    category_id: str | None = None,
    shape_models: Sequence[str] | None = None,
    severities: Sequence[str] | None = None,
    charts: bool = True,
) -> dict[str, Any]:
    """Run one or both analysis branches into separate output directories."""
    if mode not in {"quantitative", "qualitative", "all"}:
        raise ValueError(f"Modalità di analisi non valida: {mode!r}")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    if mode in {"quantitative", "all"}:
        results["quantitative"] = analyze_quantitative(
            db_path, root / "quantitative", catalogue_id=catalogue_id,
            category_id=category_id, charts=charts,
        )
    if mode in {"qualitative", "all"}:
        results["qualitative"] = analyze_qualitative(
            report_store_path, root / "qualitative", shape_models=shape_models,
            severities=severities,
        )
    _write_json(root / "analysis_summary.json", results)
    return results

