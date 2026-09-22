from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    # Piveau Hub Search API (Elasticsearch-backed search service).
    # We use the public Search API, not Elasticsearch directly.
    search_base_url: str = "https://data.europa.eu/api/hub/search"

    # Piveau Hub Repo API: source of the RDF metadata graph.
    repo_base_url: str = "https://data.europa.eu/api/hub/repo"

    # Catalogue to analyse.
    catalogue_id: str = "dati-lombardia-it"

    # Acquisition parameters.
    page_size: int = 100
    timeout: int = 60
    search_retries: int = 2
    fetch_retries: int = 2

    # Filesystem.
    rdf_dir: Path = Path("data/rdf")
    reports_dir: Path = Path("reports")
    db_path: Path = Path("analysis.db")

    # Local SHACL validator.
    validator_url: str = "http://192.168.122.221:8080/shacl/validation/report"
    validator_timeout: int = 30
    validator_retries: int = 0
    validator_request_delay: float = 0.0
