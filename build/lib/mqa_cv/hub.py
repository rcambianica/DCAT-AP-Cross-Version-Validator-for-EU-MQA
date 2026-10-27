import json
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

logger = logging.getLogger(__name__)

# Parameters used by data.europa.eu Hub Search to filter the Italian catalogue.
_CATALOGUE_FACETS = {
    "country": [],
    "catalog": ["dati-gov-it"],
    "format": [],
    "formatTypes": [],
    "scoring": [],
    "license": [],
    "categories": [],
    "publisher": [],
    "subject": [],
    "keywords": [],
    "is_hvd": [],
    "hvdCategory": [],
    "superCatalog": [],
}


class HubClient:
    """Client for data.europa.eu Hub Search and Repo APIs."""

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
            "User-Agent": "MQA-Cross-Version-Validator (+sequential; category discovery)",
            "Accept": "application/json",
        })
        return session

    @staticmethod
    def _italian_label(title: Any, fallback: str) -> str:
        if isinstance(title, dict):
            return str(title.get("it") or title.get("en") or next(iter(title.values()), fallback))
        if isinstance(title, str) and title.strip():
            return title.strip()
        return fallback

    def get_categories(self, catalogue_id: str = "dati-gov-it") -> list[dict[str, Any]]:
        """Return category facets for a catalogue, with Italian labels when available."""
        facets = dict(_CATALOGUE_FACETS)
        facets["catalog"] = [catalogue_id]
        facets["categories"] = []
        params = {
            "filters": "catalogue,dataset,dataservice,datasetseries,resource",
            "limit": 0,
            "aggregation": "true",
            "aggregationAllFields": "true",
            "globalAggregation": "false",
            "includes": "id,categories.label,catalog.id",
            "facets": json.dumps(facets, ensure_ascii=False, separators=(",", ":")),
        }
        response = self.session.get(
            f"{self.search_base_url}/search", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {})
        category_facet = next(
            (facet for facet in result.get("facets", []) if facet.get("id") == "categories"),
            None,
        )
        if category_facet is None:
            available = [facet.get("id") for facet in result.get("facets", [])]
            raise RuntimeError(
                "Hub Search response did not contain the 'categories' facet "
                f"(available facets: {available})"
            )

        categories = []
        for item in category_facet.get("items", []):
            category_id = str(item.get("id", "")).strip()
            if not category_id:
                continue
            categories.append({
                "id": category_id,
                "label": self._italian_label(item.get("title"), category_id),
                "count": int(item.get("count", 0)),
            })
        categories.sort(key=lambda item: (item["label"].casefold(), item["id"]))
        logger.info("Hub Search: found %d categories for catalogue=%s", len(categories), catalogue_id)
        return categories

    def get_dataset_ids(self, catalogue_id: str) -> list[str]:
        """Return all dataset IDs belonging to a catalogue using the catalogue endpoint."""
        if not catalogue_id:
            raise ValueError("catalogue_id is required")

        url = f"{self.search_base_url}/datasets"
        logger.info("Hub Search: retrieving dataset IDs for catalogue=%s", catalogue_id)
        response = self.session.get(url, params={"catalogue": catalogue_id}, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(
                "Unexpected /datasets response: "
                f"expected JSON list, got {type(data).__name__}"
            )
        return self._unique_ids(data)

    @staticmethod
    def _unique_ids(values: list[Any]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                value = value.get("id")
            if value is None:
                continue
            dataset_id = str(value).strip()
            if not dataset_id or dataset_id in seen:
                continue
            seen.add(dataset_id)
            ids.append(dataset_id)
        return ids

    def get_dataset_ids_by_category(
        self,
        catalogue_id: str,
        category_id: str,
        limit: int | None = None,
        page_size: int = 100,
    ) -> list[str]:
        """Search dataset IDs in one category using Hub Search pagination.

        The category is a facet identifier such as ENVI, not its translated label.
        ``limit`` caps returned IDs; ``None`` follows the API's reported result count.
        """
        if not catalogue_id:
            raise ValueError("catalogue_id is required")
        if not category_id:
            raise ValueError("category_id is required")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")

        facets = dict(_CATALOGUE_FACETS)
        facets["catalog"] = [catalogue_id]
        facets["categories"] = [category_id]
        ids: list[str] = []
        seen: set[str] = set()
        page = 0

        while limit is None or len(ids) < limit:
            request_limit = page_size if limit is None else min(page_size, limit - len(ids))
            params = {
                "filters": "dataset",
                "limit": request_limit,
                "page": page,
                "includes": "id,categories.label,catalog.id",
                "facets": json.dumps(facets, ensure_ascii=False, separators=(",", ":")),
            }
            response = self.session.get(
                f"{self.search_base_url}/search", params=params, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result", {})
            results = result.get("results", [])
            if not isinstance(results, list):
                raise RuntimeError("Unexpected Hub Search response: result.results is not a list")

            page_ids = self._unique_ids(results)
            for dataset_id in page_ids:
                if dataset_id not in seen:
                    seen.add(dataset_id)
                    ids.append(dataset_id)
                    if limit is not None and len(ids) >= limit:
                        break

            logger.info(
                "Hub Search category=%s page=%d: received=%d distinct=%d",
                category_id, page, len(results), len(page_ids),
            )
            if not results or len(results) < request_limit or not page_ids:
                break
            page += 1

        logger.info(
            "Hub Search: category=%s catalogue=%s returned %d dataset IDs",
            category_id, catalogue_id, len(ids),
        )
        return ids

    def fetch_dataset_rdf(self, dataset_id: str) -> tuple[str, str]:
        """Retrieve a dataset's RDF graph through the Repo API."""
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
        return response.text, response.url
