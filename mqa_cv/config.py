from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    hub_base_url: str = "https://data.europa.eu/api/hub/repo"
    catalogue_id: str = "dati-gov-it"

    # API SHACL pubblica di data.europa.eu.
    validator_url: str = "https://data.europa.eu/api/mqa/shacl/validation/report/"

    # Opzionale: se valorizzato, i profili vengono scoperti da un openapi-shacl.yaml
    # locale. Se None, viene usata la lista incorporata in mqa_cv/profiles.py.
    openapi_path: Path | None = None

    db_path: Path = Path("analysis.db")
    rdf_dir: Path = Path("data/rdf")
    report_dir: Path = Path("reports")
    request_timeout: int = 120
    page_size: int = 500

    # Il validatore UE e' un servizio pubblico: un piccolo intervallo evita burst.
    request_delay: float = 0.25
    request_retries: int = 3
