import os
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

    # Network parameters.
    timeout: int = 60

    # Filesystem paths.
    rdf_dir: Path = Path("data/rdf")
    db_path: Path = Path("analysis.db")
    # Persistent PyOxigraph store: one named graph per SHACL validation report.
    report_store_path: Path = Path("data/validation-reports")

    # Local SHACL validator.
    validator_url: str = "http://localhost:8080/shacl/validation/report"
    validator_timeout: int = 120

    validator_openapi_path: Path | None = None

    def __post_init__(self) -> None:
        # Allow isolated benchmark runs against cloned databases without
        # changing the production analysis.db or RDF directory.
        if os.environ.get("MQA_DB_PATH"):
            self.db_path = Path(os.environ["MQA_DB_PATH"])
        if os.environ.get("MQA_RDF_DIR"):
            self.rdf_dir = Path(os.environ["MQA_RDF_DIR"])
        if os.environ.get("MQA_REPORT_STORE_PATH"):
            self.report_store_path = Path(os.environ["MQA_REPORT_STORE_PATH"])
        if os.environ.get("MQA_SHACL_VALIDATOR_TIMEOUT"):
            self.validator_timeout = int(os.environ["MQA_SHACL_VALIDATOR_TIMEOUT"])
