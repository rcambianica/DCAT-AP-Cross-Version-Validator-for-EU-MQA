from __future__ import annotations

import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from rdflib import Graph, Namespace
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


class ShaclValidatorClient:
    def __init__(
        self,
        endpoint: str,
        timeout: int = 120,
        request_delay: float = 0.0,
        retries: int = 3,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "mqa-cross-version/0.2 (+research; data.europa.eu SHACL API)",
            }
        )

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
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

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

        # Il servizio pubblico puo' cambiare serializzazione di default. Anche se
        # chiediamo Turtle, proviamo formati RDF comuni come fallback.
        for fmt in ("turtle", "json-ld", "xml", "nt", "trig", "n3"):
            if fmt not in candidates:
                candidates.append(fmt)

        last_error: Exception | None = None
        for fmt in candidates:
            try:
                graph = Graph()
                graph.parse(data=response.content, format=fmt)
                return graph
            except Exception as exc:  # pragma: no cover - fallback parser chain
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
            return ValidationResult(shape_model, None, 0, 0, 0, 0, None, str(exc))

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
            )

        conforms_values = list(graph.objects(None, SH.conforms))
        conforms = None
        if conforms_values:
            conforms = bool(conforms_values[0].toPython())

        result_nodes = set(graph.subjects(RDF.type, SH.ValidationResult))
        violations = 0
        warnings = 0
        infos = 0

        for node in result_nodes:
            severities = set(graph.objects(node, SH.resultSeverity))
            if SH.Violation in severities:
                violations += 1
            if SH.Warning in severities:
                warnings += 1
            if SH.Info in severities:
                infos += 1

        return ValidationResult(
            shape_model=shape_model,
            conforms=conforms,
            results=len(result_nodes),
            violations=violations,
            warnings=warnings,
            infos=infos,
            http_status=response.status_code,
        )
