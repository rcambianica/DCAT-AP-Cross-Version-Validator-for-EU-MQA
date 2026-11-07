"""Persistent storage for complete SHACL validation reports using PyOxigraph."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone


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
