from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    # Piveau Hub Search API: discovers dataset IDs through its search index.
    search_base_url: str = "https://data.europa.eu/api/hub/search"

    # Piveau Hub Repo API: retrieves the RDF graph for a dataset ID.
    repo_base_url: str = "https://data.europa.eu/api/hub/repo"

    # The European portal currently exposes this Italian catalogue in Hub Search.
    catalogue_id: str = "dati-gov-it"

    # Network and pagination parameters.
    page_size: int = 100
    timeout: int = 60

    # Filesystem paths.
    rdf_dir: Path = Path("data/rdf")
    reports_dir: Path = Path("reports")
    db_path: Path = Path("analysis.db")

    # Local SHACL validator.
    validator_url: str = "http://127.0.0.1:8080/shacl/validation/report"
