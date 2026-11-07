import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from src.db import Database
from src.profiles import _extract_shape_models
from src.validator import ShaclValidatorClient


REPORT = b'''@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .
@prefix dct: <http://purl.org/dc/terms/> .
[] a sh:ValidationReport ;
  sh:conforms false ;
  sh:result [ a sh:ValidationResult ;
    sh:focusNode ex:dataset1 ;
    sh:resultPath dct:title ;
    sh:value "Example" ;
    sh:sourceConstraintComponent sh:MinCountConstraintComponent ;
    sh:sourceShape ex:titleShape ;
    sh:resultSeverity sh:Violation ;
    sh:resultMessage "Title is required"@en ;
    ex:customProperty "preserved" ] .
'''


class FakeReportStore:
    """Small RDFLib-backed stand-in for PyOxigraph in unit tests."""

    read_only = False

    def __init__(self):
        self.graphs = {}
        self.metadata = {}

    def save_report_metadata(self, graph_iri, **metadata):
        self.metadata[graph_iri] = metadata

    def save_report(self, graph_iri, body, rdf_format):
        graph = Graph()
        graph.parse(data=body, format=rdf_format or "turtle")
        self.graphs[graph_iri] = graph
        return len(graph)

    def get_report_graph(self, graph_iri):
        return self.graphs[graph_iri]

    def dump_report(self, graph_iri, rdf_format="turtle"):
        return self.graphs[graph_iri].serialize(format=rdf_format, encoding="utf-8")

    def graph_exists(self, graph_iri):
        return graph_iri in self.graphs

    def remove_report(self, graph_iri):
        self.graphs.pop(graph_iri, None)
        self.metadata.pop(graph_iri, None)


class FakeResponse:
    status_code = 200
    content = REPORT
    headers = {"Content-Type": "text/turtle; charset=utf-8"}

    @property
    def text(self):
        return self.content.decode("utf-8")


class ValidationReportTests(unittest.TestCase):
    def test_german_and_orig_profiles_are_excluded(self):
        document = {"parameters": [{"name": "shapeModel", "schema": {"enum": [
            "dcatap301level1", "dcatap301level2", "dcatap30de",
            "dcatap121orig", "dcatap11orig", "dcatap301"
        ]}}]}
        self.assertEqual(_extract_shape_models(document), ["dcatap301level1", "dcatap301level2", "dcatap301"])

    def test_profile_filter_is_case_insensitive_for_orig(self):
        document = {"parameters": [{"name": "shapeModel", "schema": {"enum": [
            "dcatap301ORIG", "dcatap301level1"
        ]}}]}
        self.assertEqual(_extract_shape_models(document), ["dcatap301level1"])

    def test_validator_parses_counts_and_preserves_rdf_payload(self):
        client = ShaclValidatorClient("http://localhost:8080/shacl/validation/report")
        client.session.post = Mock(return_value=FakeResponse())
        result = client.validate(REPORT, "dcatap301level1")

        self.assertFalse(result.conforms)
        self.assertEqual(result.results, 1)
        self.assertEqual(result.violations, 1)
        self.assertEqual(result.report_body, REPORT)
        self.assertTrue(result.report_parsed)
        self.assertEqual(result.report_format, "turtle")
        self.assertEqual(result.report_content_type, "text/turtle; charset=utf-8")
        self.assertFalse(hasattr(result, "validation_details"))

    def test_report_is_stored_as_named_graph_and_not_normalized_in_sqlite(self):
        client = ShaclValidatorClient("http://localhost:8080/shacl/validation/report")
        client.session.post = Mock(return_value=FakeResponse())
        result = client.validate(REPORT, "dcatap301level1")
        report_store = FakeReportStore()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "analysis.db", report_store=report_store)
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO datasets(dataset_id,catalogue_id,fetch_status,rdf_path) VALUES (?,?,?,?)",
                    ("test-dataset", "test-catalogue", "ok", "data/rdf/test-dataset.ttl"),
                )

            validation_id = db.save_validation_result(
                dataset_id="test-dataset", validator_url="http://localhost:8080/shacl/validation/report",
                shape_model=result.shape_model, conforms=result.conforms, results=result.results,
                violations=result.violations, warnings=result.warnings, infos=result.infos,
                http_status=result.http_status, error=result.error, execution_status="validated",
                report_body=result.report_body, report_content_type=result.report_content_type,
                report_parsed=result.report_parsed, report_format=result.report_format,
            )

            with db.get_connection() as conn:
                validation = conn.execute("SELECT * FROM validations").fetchone()
                validation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(validations)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(validation["id"], validation_id)
            self.assertEqual(user_version, 3)
            self.assertEqual(tables & {"datasets", "validations", "schema_metadata", "validation_results"}, {"datasets", "validations"})
            self.assertEqual(validation["report_status"], "stored")
            self.assertEqual(validation["report_format"], "turtle")
            self.assertTrue(validation["report_graph_iri"].startswith(f"urn:mqa:validation-report:{validation_id}:"))
            self.assertEqual(validation["report_triples"], len(report_store.graphs[validation["report_graph_iri"]]))
            self.assertEqual(report_store.metadata[validation["report_graph_iri"]]["shape_model"], "dcatap301level1")
            self.assertEqual(report_store.metadata[validation["report_graph_iri"]]["dataset_id"], "test-dataset")
            self.assertTrue(validation["report_sha256"])
            self.assertTrue(validation["report_saved_at"])
            self.assertIn("report_graph_iri", validation_columns)
            self.assertNotIn("report_body_gzip", validation_columns)
            self.assertNotIn("validation_results", tables)
            graph = report_store.get_report_graph(validation["report_graph_iri"])
            ex = Namespace("https://example.org/")
            self.assertIn((None, ex.customProperty, Literal("preserved")), graph)
            self.assertEqual(len(db.get_validation_report_rows()), 1)

    def test_legacy_schema_is_reset_for_a_clean_repopulation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE datasets (
                    dataset_id TEXT PRIMARY KEY, catalogue_id TEXT, source_url TEXT, rdf_path TEXT,
                    sha256 TEXT, fetch_status TEXT NOT NULL DEFAULT 'pending', fetch_http_status INTEGER,
                    fetch_error TEXT, position INTEGER, discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fetched_at TIMESTAMP, validation_status TEXT NOT NULL DEFAULT 'pending',
                    validation_error TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT, validator_url TEXT,
                    shape_model TEXT, report_body_gzip BLOB, UNIQUE(dataset_id,validator_url,shape_model)
                );
                CREATE TABLE validation_results(id INTEGER PRIMARY KEY, validation_id INTEGER);
                INSERT INTO datasets(dataset_id,catalogue_id,fetch_status,rdf_path,validation_status)
                    VALUES ('old-dataset','catalogue','ok','data/rdf/old.ttl','completed');
                INSERT INTO validations(dataset_id,validator_url,shape_model,report_body_gzip)
                    VALUES ('old-dataset','http://localhost:8080/shacl/validation/report','dcatap301level1',X'00');
                INSERT INTO validation_results VALUES (1,1);
            """)
            conn.commit()
            conn.close()

            db = Database(db_path)
            db.init_schema()
            with db.get_connection() as conn:
                dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(validations)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                validation_count = conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0]
            self.assertEqual(dataset_count, 0)
            self.assertEqual(validation_count, 0)
            self.assertIn("report_graph_iri", columns)
            self.assertIn("report_status", columns)
            self.assertNotIn("report_body_gzip", columns)
            self.assertNotIn("validation_results", tables)
            db.init_schema()  # idempotent
            with db.get_connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0], 0)

    def test_report_store_failure_marks_validation_as_error(self):
        class BrokenStore(FakeReportStore):
            def save_report(self, graph_iri, body, rdf_format):
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "analysis.db", report_store=BrokenStore())
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute("INSERT INTO datasets(dataset_id,fetch_status) VALUES ('ds','ok')")
            with self.assertRaises(RuntimeError):
                db.save_validation_result(
                    dataset_id="ds", validator_url="http://validator", shape_model="dcatap301",
                    conforms=True, results=0, violations=0, warnings=0, infos=0,
                    http_status=200, error=None, execution_status="validated",
                    report_body=REPORT, report_content_type="text/turtle",
                    report_parsed=True, report_format="turtle",
                )
            with db.get_connection() as conn:
                row = conn.execute("SELECT execution_status,report_status,error FROM validations").fetchone()
            self.assertEqual(row["execution_status"], "error")
            self.assertEqual(row["report_status"], "error")
            self.assertIn("disk full", row["error"])


    def test_revalidation_replaces_and_removes_the_previous_named_graph(self):
        report_store = FakeReportStore()
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "analysis.db", report_store=report_store)
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute("INSERT INTO datasets(dataset_id,fetch_status) VALUES ('ds','ok')")

            common = dict(
                dataset_id="ds", validator_url="http://validator", shape_model="dcatap301",
                conforms=False, results=1, violations=1, warnings=0, infos=0,
                http_status=200, error=None, execution_status="validated",
                report_content_type="text/turtle", report_parsed=True, report_format="turtle",
            )
            first_id = db.save_validation_result(report_body=REPORT, **common)
            with db.get_connection() as conn:
                first_graph = conn.execute(
                    "SELECT report_graph_iri FROM validations WHERE id=?", (first_id,)
                ).fetchone()[0]

            changed_report = REPORT.replace(b'"preserved"', b'"updated"')
            second_id = db.save_validation_result(report_body=changed_report, **common)
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, report_graph_iri, report_status FROM validations"
                ).fetchone()

            self.assertEqual(first_id, second_id)
            self.assertEqual(row["report_status"], "stored")
            self.assertNotEqual(row["report_graph_iri"], first_graph)
            self.assertNotIn(first_graph, report_store.graphs)
            self.assertNotIn(first_graph, report_store.metadata)
            self.assertIn(row["report_graph_iri"], report_store.graphs)
            self.assertIn(row["report_graph_iri"], report_store.metadata)

    def test_http_400_body_is_not_inserted_as_an_rdf_report(self):
        body = b"Could not initialize class io.piveau.validating.DCATAP200"

        class ErrorResponse:
            status_code = 400
            content = body
            headers = {"Content-Type": "text/plain; charset=utf-8"}

            @property
            def text(self):
                return self.content.decode("utf-8")

        client = ShaclValidatorClient("http://localhost:8080/shacl/validation/report")
        client.session.post = Mock(return_value=ErrorResponse())
        result = client.validate(b"@prefix ex: <https://example.org/> .", "dcatap200")
        self.assertIsNone(result.conforms)
        self.assertEqual(result.http_status, 400)
        self.assertEqual(result.report_body, body)
        self.assertFalse(result.report_parsed)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "analysis.db")
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute("INSERT INTO datasets(dataset_id,catalogue_id,fetch_status) VALUES (?,?,?)", ("ds", "test", "ok"))
            db.save_validation_result(
                dataset_id="ds", validator_url="http://localhost:8080/shacl/validation/report",
                shape_model="dcatap200", conforms=None, results=0, violations=0,
                warnings=0, infos=0, http_status=400, error=result.error,
                execution_status="error", report_body=result.report_body,
                report_content_type=result.report_content_type, report_parsed=False,
            )
            with db.get_connection() as conn:
                row = conn.execute("SELECT report_status,report_graph_iri FROM validations").fetchone()
            self.assertEqual(row["report_status"], "not_available")
            self.assertIsNone(row["report_graph_iri"])


if __name__ == "__main__":
    unittest.main()
