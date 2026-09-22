from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # I metadata continuano a essere recuperati dal piveau Hub europeo.
    hub_base_url: str = "https://data.europa.eu/api/hub/repo"
    catalogue_id: str = "dati-gov-it"

    # Il validator SHACL usato di default e' l'istanza locale.
    validator_url: str = "http://127.0.0.1:8080/shacl/validation/report"

    # OpenAPI del checkout locale di validating-shacl. Se None, profiles.py
    # prova alcuni percorsi standard e poi usa la lista incorporata.
    openapi_path: Path | None = None

    db_path: Path = Path("analysis.db")
    rdf_dir: Path = Path("data/rdf")
    report_dir: Path = Path("reports")

    # Valori prudenti per un servizio locale. Per un endpoint remoto si possono
    # sovrascrivere dalla CLI.
    request_timeout: int = 30
    page_size: int = 500
    request_delay: float = 0.0
    request_retries: int = 0
