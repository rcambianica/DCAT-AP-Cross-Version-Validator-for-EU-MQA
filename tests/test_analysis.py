import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from src.analysis import (
    analyze,
    analyze_quantitative,
    load_qualitative_results,
    parse_shape_model,
)
from src.validator import SH


class FakeReportStore:
    def __init__(self, graphs):
        self.graphs = graphs

    def get_report_graph(self, graph_iri):
        return self.graphs[graph_iri]


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
                "quantitative_summary.json",
            }
            self.assertTrue(expected.issubset({item.name for item in (root / "out").iterdir()}))

            with (root / "out" / "profile_summary.csv").open(encoding="utf-8", newline="") as stream:
                rows = {row["shape_model"]: row for row in csv.DictReader(stream)}
            self.assertEqual(rows["dcatap300"]["shape_level"], "all")
            self.assertEqual(rows["dcatap300level1"]["shape_level"], "level1")

    def test_analyze_rejects_unknown_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                analyze(
                    mode="clustering", db_path=root / "analysis.db",
                    report_store_path=root / "store", output_dir=root / "out",
                )

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
