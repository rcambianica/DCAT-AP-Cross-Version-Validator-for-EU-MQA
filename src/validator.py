from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF
from urllib3.util.retry import Retry

SH = Namespace("http://www.w3.org/ns/shacl#")


@dataclass
class ValidationResult:
    shape_model: str
    conforms: bool | None
    results: int
    violations: int
    warnings: int
    infos: int
    http_status: int | None
    error: str | None = None
    report_body: bytes | None = None
    report_content_type: str | None = None
    report_parsed: bool = False
    validation_details: list[dict[str, Any]] = field(default_factory=list)


def _term_record(term: Any) -> dict[str, Any]:
    """Serialize an RDF term without discarding datatype/language information."""
    if isinstance(term, URIRef):
        term_type = "uri"
    elif isinstance(term, BNode):
        term_type = "bnode"
    elif isinstance(term, Literal):
        term_type = "literal"
    else:
        term_type = type(term).__name__
    return {
        "term_type": term_type,
        "value": str(term),
        "n3": term.n3(),
        "datatype": str(term.datatype) if isinstance(term, Literal) and term.datatype else None,
        "language": term.language if isinstance(term, Literal) else None,
    }


def _term_text(graph: Graph, subject: Any, predicate: URIRef) -> str | None:
    values = sorted(graph.objects(subject, predicate), key=lambda value: value.n3())
    return str(values[0]) if values else None


def _local_name(value: str | None) -> str | None:
    if not value:
        return None
    return value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _properties_for_subject(graph: Graph, subject: Any) -> dict[str, list[dict[str, Any]]]:
    properties: dict[str, list[dict[str, Any]]] = {}
    for predicate, obj in graph.predicate_objects(subject):
        properties.setdefault(str(predicate), []).append(_term_record(obj))
    for values in properties.values():
        values.sort(key=lambda item: (item["n3"], item.get("language") or "", item.get("datatype") or ""))
    return dict(sorted(properties.items()))


def _extract_report_data(graph: Graph) -> list[dict[str, Any]]:
    """Extract every sh:ValidationResult and its direct RDF properties.

    The original RDF body is retained as a compressed BLOB in SQLite. The JSON
    representation is a queryable index for properties not covered by columns.
    """
    result_nodes = set(graph.subjects(RDF.type, SH.ValidationResult))
    details: list[dict[str, Any]] = []
    for node in result_nodes:
        properties = _properties_for_subject(graph, node)
        focus = _term_text(graph, node, SH.focusNode)
        path = _term_text(graph, node, SH.resultPath)
        value = _term_text(graph, node, SH.value)
        source_shape = _term_text(graph, node, SH.sourceShape)
        component = _term_text(graph, node, SH.sourceConstraintComponent)
        severity_iri = _term_text(graph, node, SH.resultSeverity)
        messages = sorted(str(item) for item in graph.objects(node, SH.resultMessage))
        details.append({
            "result_node": str(node),
            "focus_node": focus,
            "result_path": path,
            "value": value,
            "source_shape": source_shape,
            "source_constraint_component": _local_name(component),
            "severity": _local_name(severity_iri),
            "message": " | ".join(messages) if messages else None,
            "properties_json": json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })

    # RDF graphs are unordered: sort results by semantic content for stable
    # persistence and output ordering across equivalent reports.
    stable_fields = (
        "focus_node", "result_path", "severity", "source_constraint_component",
        "value", "source_shape", "message", "properties_json",
    )
    details.sort(key=lambda item: tuple(item.get(field) or "" for field in stable_fields))
    return details


class ShaclValidatorClient:
    def __init__(
        self,
        endpoint: str,
        timeout: int = 30,
        request_delay: float = 0.0,
        retries: int = 0,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "mqa-cross-version (+research; sequential SHACL validation)"}
        )

        # Per il validator locale i retry sono disabilitati di default. Se l'utente
        # li abilita esplicitamente, valgono comunque solo per errori transitori.
        retry = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _parse_report(self, response: requests.Response) -> Graph:
        content_type = response.headers.get("Content-Type", "").lower()
        candidates: list[str] = []

        if "turtle" in content_type:
            candidates.append("turtle")
        elif "ld+json" in content_type or "json" in content_type:
            candidates.append("json-ld")
        elif "rdf+xml" in content_type or "xml" in content_type:
            candidates.append("xml")
        elif "n-triples" in content_type:
            candidates.append("nt")
        elif "trig" in content_type:
            candidates.append("trig")
        elif "n3" in content_type:
            candidates.append("n3")

        for fmt in ("turtle", "json-ld", "xml", "nt", "trig", "n3"):
            if fmt not in candidates:
                candidates.append(fmt)

        last_error: Exception | None = None
        for fmt in candidates:
            try:
                graph = Graph()
                graph.parse(data=response.content, format=fmt)
                return graph
            except Exception as exc:
                last_error = exc

        raise ValueError(f"Cannot parse SHACL report as RDF: {last_error}")

    def validate(self, rdf_data: bytes, shape_model: str) -> ValidationResult:
        if self.request_delay:
            time.sleep(self.request_delay)

        try:
            response = self.session.post(
                self.endpoint,
                params={"shapeModel": shape_model},
                data=rdf_data,
                headers={
                    "Content-Type": "text/turtle",
                    "Accept": "text/turtle, application/ld+json;q=0.9, application/rdf+xml;q=0.8",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            return ValidationResult(
                shape_model, None, 0, 0, 0, 0, None, f"Request error: {exc}"
            )

        if response.status_code != 200:
            body = response.text[:1000].replace("\n", " ")
            return ValidationResult(
                shape_model,
                None,
                0,
                0,
                0,
                0,
                response.status_code,
                f"HTTP {response.status_code}: {body}",
                report_body=response.content or None,
                report_content_type=response.headers.get("Content-Type"),
            )

        try:
            graph = self._parse_report(response)
        except Exception as exc:
            return ValidationResult(
                shape_model,
                None,
                0,
                0,
                0,
                0,
                response.status_code,
                f"Invalid SHACL report: {exc}",
                report_body=response.content or None,
                report_content_type=response.headers.get("Content-Type"),
            )

        conforms_values = list(graph.objects(None, SH.conforms))
        conforms = None
        if conforms_values:
            conforms = bool(conforms_values[0].toPython())

        result_nodes = set(graph.subjects(RDF.type, SH.ValidationResult))
        violations = warnings = infos = 0
        for node in result_nodes:
            severities = set(graph.objects(node, SH.resultSeverity))
            if SH.Violation in severities:
                violations += 1
            if SH.Warning in severities:
                warnings += 1
            if SH.Info in severities:
                infos += 1

        validation_details = _extract_report_data(graph)

        return ValidationResult(
            shape_model=shape_model,
            conforms=conforms,
            results=len(result_nodes),
            violations=violations,
            warnings=warnings,
            infos=infos,
            http_status=response.status_code,
            report_body=response.content or None,
            report_content_type=response.headers.get("Content-Type"),
            report_parsed=True,
            validation_details=validation_details,
        )
