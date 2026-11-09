import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cli import main
from src.db import Database


VALIDATOR_URL = "http://localhost:8080/shacl/validation/report"
PROFILES = ["dcatap301level1", "dcatap301level2"]
REPORT = b'''@prefix sh: <http://www.w3.org/ns/shacl#> .
[] a sh:ValidationReport ; sh:conforms false .'''


class FakeReportStore:
    read_only = False

    def __init__(self):
        self.graphs = {}
        self.metadata = {}

    def save_report_metadata(self, graph_iri, **metadata):
        self.metadata[graph_iri] = metadata

    def save_report(self, graph_iri, body, rdf_format):
        self.graphs[graph_iri] = body
        return 1


class RunModeTests(unittest.TestCase):
    def _database_with_skipped_profile(self, root: Path) -> Database:
        db = Database(root / "analysis.db", report_store=FakeReportStore())
        db.init_schema()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO datasets(dataset_id,catalogue_id,fetch_status,rdf_path,validation_status) "
                "VALUES (?,?,?,?,?)",
                ("dataset-1", "catalogue", "ok", str(root / "dataset.ttl"), "completed"),
            )
            conn.commit()

        db.save_validation_result(
            dataset_id="dataset-1", validator_url=VALIDATOR_URL,
            shape_model=PROFILES[0], conforms=False, results=1, violations=1,
            warnings=0, infos=0, http_status=200, error=None,
            execution_status="validated", report_body=REPORT,
            report_content_type="text/turtle", report_parsed=True, report_format="turtle",
        )
        db.save_validation_result(
            dataset_id="dataset-1", validator_url=VALIDATOR_URL,
            shape_model=PROFILES[1], conforms=None, results=0, violations=0,
            warnings=0, infos=0, http_status=None,
            error="SKIPPED: prerequisite profile dcatap301level1 returned conforms=false",
            execution_status="skipped",
        )
        return db

    def test_full_validation_requeues_dataset_with_old_skipped_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._database_with_skipped_profile(Path(temp_dir))
            self.assertEqual(
                db.get_pending_validation(VALIDATOR_URL, PROFILES, allow_skipped=True),
                [],
            )
            pending = db.get_pending_validation(
                VALIDATOR_URL, PROFILES, allow_skipped=False
            )
            self.assertEqual([row["dataset_id"] for row in pending], ["dataset-1"])

    def test_skipped_profiles_only_count_as_complete_when_allowed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = self._database_with_skipped_profile(Path(temp_dir))
            self.assertTrue(
                db.mark_completed_if_validated(
                    "dataset-1", VALIDATOR_URL, PROFILES, allow_skipped=True
                )
            )
            self.assertFalse(
                db.mark_completed_if_validated(
                    "dataset-1", VALIDATOR_URL, PROFILES, allow_skipped=False
                )
            )

    def test_cli_short_circuit_option_is_opt_in(self):
        with patch("src.cli.run_validation") as run_mock, patch(
            "sys.argv", ["dcat-ap-cv", "validation", "run"]
        ):
            main()
        run_mock.assert_called_once_with(limit=None, short_circuit=False)

        with patch("src.cli.run_validation") as run_mock, patch(
            "sys.argv", ["dcat-ap-cv", "validation", "run", "--short-circuit"]
        ):
            main()
        run_mock.assert_called_once_with(limit=None, short_circuit=True)


if __name__ == "__main__":
    unittest.main()
