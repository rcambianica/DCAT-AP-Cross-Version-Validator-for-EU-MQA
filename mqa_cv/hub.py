import logging
from typing import Iterator
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

logger = logging.getLogger(__name__)


class HubClient:
    """Client for the public Piveau Hub Search and Repo APIs."""

    def __init__(
        self,
        search_base_url: str = "https://data.europa.eu/api/hub/search",
        repo_base_url: str = "https://data.europa.eu/api/hub/repo",
        timeout: int = 60,
    ):
        self.search_base_url = search_base_url.rstrip("/")
        self.repo_base_url = repo_base_url.rstrip("/")
        self.timeout = timeout
        self.session = self._init_session()

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "MQA-Cross-Version-Validator/1.0",
            "Accept": "application/json",
        })
        return session

    def list_catalogues(self) -> list[str]:
        """Return catalogue identifiers exposed by Hub Search."""
        url = f"{self.search_base_url}/catalogues"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected /catalogues response: {type(data).__name__}")
        return [str(x) for x in data]

    def iter_dataset_ids(
        self,
        catalogue_id: str | None = None,
        page_size: int = 1000,
        limit: int | None = None,
    ) -> Iterator[str]:
        """Iterate dataset IDs using Hub Search's scroll API.

        The initial /search request creates a scroll context and returns the
        first batch plus a scrollId. Subsequent batches are fetched from
        /scroll until the API returns an empty page or no scrollId remains.
        ``limit`` is a local cap intended for test runs; by default all IDs
        exposed by the scroll context are yielded.
        """
        import json
        import time

        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")

        if catalogue_id:
            logger.info("Checking catalogue '%s' in Hub Search...", catalogue_id)
            catalogues = self.list_catalogues()
            if catalogue_id not in catalogues:
                raise ValueError(
                    f"Catalogue '{catalogue_id}' is not exposed by Hub Search. "
                    f"Available catalogue IDs include: {', '.join(catalogues[:20])}"
                    + (" ..." if len(catalogues) > 20 else "")
                )

        search_url = f"{self.search_base_url}/search"
        params = {
            "filters": "dataset",
            "facets": json.dumps({"catalog": [catalogue_id]}, separators=(",", ":")) if catalogue_id else None,
            "scroll": "true",
            "limit": page_size,
        }
        # Requests omits query parameters whose value is None.
        logger.info(
            "Search API: starting scroll (catalogue=%s, page_size=%d)",
            catalogue_id or "<all>", page_size,
        )
        response = self.session.get(
            search_url, params=params, timeout=self.timeout
        )
        response.raise_for_status()
        data = response.json()
        result = data.get("result") or {}
        scroll_id = result.get("scrollId") or data.get("scrollId")
        results = result.get("results") or []

        seen: set[str] = set()
        emitted = 0
        page = 1

        while True:
            logger.info(
                "Search API scroll: page=%d returned=%d%s",
                page, len(results), f" total_distinct={emitted}" if emitted else "",
            )
            if not results:
                logger.info("Search API scroll: no more results.")
                break

            new_ids = 0
            for item in results:
                if not isinstance(item, dict):
                    continue
                value = item.get("id") or item.get("idName")
                if not value:
                    continue
                dataset_id = str(value)
                if dataset_id in seen:
                    continue
                seen.add(dataset_id)
                emitted += 1
                new_ids += 1
                yield dataset_id
                if limit is not None and emitted >= limit:
                    logger.info("Search API scroll: reached local limit=%d", limit)
                    return

            if not scroll_id:
                logger.warning(
                    "Search API returned %d result(s) without a scrollId; "
                    "the scroll cannot continue. Distinct IDs collected: %d.",
                    len(results), emitted,
                )
                break

            # A short pause reduces pressure on the search service.
            time.sleep(0.1)
            scroll_url = f"{self.search_base_url}/scroll"
            scroll_response = self.session.get(
                scroll_url,
                params={"scrollId": scroll_id},
                timeout=self.timeout,
            )
            scroll_response.raise_for_status()
            scroll_data = scroll_response.json()
            scroll_result = scroll_data.get("result") or {}
            scroll_id = (
                scroll_result.get("scrollId")
                or scroll_data.get("scrollId")
                or scroll_id
            )
            results = scroll_result.get("results") or []
            page += 1

        logger.info("Search API scroll completed: distinct_ids=%d pages=%d", emitted, page)

    def fetch_dataset_rdf(self, dataset_id: str) -> tuple[str, str]:
        """Retrieve a dataset's RDF graph through the Repo API.

        The current Repo API resource is ``GET /datasets/{datasetId}``.
        Content negotiation asks for Turtle first; the response media type is
        returned so callers can diagnose unexpected formats.
        """
        url = f"{self.repo_base_url}/datasets/{dataset_id}"
        headers = {
            "Accept": "text/turtle, application/ld+json, application/rdf+xml;q=0.9, text/n3;q=0.8"
        }

        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        logger.info(
            "Repo API: dataset=%s HTTP=%d content-type=%s bytes=%d",
            dataset_id, response.status_code, content_type, len(response.content),
        )

        # Keep the existing return contract. The caller can parse the RDF
        # according to Content-Type if necessary; Turtle is requested first.
        return response.text, response.url
