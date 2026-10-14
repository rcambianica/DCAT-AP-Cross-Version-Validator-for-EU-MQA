import logging
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
            "User-Agent": "MQA-Cross-Version-Validator/0.4.2",
            "Accept": "application/json",
        })
        return session

    def get_dataset_ids(self, catalogue_id: str) -> list[str]:
        """Return all dataset IDs belonging to a catalogue.

        Hub Search exposes a catalogue-specific endpoint that returns the IDs
        directly. Unlike the generic /search + /scroll workflow, this requires
        one HTTP request and avoids downloading indexed dataset metadata that
        discovery does not need.
        """
        if not catalogue_id:
            raise ValueError("catalogue_id is required")

        url = f"{self.search_base_url}/datasets"
        logger.info("Hub Search: retrieving dataset IDs for catalogue=%s", catalogue_id)
        response = self.session.get(
            url,
            params={"catalogue": catalogue_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError(
                "Unexpected /datasets response: "
                f"expected JSON list, got {type(data).__name__}"
            )

        ids: list[str] = []
        seen: set[str] = set()
        for value in data:
            if value is None:
                continue
            dataset_id = str(value).strip()
            if not dataset_id or dataset_id in seen:
                continue
            seen.add(dataset_id)
            ids.append(dataset_id)

        logger.info(
            "Hub Search: catalogue=%s returned %d distinct dataset IDs",
            catalogue_id,
            len(ids),
        )
        return ids

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
