import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.analysis import analyze_database, parse_shape_model


class AnalysisTests(unittest.TestCase):
    def test_parse_shape_model(self):
        self.assertEqual(parse_shape_model("dcatap300"), ("3.0.0", "base"))
        self.assertEqual(parse_shape_model("dcatap211level2"), ("2.1.1", "level2"))
        self.assertEqual(parse_shape_model("dcatap121level1"), ("1.2.1", "level1"))
        self.assertIsNone(parse_shape_model("dcatap300orig"))

    def test_analysis_outputs_five_charts_and_paired_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "test.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE datasets(dataset_id TEXT PRIMARY KEY, fetch_status TEXT);
                CREATE TABLE validations(
                    id INTEGER PRIMARY KEY, dataset_id TEXT, validator_url TEXT,
                    shape_model TEXT, conforms INTEGER, results INTEGER,
                    violations INTEGER, warnings INTEGER, infos INTEGER,
                    http_status INTEGER, error TEXT, execution_status TEXT,
                    report_body_gzip BLOB, report_format TEXT
                );
                CREATE TABLE validation_results(
                    id INTEGER PRIMARY KEY, validation_id INTEGER, result_node TEXT,
                    focus_node TEXT, result_path TEXT, value TEXT, source_shape TEXT,
                    source_constraint_component TEXT, severity TEXT, message TEXT,
                    properties_json TEXT
                );
                INSERT INTO datasets VALUES ('ds1','ok');
                INSERT INTO validations VALUES
                    (1,'ds1','http://validator','dcatap211level1',0,1,1,0,0,200,NULL,'validated',NULL,'turtle'),
                    (2,'ds1','http://validator','dcatap300level1',0,2,2,0,0,200,NULL,'validated',NULL,'turtle');
                INSERT INTO validation_results VALUES
                    (1,1,'r1','urn:focus','http://purl.org/dc/terms/title',NULL,'s1','MinCountConstraintComponent','Violation','missing title',
                     '{"http://www.w3.org/ns/shacl#resultPath":[{"term_type":"uri","value":"http://purl.org/dc/terms/title"}]}');
                INSERT INTO validation_results VALUES
                    (2,2,'r2','urn:focus','http://purl.org/dc/terms/title',NULL,'s1','MinCountConstraintComponent','Violation','missing title',
                     '{"http://www.w3.org/ns/shacl#resultPath":[{"term_type":"uri","value":"http://purl.org/dc/terms/title"}]}');
            """)
            conn.commit()
            conn.close()

            out = root / "out"
            summary = analyze_database(db, out)
            self.assertEqual(summary["successful_validation_rows"], 2)
            self.assertEqual(summary["paired_comparisons"], 1)
            self.assertEqual(summary["charts_generated"], 5)
            for name in (
                "validation_outcomes.png", "shacl_result_distributions.png",
                "paired_version_changes.png", "shacl_component_heatmap.png",
                "rdf_property_violations.png",
            ):
                self.assertTrue((out / name).is_file(), name)
            self.assertTrue((out / "paired_version_deltas.csv").is_file())
            self.assertTrue((out / "shacl_paths.csv").is_file())


if __name__ == "__main__":
    unittest.main()
