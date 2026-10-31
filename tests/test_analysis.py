import tempfile
import unittest
from pathlib import Path

from src.analysis import profile_version, analyze_database


class AnalysisTests(unittest.TestCase):
    def test_profile_version_only_base_profiles(self):
        self.assertEqual(profile_version("dcatap300"), ("3.0.0", "dcatap300"))
        self.assertEqual(profile_version("dcatap211"), ("2.1.1", "dcatap211"))
        self.assertIsNone(profile_version("dcatap300level1"))
        self.assertIsNone(profile_version("dcatap300orig"))

    def test_analysis_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "test.db"
            import sqlite3
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE datasets(dataset_id TEXT PRIMARY KEY, fetch_status TEXT);
                CREATE TABLE validations(id INTEGER PRIMARY KEY, dataset_id TEXT, shape_model TEXT,
                    conforms INTEGER, results INTEGER, violations INTEGER, warnings INTEGER, infos INTEGER,
                    http_status INTEGER, error TEXT, execution_status TEXT, validated_at TEXT);
                CREATE TABLE validation_results(id INTEGER PRIMARY KEY, validation_id INTEGER,
                    severity TEXT, source_shape TEXT, source_constraint_component TEXT,
                    result_path TEXT);
                INSERT INTO datasets VALUES ('ds1','ok');
                INSERT INTO validations VALUES (1,'ds1','dcatap300',0,1,1,0,0,200,NULL,'validated','2026-01-01');
                INSERT INTO validation_results VALUES (1,1,'Violation','ShapeA','MinCountConstraintComponent','title');
            """)
            conn.commit(); conn.close()
            out = root / "out"
            summary = analyze_database(db, out)
            self.assertEqual(summary["successful_validation_rows"], 1)
            self.assertTrue((out / "profile_summary.csv").exists())
            self.assertTrue((out / "shacl_issues.csv").exists())
            self.assertTrue((out / "profile_conformance.png").exists())


if __name__ == "__main__":
    unittest.main()
