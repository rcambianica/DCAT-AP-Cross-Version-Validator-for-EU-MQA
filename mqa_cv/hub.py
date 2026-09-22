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
        page_size: int = 100,
        limit: int | None = None,
    ) -> Iterator[str]:
        """Retrieve dataset IDs through the POST /search API.

        The catalogue is applied through the documented ``facets.catalog``
        filter. Pagination uses the documented ``page`` parameter; the older
        ``from``/``size`` query parameters are deliberately not used.
        """
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

        emitted = 0
        page = 0
        url = f"{self.search_base_url}/search"
        seen: set[str] = set()

        while True:
            remaining = None if limit is None else limit - emitted
            if remaining is not None and remaining <= 0:
                return

            current_limit = page_size if remaining is None else min(page_size, remaining)
            payload = {
                "q": "",
                "filters": ["dataset"],
                "page": page,
                "limit": current_limit,
                "aggregation": False,
                "includes": ["id"],
                "scroll": False,
                "searchAfter": False,
            }
            if catalogue_id:
                payload["facets"] = {"catalog": [catalogue_id]}

            logger.info(
                "Search API: catalogue=%s page=%d limit=%d",
                catalogue_id or "<all>", page, current_limit,
            )

            response = self.session.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            result = data.get("result") or {}
            results = result.get("results") or []
            total = result.get("count")
            logger.info(
                "Search API: page=%d returned=%d total=%s",
                page, len(results), total if total is not None else "?",
            )

            if not results:
                return

            new_ids = 0
            for item in results:
                if not isinstance(item, dict):
                    continue
                dataset_id = item.get("id") or item.get("idName")
                if not dataset_id:
                    continue
                dataset_id = str(dataset_id)
                if dataset_id in seen:
                    continue
                seen.add(dataset_id)
                yield dataset_id
                emitted += 1
                new_ids += 1
                if limit is not None and emitted >= limit:
                    return

            # If the API returned no new IDs, advancing pages is unlikely to
            # make progress and could create an infinite loop.
            if new_ids == 0:
                logger.warning("Search API returned no new dataset IDs; stopping pagination.")
                return

            # The API may return its default page size even when a smaller
            # requested limit is supplied. A page shorter than the requested
            # page size is still a reliable end-of-results signal.
            if total is not None and emitted >= int(total):
                return
            if len(results) < current_limit:
                return

            page += 1

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
