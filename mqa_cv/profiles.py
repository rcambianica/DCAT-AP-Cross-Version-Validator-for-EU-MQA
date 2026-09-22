from __future__ import annotations

import os
import re
from pathlib import Path


# Fallback: profili osservati nel servizio data.europa.eu / validating-shacl.
# Quando possibile, per questa analisi e' preferibile usare l'OpenAPI della
# stessa istanza locale che esegue il validator.
BUILTIN_PROFILES = [
    "dcatap301level1",
    "dcatap301level2",
    "dcatap301level3",
    "dcatap301",
    "dcatap300level1",
    "dcatap300level2",
    "dcatap300level3",
    "dcatap300",
    "dcatap211level1",
    "dcatap211level2",
    "dcatap211level3",
    "dcatap211",
    "dcatap210level1",
    "dcatap210",
    "dcatap201",
    "dcatap200",
    "dcatap200conventions",
    "dcatap121",
    "dcatap121orig",
    "dcatap12",
    "dcatap11",
    "dcatap11orig",
]

PROFILE_LABELS = {
    "dcatap200": "DCAT-AP 2.0.0",
    "dcatap200conventions": "DCAT-AP 2.0.0 Conventions",
}


def profile_label(profile: str) -> str:
    return PROFILE_LABELS.get(profile, profile)


# Accetta sia gli identificativi dcatap... sia gli storici shapes....
PROFILE_RE = re.compile(
    r"\b(?:dcatap|shapes)[A-Za-z0-9_-]*(?:level[123]|orig|conventions)?\b",
    re.IGNORECASE,
)

# Percorsi tipici del checkout/build locale di validating-shacl.
DEFAULT_OPENAPI_PATHS = [
    Path("src/main/resources/webroot/openapi-shacl.yaml"),
    Path("src/main/resources/webroot/openapi-shacl.yml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yml"),
]


def _extract_profiles(text: str) -> list[str]:
    """Estrae gli shapeModel da un OpenAPI senza richiedere PyYAML.

    Il file puo' contenere piu' enum o riferimenti; manteniamo solo valori che
    assomigliano a shapeModel DCAT-AP e preserviamo l'ordine di apparizione.
    """
    profiles: list[str] = []
    seen: set[str] = set()

    for match in PROFILE_RE.finditer(text):
        profile = match.group(0)
        if profile.lower() in seen:
            continue
        seen.add(profile.lower())
        profiles.append(profile)

    return profiles


def find_openapi_path(explicit: Path | None = None) -> Path | None:
    """Trova l'OpenAPI locale se disponibile.

    Priorita': argomento CLI, variabile MQA_SHACL_OPENAPI, percorsi standard.
    """
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"OpenAPI not found: {explicit}")
        return explicit

    env_path = os.environ.get("MQA_SHACL_OPENAPI")
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"MQA_SHACL_OPENAPI not found: {path}")
        return path

    for path in DEFAULT_OPENAPI_PATHS:
        if path.is_file():
            return path

    return None


def discover_profiles(openapi_path: Path | None = None) -> list[str]:
    """Restituisce i profili dell'istanza locale, quando l'OpenAPI e' disponibile.

    Se non viene trovato un OpenAPI locale, usa il fallback incorporato. Questo
    evita di bloccare l'analisi, ma la lista fallback va considerata meno
    autorevole della configurazione della specifica istanza locale.
    """
    path = find_openapi_path(openapi_path)
    if path is None:
        return BUILTIN_PROFILES.copy()

    profiles = _extract_profiles(path.read_text(encoding="utf-8"))
    if not profiles:
        raise ValueError(f"Nessun shapeModel DCAT-AP trovato in {path}")
    return profiles


def level1_profiles(profiles: list[str]) -> list[str]:
    """Seleziona i profili Level 1 quando disponibili.

    Per le versioni storiche che non espongono Level 1/2/3, mantiene il profilo
    base non-orig. Non assume che shape set storici e moderni siano metodologicamente
    equivalenti.
    """
    selected: list[str] = []
    lower = [p.lower() for p in profiles]

    for p in profiles:
        low = p.lower()
        if "orig" in low or "conventions" in low:
            continue
        if "level1" in low:
            selected.append(p)
            continue
        if not any(tag in low for tag in ("level2", "level3")):
            base = re.sub(r"level[123]$", "", low)
            has_level1 = any(q == f"{base}level1" for q in lower)
            if not has_level1:
                selected.append(p)

    return selected
