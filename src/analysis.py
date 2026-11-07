"""Read-only descriptive analysis of DCAT-AP SHACL validation results.

Generates five charts and CSV tables using SQLite validation metadata and the
complete SHACL report graphs stored in PyOxigraph. Individual SHACL results are
materialized only in memory during analysis.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SH = "http://www.w3.org/ns/shacl#"
RDF_FIRST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first"
RDF_REST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest"
RDF_NIL = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database non trovato: {db_path}")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_shape_model(profile: str) -> tuple[str, str] | None:
    """Return (version, level); base profiles use level='base'."""
    match = re.fullmatch(r"dcatap(\d+)(?:level([123]))?", (profile or "").casefold())
    if not match:
        return None
    digits, level_number = match.groups()
    if len(digits) == 1:
        version = digits
    elif len(digits) == 2:
        version = f"{digits[0]}.{digits[1]}"
    else:
        version = f"{digits[0]}.{digits[1]}.{int(digits[2:])}"
    return version, f"level{level_number}" if level_number else "base"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    )
    if value == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
        return "rdf:type"
    for namespace, prefix in namespaces:
        if value.startswith(namespace):
            return prefix + value[len(namespace):]
    return value


def _rdf_path_expression(graph, node, depth: int = 0, seen: set | None = None) -> str:
    """Render a SHACL path expression without exposing generated blank-node IDs."""
    from rdflib import BNode, URIRef

    if depth > 12:
        return "<percorso complesso: profondità massima>"
    seen = set() if seen is None else seen
    if node in seen:
        return "<percorso ciclico>"
    if isinstance(node, URIRef):
        return _short_iri(str(node))
    if not isinstance(node, BNode):
        return str(node)
    seen = seen | {node}

    def one(predicate: str):
        values = list(graph.objects(node, URIRef(predicate)))
        return values[0] if len(values) == 1 else None

    inverse = one(SH + "inversePath")
    if inverse is not None:
        return f"inverse({_rdf_path_expression(graph, inverse, depth + 1, seen)})"
    for predicate, operator in (("zeroOrMorePath", "*"), ("oneOrMorePath", "+"), ("zeroOrOnePath", "?")):
        child = one(SH + predicate)
        if child is not None:
            return f"({_rdf_path_expression(graph, child, depth + 1, seen)}){operator}"
    alternative = one(SH + "alternativePath")
    if alternative is not None:
        members = _rdf_list(graph, alternative)
        if members is not None:
            return "(" + " | ".join(_rdf_path_expression(graph, item, depth + 1, seen) for item in members) + ")"
    # A blank-node SHACL sequence path is encoded as an RDF list itself.
    members = _rdf_list(graph, node)
    if members is not None:
        return "/".join(_rdf_path_expression(graph, item, depth + 1, seen) for item in members)
    return "<percorso SHACL complesso non risolto>"


def _rdf_list(graph, head):
    from rdflib import BNode, URIRef
    items, current, seen = [], head, set()
    nil = URIRef(RDF_NIL)
    while current != nil:
        if current in seen or len(items) > 100:
            return None
        seen.add(current)
        first = list(graph.objects(current, URIRef(RDF_FIRST)))
        rest = list(graph.objects(current, URIRef(RDF_REST)))
        if len(first) != 1 or len(rest) != 1:
            return None
        items.append(first[0])
        current = rest[0]
    return items


def _resolve_path(graph, result_path) -> tuple[str, str]:
    """Classify a SHACL path term and render complex paths canonically."""
    from rdflib import BNode, URIRef

    if result_path is None:
        return "missing", "(resultPath assente)"
    if isinstance(result_path, URIRef):
        return "simple_iri", _short_iri(str(result_path))
    if isinstance(result_path, BNode):
        expression = _rdf_path_expression(graph, result_path)
        if expression == "<percorso SHACL complesso non risolto>":
            return "complex_unresolved", expression
        return "complex_resolved", expression
    return "unclassified", "<percorso non classificabile>"


def _local_name(term) -> str | None:
    if term is None:
        return None
    value = str(term)
    return value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _extract_result_rows(validation, graph) -> list[dict[str, Any]]:
    """Build transient analysis records from a complete report graph.

    No ValidationResult is persisted in SQLite: this conversion exists only in
    memory while a report is analysed.
    """
    from rdflib import RDF
    from rdflib.namespace import SH

    rows: list[dict[str, Any]] = []
    nodes = graph.subjects(RDF.type, SH.ValidationResult)
    for node in nodes:
        severities = list(graph.objects(node, SH.resultSeverity))
        severity = _local_name(severities[0]) if severities else None
        if severity not in {"Violation", "Warning", "Info"}:
            continue
        paths = list(graph.objects(node, SH.resultPath))
        path_type, path_label = _resolve_path(graph, paths[0] if paths else None)
        components = list(graph.objects(node, SH.sourceConstraintComponent))
        rows.append({
            "validation_id": validation["id"],
            "dataset_id": validation["dataset_id"],
            "shape_model": validation["shape_model"],
            "severity": severity,
            "path_type": path_type,
            "path_label": path_label,
            "source_constraint_component": _local_name(components[0]) if components else None,
        })
    return rows


def analyze_database(
    db_path: Path,
    output_dir: Path,
    report_store_path: Path = Path("data/validation-reports"),
    report_store=None,
) -> dict[str, Any]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Installare matplotlib e numpy: pip install matplotlib numpy") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    with connect_readonly(db_path) as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        fetch_counts = conn.execute("SELECT fetch_status, COUNT(*) n FROM datasets GROUP BY fetch_status").fetchall()
        validations = conn.execute("""
            SELECT v.id, v.dataset_id, v.validator_url, v.shape_model, v.conforms,
                   v.results, v.violations, v.warnings, v.infos,
                   v.report_graph_iri, v.report_format
            FROM validations v
            WHERE v.execution_status='validated' AND v.http_status=200
              AND v.conforms IS NOT NULL AND (v.error IS NULL OR TRIM(v.error)='')
              AND v.report_status='stored' AND v.report_graph_iri IS NOT NULL
            ORDER BY v.shape_model, v.dataset_id
        """).fetchall()

    if report_store is None:
        from src.report_store import ValidationReportStore
        report_store = ValidationReportStore(report_store_path, read_only=True)

    # The report remains RDF in Oxigraph. These small dictionaries are transient
    # analysis inputs, not a second persisted representation of SHACL results.
    result_rows: list[dict[str, Any]] = []
    for validation in validations:
        graph = report_store.get_report_graph(validation["report_graph_iri"])
        result_rows.extend(_extract_result_rows(validation, graph))

    # Summary per profile, including the three mutually exclusive outcome groups.
    summary = defaultdict(lambda: Counter({"validated": 0, "conforming": 0, "violations": 0, "warnings": 0, "infos": 0, "with_violations": 0, "without_violations_with_findings": 0, "no_findings": 0}))
    per_validation = []
    for row in validations:
        profile = row["shape_model"] or "(shape model non specificato)"
        stat = summary[profile]
        stat["validated"] += 1
        stat["conforming"] += int(row["conforms"] == 1)
        stat["violations"] += int(row["violations"] or 0)
        stat["warnings"] += int(row["warnings"] or 0)
        stat["infos"] += int(row["infos"] or 0)
        v, w, i = int(row["violations"] or 0), int(row["warnings"] or 0), int(row["infos"] or 0)
        stat["with_violations"] += int(v > 0)
        stat["without_violations_with_findings"] += int(v == 0 and (w + i) > 0)
        stat["no_findings"] += int(v == 0 and w == 0 and i == 0)
        per_validation.append({"validation_id": row["id"], "dataset_id": row["dataset_id"],
            "shape_model": profile, "violations": v, "warnings": w, "infos": i,
            "results": int(row["results"] or 0), "conforms": row["conforms"]})

    profile_rows = []
    for profile in sorted(summary):
        s = summary[profile]
        n = s["validated"]
        profile_rows.append({"shape_model": profile, **s,
            "conformance_pct": round(100*s["conforming"]/n, 2) if n else 0,
            "no_findings_pct": round(100*s["no_findings"]/n, 2) if n else 0,
            "findings_without_violations_pct": round(100*s["without_violations_with_findings"]/n, 2) if n else 0,
            "with_violations_pct": round(100*s["with_violations"]/n, 2) if n else 0})

    # Paired comparisons: same dataset, validator endpoint and same SHACL level.
    by_pair_key = {}
    for row in validations:
        parsed = parse_shape_model(row["shape_model"] or "")
        if not parsed:
            continue
        version, level = parsed
        by_pair_key[(row["dataset_id"], row["validator_url"], level, version)] = row
    versions = sorted({key[3] for key in by_pair_key}, key=_version_key)
    pairs = []
    for level in sorted({key[2] for key in by_pair_key}):
        for old, new in zip(versions, versions[1:]):
            for (dataset_id, endpoint, lev, version), a in list(by_pair_key.items()):
                if lev != level or version != old:
                    continue
                b = by_pair_key.get((dataset_id, endpoint, level, new))
                if b is None:
                    continue
                pairs.append({"dataset_id": dataset_id, "level": level,
                    "from_version": old, "to_version": new,
                    "from_profile": a["shape_model"], "to_profile": b["shape_model"],
                    "delta_violations": int(b["violations"] or 0)-int(a["violations"] or 0),
                    "delta_warnings": int(b["warnings"] or 0)-int(a["warnings"] or 0)})
    transition_summary = []
    grouped = {}
    for p in pairs:
        key = (p["level"], p["from_version"], p["to_version"])
        g = grouped.setdefault(key, {"paired_datasets": 0, "violations_increased": 0,
            "violations_decreased": 0, "violations_unchanged": 0,
            "warnings_increased": 0, "warnings_decreased": 0, "warnings_unchanged": 0})
        g["paired_datasets"] += 1
        for field, delta in (("violations", p["delta_violations"]), ("warnings", p["delta_warnings"])):
            g[f"{field}_{'increased' if delta > 0 else 'decreased' if delta < 0 else 'unchanged'}"] += 1
    for (level, old, new), values in sorted(grouped.items()):
        transition_summary.append({"level": level, "from_version": old, "to_version": new, **values})

    # Classify paths from the RDF graph itself; blank-node identifiers are never
    # treated as properties or compared across reports.
    path_groups = defaultdict(lambda: {"occurrences": 0, "datasets": set()})
    component_groups = defaultdict(lambda: {"occurrences": 0, "datasets": set()})
    for row in result_rows:
        path_type, path_label = row["path_type"], row["path_label"]
        severity = row["severity"]
        path_groups[(row["shape_model"], severity, path_type, path_label)]["occurrences"] += 1
        path_groups[(row["shape_model"], severity, path_type, path_label)]["datasets"].add(row["dataset_id"])
        component = row["source_constraint_component"] or "(componente non specificata)"
        component_groups[(row["shape_model"], severity, component)]["occurrences"] += 1
        component_groups[(row["shape_model"], severity, component)]["datasets"].add(row["dataset_id"])

    path_records = []
    for (profile, severity, path_type, label), values in sorted(path_groups.items()):
        path_records.append({"shape_model": profile, "severity": severity, "path_type": path_type,
            "path": label, "occurrences": values["occurrences"],
            "affected_datasets": len(values["datasets"])})
    component_records = []
    for (profile, severity, component), values in sorted(component_groups.items()):
        denom = next((r["validated"] for r in profile_rows if r["shape_model"] == profile), 0)
        component_records.append({"shape_model": profile, "severity": severity,
            "constraint_component": component, "occurrences": values["occurrences"],
            "affected_datasets": len(values["datasets"]), "validated_datasets": denom,
            "affected_pct": round(100*len(values["datasets"])/denom, 2) if denom else 0})

    # Global property ranking is restricted to direct IRI paths. Complex paths
    # remain separately visible in the CSV with their canonical expression.
    property_aggregate = Counter()
    property_datasets = {}
    for r in path_records:
        if r["path_type"] != "simple_iri" or r["severity"] != "Violation":
            continue
        property_aggregate[r["path"]] += r["occurrences"]
        property_datasets.setdefault(r["path"], set()).update(
            # Affected dataset IDs are accumulated below from raw result rows.
            []
        )
    property_dataset_sets = {}
    for row in result_rows:
        if row["severity"] != "Violation":
            continue
        if row["path_type"] == "simple_iri":
            property_dataset_sets.setdefault(row["path_label"], set()).add(row["dataset_id"])
    property_records = [{"property": name, "occurrences": count,
        "affected_datasets": len(property_dataset_sets.get(name, set()))}
        for name, count in property_aggregate.most_common()]

    _write_csv(output_dir/"profile_summary.csv", list(profile_rows[0].keys()) if profile_rows else
        ["shape_model","validated","conforming","with_violations","without_violations_with_findings","no_findings"], profile_rows)
    _write_csv(output_dir/"validation_distribution.csv",
        ["validation_id","dataset_id","shape_model","violations","warnings","infos","results","conforms"], per_validation)
    _write_csv(output_dir/"paired_version_deltas.csv",
        ["dataset_id","level","from_version","to_version","from_profile","to_profile","delta_violations","delta_warnings"], pairs)
    _write_csv(output_dir/"paired_version_summary.csv",
        ["level","from_version","to_version","paired_datasets","violations_increased","violations_decreased","violations_unchanged","warnings_increased","warnings_decreased","warnings_unchanged"], transition_summary)
    _write_csv(output_dir/"shacl_components.csv",
        ["shape_model","severity","constraint_component","occurrences","affected_datasets","validated_datasets","affected_pct"], component_records)
    _write_csv(output_dir/"shacl_paths.csv",
        ["shape_model","severity","path_type","path","occurrences","affected_datasets"], path_records)
    _write_csv(output_dir/"violations_by_rdf_property.csv",
        ["property","occurrences","affected_datasets"], property_records)

    # Chart 1: mutually exclusive validation outcome distribution.
    if profile_rows:
        labels = [r["shape_model"] for r in profile_rows]
        fig, ax = plt.subplots(figsize=(max(11, len(labels)*0.68), 7))
        bottom = np.zeros(len(labels))
        for key, label in (("no_findings_pct", "Nessun risultato"),
                           ("findings_without_violations_pct", "Risultati senza violation"),
                           ("with_violations_pct", "Almeno una violation")):
            vals = np.array([r[key] for r in profile_rows])
            ax.bar(labels, vals, bottom=bottom, label=label)
            bottom += vals
        ax.set_ylim(0, 100); ax.set_ylabel("Validazioni (%)")
        ax.set_xlabel("Shape model"); ax.set_title("Distribuzione degli esiti per shape model")
        ax.tick_params(axis="x", rotation=65); ax.legend()
        ax.grid(axis="y", linestyle=":", alpha=.4); ax.set_axisbelow(True)
        fig.tight_layout(); fig.savefig(output_dir/"validation_outcomes.png", dpi=220, bbox_inches="tight"); plt.close(fig)

    # Chart 2: two boxplot panels, raw per-dataset distributions.
    if per_validation:
        labels = [r["shape_model"] for r in profile_rows]
        fig, axes = plt.subplots(2, 1, figsize=(max(11, len(labels)*.7), 10), sharex=True)
        for ax, field, title in ((axes[0], "violations", "Violation per validazione"),
                                 (axes[1], "warnings", "Warning per validazione")):
            data = [[r[field] for r in per_validation if r["shape_model"] == label] for label in labels]
            ax.boxplot(data, tick_labels=labels, showfliers=True)
            ax.set_ylabel("Numero di risultati"); ax.set_title(title)
            ax.grid(axis="y", linestyle=":", alpha=.4); ax.set_axisbelow(True)
        axes[1].tick_params(axis="x", rotation=65)
        fig.suptitle("Distribuzione dei risultati SHACL tra dataset")
        fig.tight_layout(); fig.savefig(output_dir/"shacl_result_distributions.png", dpi=220, bbox_inches="tight"); plt.close(fig)

    # Chart 3: paired deltas, grouped by the same validation level.
    if transition_summary:
        labels = [f"{r['level']}\n{r['from_version']} → {r['to_version']}" for r in transition_summary]
        x = np.arange(len(labels)); width = .36
        fig, axes = plt.subplots(2, 1, figsize=(max(10, len(labels)*.85), 9), sharex=True)
        for ax, prefix, title in ((axes[0], "violations", "Variazione delle violation"),
                                  (axes[1], "warnings", "Variazione dei warning")):
            inc = [r[f"{prefix}_increased"] for r in transition_summary]
            dec = [r[f"{prefix}_decreased"] for r in transition_summary]
            same = [r[f"{prefix}_unchanged"] for r in transition_summary]
            ax.bar(x-width, inc, width, label="Aumentati")
            ax.bar(x, dec, width, label="Diminuiti")
            ax.bar(x+width, same, width, label="Invariati")
            ax.set_ylabel("Numero di dataset"); ax.set_title(title); ax.legend()
            ax.grid(axis="y", linestyle=":", alpha=.4); ax.set_axisbelow(True)
        axes[1].set_xticks(x, labels, rotation=35, ha="right")
        fig.suptitle("Confronto appaiato dei risultati tra versioni DCAT-AP")
        fig.tight_layout(); fig.savefig(output_dir/"paired_version_changes.png", dpi=220, bbox_inches="tight"); plt.close(fig)

    # Chart 4: component prevalence heatmaps, separate panels by severity.
    if component_records:
        components = sorted({r["constraint_component"] for r in component_records})
        profiles = [r["shape_model"] for r in profile_rows]
        fig, axes = plt.subplots(2, 1, figsize=(max(11, len(profiles)*.7), max(7, len(components)*.42)), sharex=True)
        for ax, severity in zip(axes, ("Violation", "Warning")):
            matrix = np.zeros((len(components), len(profiles)))
            lookup = {(r["shape_model"], r["constraint_component"]): r["affected_pct"]
                      for r in component_records if r["severity"] == severity}
            for yi, component in enumerate(components):
                for xi, profile in enumerate(profiles):
                    matrix[yi, xi] = lookup.get((profile, component), 0)
            image = ax.imshow(matrix, aspect="auto", vmin=0, vmax=100)
            ax.set_yticks(np.arange(len(components)), components)
            ax.set_title(f"{severity}: dataset interessati (%)")
            fig.colorbar(image, ax=ax, label="Dataset (%)")
        axes[1].set_xticks(np.arange(len(profiles)), profiles, rotation=65, ha="right")
        fig.suptitle("Prevalenza delle componenti SHACL per configurazione")
        fig.tight_layout(); fig.savefig(output_dir/"shacl_component_heatmap.png", dpi=220, bbox_inches="tight"); plt.close(fig)

    # Chart 5: top 15 direct RDF properties, ranked by distinct datasets affected.
    if property_records:
        top = sorted(property_records, key=lambda r: (r["affected_datasets"], r["occurrences"]), reverse=True)[:15]
        top.reverse()
        fig, ax = plt.subplots(figsize=(11, max(5.5, .38*len(top))))
        ax.barh([r["property"] for r in top], [r["affected_datasets"] for r in top])
        ax.set_xlabel("Dataset interessati (almeno una violation)")
        ax.set_ylabel("Proprietà RDF diretta")
        ax.set_title("Proprietà RDF maggiormente coinvolte nelle violation")
        ax.grid(axis="x", linestyle=":", alpha=.4); ax.set_axisbelow(True)
        fig.tight_layout(); fig.savefig(output_dir/"rdf_property_violations.png", dpi=220, bbox_inches="tight"); plt.close(fig)

    charts = ["validation_outcomes.png", "shacl_result_distributions.png", "paired_version_changes.png",
              "shacl_component_heatmap.png", "rdf_property_violations.png"]
    return {"datasets_in_inventory": dataset_count,
        "fetch_status": {r[0]: r[1] for r in fetch_counts},
        "successful_validation_rows": len(validations), "profiles": len(profile_rows),
        "paired_comparisons": len(pairs), "shacl_result_rows": len(result_rows),
        "path_categories": dict(Counter(r["path_type"] for r in path_records)),
        "charts_generated": sum((output_dir/name).is_file() for name in charts),
        "output_dir": str(output_dir.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description="Analisi descrittiva read-only di SQLite e dei report MQA in PyOxigraph")
    parser.add_argument("--db", type=Path, default=Path("analysis.db"), help="Percorso di analysis.db")
    parser.add_argument("--store", type=Path, default=Path("data/validation-reports"), help="Percorso del triplestore PyOxigraph")
    parser.add_argument("--out", type=Path, default=Path("analysis-output"), help="Directory output CSV/PNG")
    args = parser.parse_args()
    for key, value in analyze_database(args.db, args.out, args.store).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
