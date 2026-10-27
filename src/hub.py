import json
import logging
from typing import Any
from urllib.parse import quote

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

    def get_dataset_ids_by_categories(
        self,
        catalogue_id: str,
        category_ids: list[str],
        limit: int | None = None,
        category_count: int | None = None,
    ) -> list[str]:
        """Retrieve IDs for selected categories using Hub Search scroll.

        Hub Search limits each search response to 1000 results. The scroll API
        keeps one search context and allows retrieving the full result set without
        incrementing ordinary page numbers.
        """
        if not catalogue_id:
            raise ValueError("catalogue_id is required")
        category_ids = list(dict.fromkeys(x.strip() for x in category_ids if x and x.strip()))
        if not category_ids:
            raise ValueError("at least one category_id is required")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")

        facets = dict(_CATALOGUE_FACETS)
        facets["catalog"] = [catalogue_id]
        facets["categories"] = category_ids
        # The API caps a single response at 1000. The category count is only
        # a useful estimate of the expected result size, not a valid API limit.
        request_limit = min(1000, limit) if limit is not None else 1000
        params = {
            "filters": "dataset",
            "limit": request_limit,
            "page": 0,
            "scroll": "true",
            "includes": "id,categories.label,catalog.id",
            "facets": json.dumps(facets, ensure_ascii=False, separators=(",", ":")),
        }
        logger.info(
            "Hub Search: category discovery with scroll catalogue=%s categories=%s batch_limit=%d expected_count=%s",
            catalogue_id, ",".join(category_ids), request_limit, category_count,
        )
        response = self.session.get(
            f"{self.search_base_url}/search", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()

        def unpack(current_payload: dict[str, Any]) -> tuple[list[Any], str | None]:
            result = current_payload.get("result", {})
            results = result.get("results", [])
            if not isinstance(results, list):
                raise RuntimeError("Unexpected Hub Search response: result.results is not a list")
            scroll_id = (
                current_payload.get("scrollId")
                or current_payload.get("scroll_id")
                or result.get("scrollId")
                or result.get("scroll_id")
            )
            return results, str(scroll_id) if scroll_id else None

        results, scroll_id = unpack(payload)
        ids = self._unique_ids(results)
        if limit is None and len(results) >= request_limit and not scroll_id:
            raise RuntimeError(
                "Hub Search returned a full batch but no scrollId; discovery may be incomplete. "
                "Check the API response or retry."
            )

        while scroll_id and (limit is None or len(ids) < limit):
            scroll_response = self.session.get(
                f"{self.search_base_url}/scroll",
                params={"scrollId": scroll_id},
                timeout=self.timeout,
            )
            scroll_response.raise_for_status()
            scroll_payload = scroll_response.json()
            next_results, next_scroll_id = unpack(scroll_payload)
            if not next_results:
                # An empty scroll batch marks successful exhaustion of the result set.
                break
            if limit is None and len(next_results) >= request_limit and not next_scroll_id:
                raise RuntimeError(
                    "Hub Search returned a full scroll batch but no next scrollId; "
                    "discovery may be incomplete. Check the API response or retry."
                )
            ids = self._unique_ids(ids + next_results)
            scroll_id = next_scroll_id

        if limit is not None:
            ids = ids[:limit]
        logger.info(
            "Hub Search: categories=%s catalogue=%s returned %d unique dataset IDs via scroll",
            ",".join(category_ids), catalogue_id, len(ids),
        )
        return ids

    def get_dataset_ids_by_category(
        self,
        catalogue_id: str,
        category_id: str,
        limit: int | None = None,
        page_size: int = 100,
    ) -> list[str]:
        """Backward-compatible wrapper for one-category discovery.

        ``page_size`` is accepted for compatibility but no longer used: discovery
        intentionally performs a single Hub Search request.
        """
        del page_size
        return self.get_dataset_ids_by_categories(
            catalogue_id, [category_id], limit=limit
        )

    def fetch_dataset_rdf(self, dataset_id: str) -> tuple[str, str]:
        """Retrieve a dataset's RDF graph through the current Repo resources API."""
        resource_id = quote(dataset_id, safe="-~")
        url = f"{self.repo_base_url}/resources/{resource_id}"
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
