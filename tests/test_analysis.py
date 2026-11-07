import sqlite3
import tempfile
import unittest
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from src.analysis import analyze_database, parse_shape_model
from src.validator import SH


class FakeReportStore:
    def __init__(self, graphs):
        self.graphs = graphs

    def get_report_graph(self, graph_iri):
        return self.graphs[graph_iri]


def make_report(path: str) -> Graph:
    graph = Graph()
    ex = Namespace("https://example.org/")
    result = BNode()
    graph.add((ex.report, RDF.type, SH.ValidationReport))
    graph.add((ex.report, SH.conforms, Literal(False)))
    graph.add((result, RDF.type, SH.ValidationResult))
    graph.add((result, SH.resultSeverity, SH.Violation))
    graph.add((result, SH.resultPath, URIRef(path)))
    graph.add((result, SH.sourceConstraintComponent, SH.MinCountConstraintComponent))
    graph.add((result, SH.focusNode, ex.dataset))
    graph.add((result, SH.resultMessage, Literal("missing title")))
    return graph


class AnalysisTests(unittest.TestCase):
    def test_parse_shape_model(self):
        self.assertEqual(parse_shape_model("dcatap300"), ("3.0.0", "base"))
        self.assertEqual(parse_shape_model("dcatap211level2"), ("2.1.1", "level2"))
        self.assertEqual(parse_shape_model("dcatap121level1"), ("1.2.1", "level1"))
        self.assertIsNone(parse_shape_model("dcatap300orig"))

    def test_analysis_reads_named_report_graphs_and_outputs_five_charts(self):
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
                    report_graph_iri TEXT, report_status TEXT, report_format TEXT
                );
                INSERT INTO datasets VALUES ('ds1','ok');
                INSERT INTO validations VALUES
                    (1,'ds1','http://validator','dcatap211level1',0,1,1,0,0,200,NULL,'validated','urn:report:1','stored','turtle'),
                    (2,'ds1','http://validator','dcatap300level1',0,1,1,0,0,200,NULL,'validated','urn:report:2','stored','turtle');
            """)
            conn.commit()
            conn.close()

            report_store = FakeReportStore({
                "urn:report:1": make_report("http://purl.org/dc/terms/title"),
                "urn:report:2": make_report("http://purl.org/dc/terms/title"),
            })
            out = root / "out"
            summary = analyze_database(db, out, report_store=report_store)
            self.assertEqual(summary["successful_validation_rows"], 2)
            self.assertEqual(summary["paired_comparisons"], 1)
            self.assertEqual(summary["shacl_result_rows"], 2)
            self.assertEqual(summary["charts_generated"], 5)
            self.assertEqual(summary["path_categories"], {"simple_iri": 2})
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
