from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import requests


class HubClient:
    def __init__(self, base_url: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "mqa-cross-version/0.1"})

    def iter_dataset_ids(
        self,
        catalogue_id: str,
        page_size: int = 500,
        limit: int | None = None,
    ) -> Iterator[str]:
        offset = 0
        emitted = 0

        while True:
            remaining = None if limit is None else limit - emitted
            if remaining is not None and remaining <= 0:
                return

            current_limit = page_size if remaining is None else min(page_size, remaining)
            url = f"{self.base_url}/catalogues/{quote(catalogue_id)}/datasets"
            response = self.session.get(
                url,
                params={
                    "valueType": "identifiers",
                    "offset": offset,
                    "limit": current_limit,
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            items = response.json()

            if not items:
                return

            for item in items:
                yield str(item)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

            if len(items) < current_limit:
                return

            offset += len(items)

    def fetch_turtle(self, dataset_id: str) -> tuple[int, bytes]:
        url = f"{self.base_url}/datasets/{quote(dataset_id, safe='~-')}"
        response = self.session.get(
            url,
            headers={"Accept": "text/turtle"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.status_code, response.content


def safe_dataset_filename(dataset_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._~-]+", "_", dataset_id)
    digest = hashlib.sha256(dataset_id.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:120]}__{digest}.ttl"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_rdf(rdf_dir: Path, dataset_id: str, data: bytes) -> Path:
    rdf_dir.mkdir(parents=True, exist_ok=True)
    path = rdf_dir / safe_dataset_filename(dataset_id)
    path.write_bytes(data)
    return path
