"""Optional integration tests against the real PyOxigraph engine."""
import tempfile
import unittest
from pathlib import Path

from src.report_store import ValidationReportStore


def _pyoxigraph_importable() -> bool:
    try:
        import pyoxigraph  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


@unittest.skipUnless(_pyoxigraph_importable(), "pyoxigraph non installato o incompatibile con il runtime Python")
class PyOxigraphIntegrationTests(unittest.TestCase):
    REPORT = b'''@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .

ex:report a sh:ValidationReport ;
    sh:conforms false .
_:result a sh:ValidationResult ;
    sh:resultSeverity sh:Violation ;
    sh:focusNode ex:dataset ;
    sh:resultPath ex:title ;
    sh:sourceConstraintComponent sh:MinCountConstraintComponent .
'''

    def test_store_named_graphs_and_isolate_blank_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ValidationReportStore(Path(tmp) / "oxigraph")
            graph_a = "urn:test:report:a"
            graph_b = "urn:test:report:b"

            count_a = store.save_report(graph_a, self.REPORT, "turtle")
            count_b = store.save_report(graph_b, self.REPORT, "turtle")

            self.assertEqual(count_a, count_b)
            self.assertTrue(store.graph_exists(graph_a))
            self.assertTrue(store.graph_exists(graph_b))

            store.save_report_metadata(
                graph_a,
                validation_id=1,
                dataset_id="dataset-a",
                validator_url="https://example.org/validator",
                shape_model="dcatap301level1",
                conforms=False,
                results=1,
                violations=1,
                warnings=0,
                infos=0,
                report_sha256="abc123",
                report_format="turtle",
                report_content_type="text/turtle",
                report_triples=count_a,
            )
            catalog_rows = list(store.query(
                "PREFIX mqa: <urn:mqa:vocab:> "
                "SELECT ?model WHERE { GRAPH <urn:mqa:validation-catalog> { "
                f"<{graph_a}> mqa:shapeModel ?model }}"
            ))
            self.assertEqual(str(catalog_rows[0]["model"]), "dcatap301level1")

            query_a = list(store.query(
                f"SELECT ?result WHERE {{ GRAPH <{graph_a}> {{ "
                "?result a <http://www.w3.org/ns/shacl#ValidationResult> } }"
            ))
            query_b = list(store.query(
                f"SELECT ?result WHERE {{ GRAPH <{graph_b}> {{ "
                "?result a <http://www.w3.org/ns/shacl#ValidationResult> } }"
            ))
            self.assertEqual(len(query_a), 1)
            self.assertEqual(len(query_b), 1)
            self.assertNotEqual(str(query_a[0]["result"]), str(query_b[0]["result"]))

            restored = store.get_report_graph(graph_a)
            self.assertEqual(len(restored), count_a)
            self.assertIn(b"ValidationReport", store.dump_report(graph_a, "turtle"))

            store.remove_report(graph_a)
            self.assertFalse(store.graph_exists(graph_a))
            self.assertTrue(store.graph_exists(graph_b))


if __name__ == "__main__":
    unittest.main()
