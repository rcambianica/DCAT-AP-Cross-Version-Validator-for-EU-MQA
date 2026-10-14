import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mqa_cv.db import Database
from mqa_cv.profiles import _extract_shape_models
from mqa_cv.validator import ShaclValidatorClient


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


class FakeResponse:
    status_code = 200
    content = REPORT
    headers = {"Content-Type": "text/turtle; charset=utf-8"}

    @property
    def text(self):
        return self.content.decode("utf-8")


class ValidationReportTests(unittest.TestCase):
    def test_german_profiles_are_excluded(self):
        document = {"parameters": [{"name": "shapeModel", "schema": {"enum": [
            "dcatap301level1", "dcatap301level2", "dcatap30de", "dcatap301"
        ]}}]}
        profiles = _extract_shape_models(document)
        self.assertEqual(profiles, ["dcatap301level1", "dcatap301level2", "dcatap301"])

    def test_validator_extracts_individual_result_and_raw_body(self):
        client = ShaclValidatorClient("http://localhost:8080/shacl/validation/report")
        client.session.post = Mock(return_value=FakeResponse())
        result = client.validate(REPORT, "dcatap301level1")

        self.assertFalse(result.conforms)
        self.assertEqual(result.results, 1)
        self.assertEqual(result.report_body, REPORT)
        self.assertEqual(result.validation_details[0]["severity"], "Violation")
        self.assertEqual(result.validation_details[0]["source_constraint_component"], "MinCountConstraintComponent")
        properties = json.loads(result.validation_details[0]["properties_json"])
        self.assertIn("https://example.org/customProperty", properties)
        self.assertEqual(properties["https://example.org/customProperty"][0]["value"], "preserved")
        self.assertIn('"language":"en"', result.validation_details[0]["properties_json"])

    def test_raw_report_and_normalized_details_are_persisted(self):
        client = ShaclValidatorClient("http://localhost:8080/shacl/validation/report")
        client.session.post = Mock(return_value=FakeResponse())
        result = client.validate(REPORT, "dcatap301level1")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "analysis.db")
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO datasets(dataset_id,catalogue_id,fetch_status,rdf_path) VALUES (?,?,?,?)",
                    ("test-dataset", "test-catalogue", "ok", "data/rdf/test-dataset.ttl"),
                )
                conn.commit()

            db.save_validation_result(
                dataset_id="test-dataset", validator_url="http://localhost:8080/shacl/validation/report",
                shape_model=result.shape_model, conforms=result.conforms, results=result.results,
                violations=result.violations, warnings=result.warnings, infos=result.infos,
                http_status=result.http_status, error=result.error, execution_status="validated",
                report_body=result.report_body, report_content_type=result.report_content_type,
                report_parsed=result.report_metadata_json is not None,
                report_metadata_json=result.report_metadata_json,
                validation_details=result.validation_details,
            )

            with db.get_connection() as conn:
                validation = conn.execute("SELECT * FROM validations").fetchone()
                detail = conn.execute("SELECT * FROM validation_results").fetchone()
            self.assertEqual(validation["report_format"], "turtle")
            self.assertTrue(validation["report_body_gzip"])
            self.assertTrue(validation["report_saved_at"])
            self.assertEqual(detail["severity"], "Violation")
            self.assertEqual(json.loads(detail["properties_json"])["https://example.org/customProperty"][0]["value"], "preserved")
            self.assertEqual(gzip.decompress(validation["report_body_gzip"]), REPORT)
            raw_record = db.get_raw_report("test-dataset", "dcatap301level1")
            self.assertEqual(gzip.decompress(raw_record["report_body_gzip"]), REPORT)
            found = db.get_validation_details("test-dataset", "dcatap301level1", "Violation", 50)
            self.assertEqual(len(found), 1)

    def test_existing_database_is_migrated_and_old_results_are_queued_for_report_capture(self):
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT, validator_url TEXT, shape_model TEXT,
                    conforms INTEGER, results INTEGER, violations INTEGER, warnings INTEGER, infos INTEGER,
                    http_status INTEGER, error TEXT, execution_status TEXT NOT NULL DEFAULT 'validated',
                    validated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(dataset_id,validator_url,shape_model)
                );
                INSERT INTO datasets(dataset_id,catalogue_id,fetch_status,rdf_path,validation_status)
                    VALUES ('old-dataset','catalogue','ok','data/rdf/old.ttl','completed');
                INSERT INTO validations(dataset_id,validator_url,shape_model,conforms,results,violations,warnings,infos,http_status,execution_status)
                    VALUES ('old-dataset','http://localhost:8080/shacl/validation/report','dcatap301level1',0,1,1,0,0,200,'validated');
            """)
            conn.commit()
            conn.close()

            db = Database(db_path)
            db.init_schema()
            with db.get_connection() as conn:
                state = conn.execute("SELECT validation_status FROM datasets WHERE dataset_id='old-dataset'").fetchone()[0]
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(validations)")}
            self.assertEqual(state, "pending")
            self.assertIn("report_body_gzip", columns)
            self.assertIn("report_metadata_json", columns)

    def test_http_400_body_can_be_retained_for_diagnosis(self):
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

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "analysis.db")
            db.init_schema()
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO datasets(dataset_id,catalogue_id,fetch_status) VALUES (?,?,?)",
                    ("dcatap200-dataset", "test", "ok"),
                )
                conn.commit()
            db.save_validation_result(
                dataset_id="dcatap200-dataset",
                validator_url="http://localhost:8080/shacl/validation/report",
                shape_model="dcatap200", conforms=None, results=0, violations=0, warnings=0, infos=0,
                http_status=400, error=result.error, execution_status="error", report_body=result.report_body,
                report_content_type=result.report_content_type, report_parsed=False,
            )
            saved = db.get_raw_report("dcatap200-dataset", "dcatap200")
            self.assertEqual(saved["report_format"], "http-body")
            self.assertEqual(gzip.decompress(saved["report_body_gzip"]), body)


if __name__ == "__main__":
    unittest.main()
