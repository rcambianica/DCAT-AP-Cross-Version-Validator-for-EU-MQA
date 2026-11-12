"""Persistent storage for complete SHACL validation reports using PyOxigraph."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import tempfile


class ReportStoreError(RuntimeError):
    """Raised when a validation report cannot be persisted or retrieved."""


class ValidationReportStore:
    """Store each complete SHACL report in its own named RDF graph.

    The report body is parsed by Oxigraph itself. Blank nodes are renamed on
    ingestion because their labels are document-local and may otherwise collide
    when reports from different validations are loaded into the same store.
    PyOxigraph is imported lazily so discovery and RDF fetching do not need to
    open the report store.
    """

    CATALOG_GRAPH_IRI = "urn:mqa:validation-catalog"
    MQA = "urn:mqa:vocab:"

    _FORMAT_NAMES = {
        "turtle": "TURTLE",
        "ttl": "TURTLE",
        "json-ld": "JSON_LD",
        "jsonld": "JSON_LD",
        "xml": "RDF_XML",
        "rdf-xml": "RDF_XML",
        "rdfxml": "RDF_XML",
        "nt": "N_TRIPLES",
        "n-triples": "N_TRIPLES",
        "ntriples": "N_TRIPLES",
        "n3": "N3",
    }

    def __init__(self, path: Path | str, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pyoxigraph
        except ImportError as exc:  # pragma: no cover - exercised in installation checks
            raise RuntimeError(
                "PyOxigraph non è installato. Eseguire: pip install 'pyoxigraph>=0.5,<0.6'"
            ) from exc

        self._ox = pyoxigraph
        try:
            if read_only:
                self.store = pyoxigraph.Store.read_only(str(self.path))
            else:
                self.store = pyoxigraph.Store(str(self.path))
        except Exception as exc:
            raise ReportStoreError(f"Impossibile aprire il triplestore {self.path}: {exc}") from exc

    def _rdf_format(self, name: str | None):
        normalized = (name or "turtle").strip().lower()
        enum_name = self._FORMAT_NAMES.get(normalized)
        if enum_name is None:
            raise ReportStoreError(
                f"Formato RDF non supportato per un validation report: {name!r}. "
                "Sono supportati Turtle, JSON-LD, RDF/XML, N-Triples e N3."
            )
        try:
            return getattr(self._ox.RdfFormat, enum_name)
        except AttributeError as exc:
            raise ReportStoreError(f"Formato RDF PyOxigraph non disponibile: {enum_name}") from exc

    def save_report(self, graph_iri: str, body: bytes, rdf_format: str | None) -> int:
        """Persist a complete report in one named graph and return its triple count.

        The caller builds a content-addressed graph IRI from the validation ID
        and SHA-256 of the original HTTP response. Repeated writes are therefore
        idempotent. Parsing is transactional before the graph is committed.
        """
        if self.read_only:
            raise ReportStoreError("Il triplestore è aperto in sola lettura")
        if not body:
            raise ReportStoreError("Il report RDF è vuoto")

        graph = self._ox.NamedNode(graph_iri)
        try:
            if not self.store.contains_named_graph(graph):
                parsed_quads = self._ox.parse(
                    input=body,
                    format=self._rdf_format(rdf_format),
                    without_named_graphs=True,
                    rename_blank_nodes=True,
                )
                quads = (
                    self._ox.Quad(quad.subject, quad.predicate, quad.object, graph)
                    for quad in parsed_quads
                )
                # Store.extend is transactional: a parse or write failure does
                # not leave a partially populated named graph. A generator also
                # avoids making a second Python-side copy of each report graph.
                self.store.extend(quads)
            self.store.flush()
            count = sum(1 for _ in self.store.quads_for_pattern(None, None, None, graph))
            if count == 0:
                raise ReportStoreError(f"Il grafo {graph_iri} è presente ma vuoto")
            return count
        except ReportStoreError:
            raise
        except Exception as exc:
            raise ReportStoreError(
                f"Errore durante il salvataggio del report {graph_iri}: {exc}"
            ) from exc

    def save_report_metadata(
        self,
        graph_iri: str,
        *,
        validation_id: int,
        dataset_id: str,
        validator_url: str,
        shape_model: str,
        conforms: bool | None,
        results: int,
        violations: int,
        warnings: int,
        infos: int,
        report_sha256: str,
        report_format: str | None,
        report_content_type: str | None,
        report_triples: int,
    ) -> None:
        """Index a report graph in the dedicated SPARQL-queryable catalog graph."""
        if self.read_only:
            raise ReportStoreError("Il triplestore è aperto in sola lettura")
        if not self.graph_exists(graph_iri):
            raise ReportStoreError(f"Impossibile indicizzare un report inesistente: {graph_iri}")

        ox = self._ox
        subject = ox.NamedNode(graph_iri)
        catalog = ox.NamedNode(self.CATALOG_GRAPH_IRI)
        mqa = self.MQA
        triples = [
            ox.Quad(subject, ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), ox.NamedNode(mqa + "ValidationReport"), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "validationId"), ox.Literal(str(validation_id)), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "datasetId"), ox.Literal(dataset_id), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "validatorUrl"), ox.Literal(validator_url), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "shapeModel"), ox.Literal(shape_model), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "results"), ox.Literal(results), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "violations"), ox.Literal(violations), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "warnings"), ox.Literal(warnings), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "infos"), ox.Literal(infos), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "reportSha256"), ox.Literal(report_sha256), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "reportTriples"), ox.Literal(report_triples), catalog),
            ox.Quad(subject, ox.NamedNode(mqa + "storedAt"), ox.Literal(datetime.now(timezone.utc).isoformat(), datatype=ox.NamedNode("http://www.w3.org/2001/XMLSchema#dateTime")), catalog),
        ]
        if conforms is not None:
            triples.append(ox.Quad(subject, ox.NamedNode(mqa + "conforms"), ox.Literal(conforms), catalog))
        if report_format:
            triples.append(ox.Quad(subject, ox.NamedNode(mqa + "reportFormat"), ox.Literal(report_format), catalog))
        if report_content_type:
            triples.append(ox.Quad(subject, ox.NamedNode(mqa + "reportContentType"), ox.Literal(report_content_type), catalog))

        try:
            # Replace metadata for this graph (idempotent on retries).
            self.store.update(
                f"DELETE WHERE {{ GRAPH <{self.CATALOG_GRAPH_IRI}> {{ <{graph_iri}> ?p ?o }} }}"
            )
            self.store.extend(triples)
            self.store.flush()
        except Exception as exc:
            raise ReportStoreError(f"Impossibile indicizzare i metadati del report {graph_iri}: {exc}") from exc

    def graph_exists(self, graph_iri: str) -> bool:
        try:
            return self.store.contains_named_graph(self._ox.NamedNode(graph_iri))
        except Exception as exc:
            raise ReportStoreError(f"Impossibile verificare il grafo {graph_iri}: {exc}") from exc

    def remove_report(self, graph_iri: str) -> None:
        """Remove a report graph that is no longer referenced by SQLite."""
        if self.read_only:
            raise ReportStoreError("Il triplestore è aperto in sola lettura")
        try:
            graph = self._ox.NamedNode(graph_iri)
            self.store.update(
                f"DELETE WHERE {{ GRAPH <{self.CATALOG_GRAPH_IRI}> {{ <{graph_iri}> ?p ?o }} }}"
            )
            self.store.remove_graph(graph)
            self.store.flush()
        except Exception as exc:
            raise ReportStoreError(f"Impossibile rimuovere il grafo {graph_iri}: {exc}") from exc

    def dump_report(self, graph_iri: str, rdf_format: str | None = "turtle") -> bytes:
        """Serialize a stored graph (not a byte-for-byte copy of the HTTP body)."""
        try:
            if not self.graph_exists(graph_iri):
                raise ReportStoreError(f"Report non trovato nel triplestore: {graph_iri}")
            return self.store.dump(
                format=self._rdf_format(rdf_format),
                from_graph=self._ox.NamedNode(graph_iri),
            )
        except ReportStoreError:
            raise
        except Exception as exc:
            raise ReportStoreError(
                f"Impossibile serializzare il report {graph_iri}: {exc}"
            ) from exc

    @staticmethod
    def _sparql_string_literal(value: str) -> str:
        """Return a safely escaped SPARQL string literal."""
        # JSON string escaping is compatible with SPARQL string literals for
        # quotes, backslashes, control characters and Unicode escape sequences.
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _filename_token(value: str, *, fallback: str = "unknown") -> str:
        """Convert metadata into a filesystem-safe, bounded filename token."""
        token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-")
        return (token[:80] or fallback)

    def list_reports(
        self,
        *,
        dataset_id: str | None = None,
        shape_model: str | None = None,
    ) -> list[dict[str, str | None]]:
        """List report graph identifiers and catalog metadata.

        Selection is performed against the Oxigraph report catalog, which is
        the authoritative index for named graphs. Omitting both filters returns
        every report registered in the catalog.
        """
        if dataset_id is not None and not dataset_id.strip():
            raise ReportStoreError("dataset_id non può essere vuoto")
        if shape_model is not None and not shape_model.strip():
            raise ReportStoreError("shape_model non può essere vuoto")

        filters = []
        if dataset_id is not None:
            filters.append(
                f"FILTER(?datasetId = {self._sparql_string_literal(dataset_id)})"
            )
        if shape_model is not None:
            filters.append(
                f"FILTER(?shapeModel = {self._sparql_string_literal(shape_model)})"
            )
        filter_block = "\n            ".join(filters)

        query = f"""
            PREFIX mqa: <{self.MQA}>
            SELECT ?reportGraph ?validationId ?datasetId ?shapeModel ?reportSha256
            WHERE {{
                GRAPH <{self.CATALOG_GRAPH_IRI}> {{
                    ?reportGraph a mqa:ValidationReport ;
                                 mqa:datasetId ?datasetId ;
                                 mqa:shapeModel ?shapeModel .
                    OPTIONAL {{ ?reportGraph mqa:validationId ?validationId }}
                    OPTIONAL {{ ?reportGraph mqa:reportSha256 ?reportSha256 }}
                    {filter_block}
                }}
            }}
            ORDER BY ?shapeModel ?datasetId ?validationId ?reportGraph
        """
        try:
            rows = self.query(query)
            reports = []
            for row in rows:
                def value(name: str) -> str | None:
                    term = row[name]
                    return getattr(term, "value", None) if term is not None else None

                reports.append({
                    "report_graph_iri": value("reportGraph"),
                    "validation_id": value("validationId"),
                    "dataset_id": value("datasetId"),
                    "shape_model": value("shapeModel"),
                    "report_sha256": value("reportSha256"),
                })
            return reports
        except ReportStoreError:
            raise
        except Exception as exc:
            raise ReportStoreError(
                f"Impossibile elencare i validation report dal catalogo: {exc}"
            ) from exc

    def export_report_turtle(
        self,
        graph_iri: str,
        output_path: Path | str,
    ) -> Path:
        """Export one named report graph as a Turtle file."""
        destination = Path(output_path)
        try:
            body = self.dump_report(graph_iri, "turtle")
            if not body:
                raise ReportStoreError(f"Il report {graph_iri} non contiene triple")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Create the temporary file in the destination directory so that
            # os.replace remains atomic (same filesystem) and concurrent
            # exports do not share a fixed ``.tmp`` filename.
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                    delete=False,
                ) as temporary:
                    temporary.write(body)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, destination)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return destination
        except ReportStoreError:
            raise
        except Exception as exc:
            raise ReportStoreError(
                f"Impossibile esportare il report {graph_iri} in {destination}: {exc}"
            ) from exc

    def export_reports_turtle(
        self,
        output_dir: Path | str,
        *,
        dataset_id: str | None = None,
        shape_model: str | None = None,
    ) -> list[Path]:
        """Export selected report graphs as individual Turtle files.

        The two optional filters cover all supported selection modes:
        - no filters: every report in the Oxigraph catalog;
        - shape_model: all datasets validated with that shape model;
        - dataset_id: all shape models available for that dataset;
        - both filters: the report for the dataset/shape-model pair.
        """
        destination = Path(output_dir)
        try:
            destination.mkdir(parents=True, exist_ok=True)
            reports = self.list_reports(
                dataset_id=dataset_id, shape_model=shape_model
            )
            exported = []
            for report in reports:
                graph_iri = report["report_graph_iri"]
                if not graph_iri:
                    raise ReportStoreError(
                        "Il catalogo contiene un validation report senza IRI del grafo"
                    )

                validation_id = self._filename_token(
                    report["validation_id"] or "no-validation-id"
                )
                dataset_token = self._filename_token(report["dataset_id"] or "unknown-dataset")
                shape_token = self._filename_token(report["shape_model"] or "unknown-shape")
                digest = report["report_sha256"] or hashlib.sha256(
                    graph_iri.encode("utf-8")
                ).hexdigest()
                filename = (
                    f"validation-{validation_id}__dataset-{dataset_token}"
                    f"__shape-{shape_token}__{digest[:12]}.ttl"
                )
                exported.append(
                    self.export_report_turtle(graph_iri, destination / filename)
                )
            return exported
        except ReportStoreError:
            raise
        except Exception as exc:
            raise ReportStoreError(
                f"Esportazione dei validation report non riuscita: {exc}"
            ) from exc

    def get_report_graph(self, graph_iri: str):
        """Return an RDFLib Graph view of a stored report for analysis tools."""
        from rdflib import Graph

        body = self.dump_report(graph_iri, "turtle")
        graph = Graph()
        try:
            graph.parse(data=body, format="turtle")
        except Exception as exc:
            raise ReportStoreError(
                f"Il report {graph_iri} non è rileggibile come Turtle: {exc}"
            ) from exc
        return graph

    def query(self, sparql: str):
        """Execute a SPARQL query against the complete RDF dataset."""
        try:
            return self.store.query(sparql)
        except Exception as exc:
            raise ReportStoreError(f"Query SPARQL non riuscita: {exc}") from exc

    def flush(self) -> None:
        if not self.read_only:
            self.store.flush()

    def optimize(self) -> None:
        if not self.read_only:
            self.store.optimize()
