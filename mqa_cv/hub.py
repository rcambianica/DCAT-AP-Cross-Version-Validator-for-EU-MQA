from __future__ import annotations

import json
import logging
from typing import Iterator
from urllib.parse import quote

import requests
from rdflib import Graph
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class HubClient:
    """Client for the Piveau Hub Search and Repo APIs.

    Search and retrieval are deliberately separate:
      1. Search API finds dataset IDs using the Hub's indexed search service.
      2. Repo API retrieves the authoritative RDF graph for each ID.

    We do not access Elasticsearch directly; the Search API is the public
    abstraction over the index.
    """

    def __init__(
        self,
        search_base_url: str,
        repo_base_url: str,
        timeout: int = 60,
        search_retries: int = 2,
        fetch_retries: int = 2,
    ):
        self.search_base_url = search_base_url.rstrip("/")
        self.repo_base_url = repo_base_url.rstrip("/")
        self.timeout = timeout
        self.search_session = self._init_session(search_retries)
        self.repo_session = self._init_session(fetch_retries)

    @staticmethod
    def _init_session(retries: int) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "MQA-Cross-Version-Validator/0.4 (+research; Piveau Hub client)",
        })
        return session

    @staticmethod
    def _extract_results(data: dict) -> list[dict]:
        result = data.get("result", {})
        if isinstance(result, dict):
            results = result.get("results", [])
        else:
            results = []
        return results if isinstance(results, list) else []

    @staticmethod
    def _extract_dataset_id(item: dict) -> str | None:
        # `id` is the preferred normalized Hub identifier. The fallbacks make
        # the client tolerant of older Search API response formats.
        for key in ("id", "idName", "identifier"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def iter_dataset_ids(
        self,
        catalogue_id: str,
        page_size: int = 100,
        limit: int | None = None,
    ) -> Iterator[str]:
        """Yield dataset IDs from the current Piveau Hub Search API."""
        if page_size < 1:
            raise ValueError("page_size must be >= 1")

        url = f"{self.search_base_url}/search"
        page = 0
        emitted = 0
        seen: set[str] = set()

        while True:
            remaining = None if limit is None else limit - emitted
            if remaining is not None and remaining <= 0:
                return

            current_limit = page_size if remaining is None else min(page_size, remaining)
            payload = {
                "q": "",
                "filters": ["dataset"],
                "facets": {"catalog": [catalogue_id]},
                "page": page,
                "limit": current_limit,
                "includes": ["id"],
                "aggregation": False,
                "scroll": False,
                "searchAfter": False,
            }

            logger.info(
                "Search API: catalogue=%s page=%d limit=%d",
                catalogue_id,
                page,
                current_limit,
            )
            response = self.search_session.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            results = self._extract_results(data)

            if not results:
                return

            emitted_this_page = 0
            for item in results:
                dataset_id = self._extract_dataset_id(item)
                if not dataset_id or dataset_id in seen:
                    continue
                seen.add(dataset_id)
                yield dataset_id
                emitted += 1
                emitted_this_page += 1
                if limit is not None and emitted >= limit:
                    return

            if len(results) < current_limit:
                return

            # A page that contains only duplicates should not cause an
            # accidental infinite loop.
            if emitted_this_page == 0 and len(results) > 0:
                logger.warning("Search page %d contained no new dataset IDs", page)

            page += 1

    def fetch_dataset_rdf(self, dataset_id: str) -> tuple[bytes, str, str]:
        """Fetch a dataset RDF graph from Hub Repo and normalize it to Turtle.

        Returns:
            (turtle_bytes, source_url, source_content_type)
        """
        encoded_id = quote(dataset_id, safe="~-._")
        url = f"{self.repo_base_url}/datasets/{encoded_id}"
        response = self.repo_session.get(
            url,
            headers={
                "Accept": "text/turtle, application/ld+json;q=0.9, application/rdf+xml;q=0.8, text/n3;q=0.7",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        graph = Graph()
        parse_candidates: list[str] = []

        if "turtle" in content_type:
            parse_candidates.append("turtle")
        elif "rdf+xml" in content_type or "xml" in content_type:
            parse_candidates.append("xml")
        elif "ld+json" in content_type or "json" in content_type:
            parse_candidates.append("json-ld")
        elif "n3" in content_type:
            parse_candidates.append("n3")

        for fmt in ("turtle", "xml", "json-ld", "n3", "nt"):
            if fmt not in parse_candidates:
                parse_candidates.append(fmt)

        last_error: Exception | None = None
        for fmt in parse_candidates:
            try:
                graph.parse(data=response.content, format=fmt)
                break
            except Exception as exc:
                last_error = exc
        else:
            raise ValueError(
                f"Unable to parse RDF returned by Repo API for {dataset_id}: {last_error}"
            )

        turtle = graph.serialize(format="turtle")
        if isinstance(turtle, str):
            turtle_bytes = turtle.encode("utf-8")
        else:
            turtle_bytes = turtle

        return turtle_bytes, url, content_type
