import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from src.analysis import (
    _load_report_catalog,
    analyze,
    analyze_qualitative,
    analyze_quantitative,
    _build_sequence_comparison_summaries,
    _build_sequence_profile_count_rows,
    _generate_quantitative_charts,
    COMPARISON_SEQUENCES,
    _quantitative_profile_rows,
    load_qualitative_results,
    parse_shape_model,
)
from src.validator import SH


class FakeReportStore:
    def __init__(self, graphs):
        self.graphs = graphs

    def get_report_graph(self, graph_iri):
        return self.graphs[graph_iri]


class FakeOxigraphTerm:
    """Mimic PyOxigraph's value/str distinction for RDF terms."""

    def __init__(self, value, serialized):
        self.value = value
        self.serialized = serialized

    def __str__(self):
        return self.serialized


class FakeVariable:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "?" + self.name


class FakeSolution:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, variable):
        return self.values[variable.name]


class FakeQuerySolutions:
    def __init__(self, rows):
        self.variables = [FakeVariable(name) for name in rows[0]] if rows else []
        self.rows = [FakeSolution(row) for row in rows]

    def __iter__(self):
        return iter(self.rows)


class FakeCatalogStore:
    def query(self, _sparql):
        return FakeQuerySolutions([{
            "reportGraph": FakeOxigraphTerm(
                "urn:mqa:validation-report:1067:abc123",
                "<urn:mqa:validation-report:1067:abc123>",
            ),
            "validationId": FakeOxigraphTerm(1067, '"1067"^^<http://www.w3.org/2001/XMLSchema#integer>'),
            "datasetId": FakeOxigraphTerm("ds596", '"ds596"'),
            "shapeModel": FakeOxigraphTerm("dcatap301level1", '"dcatap301level1"'),
            "reportSha256": FakeOxigraphTerm("abc123", '"abc123"'),
            "reportTriples": FakeOxigraphTerm(42, '"42"^^<http://www.w3.org/2001/XMLSchema#integer>'),
            "storedAt": FakeOxigraphTerm("2026-11-09T10:00:00+00:00", '"2026-11-09T10:00:00+00:00"^^<http://www.w3.org/2001/XMLSchema#dateTime>'),
        }])


def make_report() -> Graph:
    graph = Graph()
    ex = Namespace("https://example.org/")
    result = BNode()
    graph.add((ex.report, RDF.type, SH.ValidationReport))
    graph.add((ex.report, SH.conforms, Literal(False)))
    graph.add((result, RDF.type, SH.ValidationResult))
    graph.add((result, SH.resultSeverity, SH.Violation))
    graph.add((result, SH.resultPath, URIRef("http://purl.org/dc/terms/title")))
    graph.add((result, SH.sourceConstraintComponent, SH.MinCountConstraintComponent))
    graph.add((result, SH.focusNode, ex.dataset))
    graph.add((result, SH.value, Literal("dataset-specific-value")))
    graph.add((result, SH.resultMessage, Literal("A title is required")))
    return graph


def make_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE datasets (
                dataset_id TEXT PRIMARY KEY, catalogue_id TEXT, category_id TEXT,
                fetch_status TEXT, fetch_http_status INTEGER, fetch_error TEXT
            );
            CREATE TABLE validations (
                id INTEGER PRIMARY KEY, dataset_id TEXT, validator_url TEXT,
                shape_model TEXT, conforms INTEGER, results INTEGER,
                violations INTEGER, warnings INTEGER, infos INTEGER,
                http_status INTEGER, error TEXT, execution_status TEXT,
                validated_at TEXT, report_status TEXT
            );
            INSERT INTO datasets VALUES ('ds1','dati-gov-it','INTR','ok',200,NULL);
            INSERT INTO validations VALUES
                (1,'ds1','http://validator','dcatap300level1',0,1,1,0,0,200,NULL,'validated','2026-01-01','stored'),
                (2,'ds1','http://validator','dcatap301level1',0,2,2,0,0,200,NULL,'validated','2026-01-02','stored'),
                (3,'ds1','http://validator','dcatap300',0,4,4,0,0,200,NULL,'validated','2026-01-01','stored');
        """)


class AnalysisTests(unittest.TestCase):
    def test_parse_shape_model_uses_all_for_unsuffixed_profile(self):
        self.assertEqual(parse_shape_model("dcatap300"), ("3.0.0", "all"))
        self.assertEqual(parse_shape_model("dcatap211level2"), ("2.1.1", "level2"))
        self.assertEqual(parse_shape_model("dcatap121level1"), ("1.2.1", "level1"))
        self.assertIsNone(parse_shape_model("dcatap300orig"))

    def test_quantitative_analysis_reads_sqlite_and_writes_current_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "test.db"
            make_database(db)

            summary = analyze_quantitative(db, root / "out", charts=False)

            self.assertEqual(summary["datasets_in_scope"], 1)
            self.assertEqual(summary["successful_validations_included"], 3)
            self.assertEqual(summary["shape_models"], 3)
            self.assertEqual(summary["paired_comparisons"], 1)
            self.assertEqual(summary["charts_generated"], [])
            expected = {
                "dataset_fetch_status.csv", "validation_execution_status.csv",
                "profile_summary.csv", "validation_distribution.csv",
                "paired_version_deltas.csv", "paired_version_summary.csv",
                "sequence_profile_counts.csv", "quantitative_summary.json",
            }
            self.assertTrue(expected.issubset({item.name for item in (root / "out").iterdir()}))

            with (root / "out" / "profile_summary.csv").open(encoding="utf-8", newline="") as stream:
                rows = {row["shape_model"]: row for row in csv.DictReader(stream)}
            self.assertEqual(rows["dcatap300"]["shape_level"], "all")
            self.assertEqual(rows["dcatap300level1"]["shape_level"], "level1")
            self.assertIn("nonconforming_with_violations_pct", rows["dcatap300level1"])


    def test_outcome_classes_use_conformance_and_violation_presence(self):
        validations = [
            {"shape_model": "dcatap301level1", "dataset_id": "ds1", "conforms": 1, "violations": 0, "warnings": 0, "infos": 0, "results": 0},
            {"shape_model": "dcatap301level1", "dataset_id": "ds2", "conforms": 0, "violations": 2, "warnings": 0, "infos": 0, "results": 2},
            {"shape_model": "dcatap301level1", "dataset_id": "ds3", "conforms": 0, "violations": 0, "warnings": 2, "infos": 0, "results": 2},
            {"shape_model": "dcatap301level1", "dataset_id": "ds4", "conforms": 0, "violations": 0, "warnings": 0, "infos": 1, "results": 1},
            {"shape_model": "dcatap301level1", "dataset_id": "ds5", "conforms": 0, "violations": 0, "warnings": 1, "infos": 1, "results": 2},
        ]

        row = _quantitative_profile_rows(validations)[0]
        self.assertEqual(row["conforming_validations"], 1)
        self.assertEqual(row["nonconforming_with_violations"], 1)
        self.assertEqual(row["nonconforming_without_violations"], 3)
        self.assertEqual(row["conformity_pct"], 20.0)
        self.assertEqual(row["nonconforming_with_violations_pct"], 20.0)
        self.assertEqual(row["nonconforming_without_violations_pct"], 60.0)

    def test_sequence_comparisons_follow_the_requested_profile_order(self):
        validations = []
        for index, profile in enumerate((
            "dcatap301level1", "dcatap301level2", "dcatap301level3", "dcatap301",
            "dcatap300level1", "dcatap300level2", "dcatap300level3", "dcatap300",
            "dcatap211level1", "dcatap211level2", "dcatap211level3", "dcatap211",
            "dcatap210level1", "dcatap210",
            "dcatap11", "dcatap12", "dcatap121", "dcatap201",
        )):
            validations.append({
                "shape_model": profile,
                "dataset_id": "ds1",
                "validator_url": "http://validator",
                "violations": index,
                "warnings": 18 - index,
            })

        summaries = _build_sequence_comparison_summaries(validations)
        self.assertEqual(list(summaries), ["dcatap301", "dcatap300", "dcatap211", "dcatap210", "legacy"])
        self.assertEqual(
            [(row["from_profile"], row["to_profile"]) for row in summaries["dcatap301"]],
            [
                ("dcatap301level1", "dcatap301level2"),
                ("dcatap301level2", "dcatap301level3"),
                ("dcatap301level3", "dcatap301"),
            ],
        )
        self.assertEqual(
            [(row["from_profile"], row["to_profile"]) for row in summaries["legacy"]],
            [
                ("dcatap11", "dcatap12"),
                ("dcatap12", "dcatap121"),
                ("dcatap121", "dcatap201"),
            ],
        )
        self.assertTrue(all(row["paired_datasets"] == 1 for rows in summaries.values() for row in rows))

    def test_sequence_profile_counts_sum_findings_and_expose_sample_size(self):
        validations = [
            {"shape_model": "dcatap301level1", "dataset_id": "ds1", "validator_url": "v", "violations": 2, "warnings": 3},
            {"shape_model": "dcatap301level1", "dataset_id": "ds2", "validator_url": "v", "violations": 4, "warnings": 1},
            {"shape_model": "dcatap301level2", "dataset_id": "ds1", "validator_url": "v", "violations": 1, "warnings": 5},
        ]
        rows = _build_sequence_profile_count_rows(validations)
        first = next(row for row in rows if row["sequence"] == "dcatap301" and row["shape_model"] == "dcatap301level1")
        second = next(row for row in rows if row["sequence"] == "dcatap301" and row["shape_model"] == "dcatap301level2")
        third = next(row for row in rows if row["sequence"] == "dcatap301" and row["shape_model"] == "dcatap301level3")

        self.assertEqual(first["validation_count"], 2)
        self.assertEqual(first["dataset_count"], 2)
        self.assertEqual(first["total_violations"], 6)
        self.assertEqual(first["total_warnings"], 4)
        self.assertEqual(second["validation_count"], 1)
        self.assertEqual(second["total_violations"], 1)
        self.assertEqual(third["validation_count"], 0)
        self.assertEqual(third["total_violations"], "")

    def test_quantitative_charts_generate_five_profile_line_charts(self):
        validations = []
        for sequence_index, profiles in enumerate(COMPARISON_SEQUENCES.values()):
            for profile_index, profile in enumerate(profiles):
                validations.append({
                    "shape_model": profile,
                    "dataset_id": "ds1",
                    "validator_url": "http://validator",
                    "conforms": 0,
                    "violations": sequence_index + profile_index + 1,
                    "warnings": sequence_index + profile_index + 2,
                    "infos": 0,
                    "results": 0,
                })

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            obsolete = output / "paired_version_changes_dcatap301.png"
            obsolete.write_bytes(b"obsolete chart")
            profile_rows = _quantitative_profile_rows(validations)
            generated = _generate_quantitative_charts(output, profile_rows, validations)

            expected = {
                "profile_severity_trends_dcatap301.png",
                "profile_severity_trends_dcatap300.png",
                "profile_severity_trends_dcatap211.png",
                "profile_severity_trends_dcatap210.png",
                "profile_severity_trends_legacy.png",
            }
            self.assertTrue(expected.issubset(set(generated)))
            self.assertTrue(all((output / name).is_file() for name in expected))
            self.assertFalse(obsolete.exists())

    def test_analyze_rejects_unknown_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                analyze(
                    mode="clustering", db_path=root / "analysis.db",
                    report_store_path=root / "store", output_dir=root / "out",
                )

    def test_catalog_terms_use_values_instead_of_ntriples_serialization(self):
        reports = _load_report_catalog(FakeCatalogStore())
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report["report_graph_iri"], "urn:mqa:validation-report:1067:abc123")
        self.assertEqual(report["validation_id"], "1067")
        self.assertEqual(report["dataset_id"], "ds596")
        self.assertEqual(report["shape_model"], "dcatap301level1")
        self.assertEqual(report["report_sha256"], "abc123")
        self.assertEqual(report["report_triples"], "42")
        self.assertEqual(report["stored_at"], "2026-11-09T10:00:00+00:00")

    def test_qualitative_analysis_writes_filter_summary_without_name_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "report_graph_iri": "urn:mqa:validation-report:1:abc",
                "validation_id": "1",
                "dataset_id": "ds1",
                "shape_model": "dcatap301level1",
                "report_sha256": "abc",
            }
            store = FakeReportStore({report["report_graph_iri"]: make_report()})
            with patch("src.analysis._load_report_catalog", return_value=[report]):
                summary = analyze_qualitative(
                    root / "store", root / "out", report_store=store,
                    shape_models=["dcatap301level1"], severities=["Violation"],
                )

            self.assertEqual(summary["catalogued_reports_selected"], 1)
            self.assertEqual(summary["reports_read"], 1)
            self.assertEqual(summary["reports_unreadable"], 0)
            self.assertEqual(summary["results_included"], 1)
            self.assertEqual(summary["shape_model_filter"], ["dcatap301level1"])
            self.assertEqual(summary["severity_filter"], ["Violation"])
            self.assertTrue((root / "out" / "qualitative_summary.json").is_file())

    def test_qualitative_analysis_default_cli_mode_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "report_graph_iri": "urn:mqa:validation-report:1:abc",
                "validation_id": "1",
                "dataset_id": "ds1",
                "shape_model": "dcatap301level1",
                "report_sha256": "abc",
            }
            store = FakeReportStore({report["report_graph_iri"]: make_report()})
            with patch("src.analysis._load_report_catalog", return_value=[report]):
                summary = analyze_qualitative(root / "store", root / "out", report_store=store)

            self.assertEqual(summary["shape_model_filter"], [])
            self.assertEqual(summary["severity_filter"], ["Info", "Violation", "Warning"])
            self.assertEqual(summary["results_included"], 1)
            self.assertEqual(summary["reports_unreadable"], 0)

    def test_qualitative_extraction_exposes_embedding_contract_without_identifiers(self):
        report = {
            "report_graph_iri": "urn:mqa:validation-report:1:abc",
            "validation_id": "1",
            "dataset_id": "dataset-secret-id",
            "shape_model": "dcatap301level1",
            "report_sha256": "abc",
        }
        store = FakeReportStore({report["report_graph_iri"]: make_report()})
        with patch("src.analysis._load_report_catalog", return_value=[report]):
            extracted = load_qualitative_results(store)

        self.assertEqual(len(extracted["reports"]), 1)
        self.assertEqual(len(extracted["results"]), 1)
        self.assertEqual(extracted["failed_graphs"], [])
        row = extracted["results"][0]
        self.assertEqual(row["severity"], "Violation")
        self.assertIn("MinCountConstraintComponent", row["embedding_text"])
        self.assertIn("dcterms:title", row["embedding_text"])
        self.assertIn("A title is required", row["embedding_text"])
        self.assertNotIn("dataset-secret-id", row["embedding_text"])
        self.assertNotIn("dataset-specific-value", row["embedding_text"])


if __name__ == "__main__":
    unittest.main()
