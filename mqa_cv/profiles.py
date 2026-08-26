from __future__ import annotations

import re
from pathlib import Path


# Profili osservati/esposti dalla versione corrente del validatore data.europa.eu
# usata per questo progetto. La lista incorporata elimina la dipendenza da un
# checkout locale di validating-shacl.
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
    """Etichetta leggibile per i profili che richiedono disambiguazione."""
    return PROFILE_LABELS.get(profile, profile)


PROFILE_RE = re.compile(
    r"\b(?:dcatap|shapes)\d+(?:level[123]|orig|conventions)?\b",
    re.IGNORECASE,
)


def discover_profiles(openapi_path: Path | None = None) -> list[str]:
    """Restituisce i profili disponibili per la cross-version analysis.

    Se viene fornito un file OpenAPI locale esistente, prova a ricavare i profili
    da quello. In caso contrario usa la lista incorporata, così il progetto può
    funzionare senza installare validating-shacl localmente.
    """
    if openapi_path is None:
        return BUILTIN_PROFILES.copy()

    if not openapi_path.is_file():
        raise FileNotFoundError(f"OpenAPI not found: {openapi_path}")

    text = openapi_path.read_text(encoding="utf-8")
    profiles: list[str] = []
    seen: set[str] = set()

    for match in PROFILE_RE.finditer(text):
        profile = match.group(0)
        if profile not in seen:
            seen.add(profile)
            profiles.append(profile)

    return profiles


def level1_profiles(profiles: list[str]) -> list[str]:
    """Selezione operativa per il confronto tra generazioni DCAT-AP.

    Per le versioni moderne seleziona i profili Level 1. Per le versioni storiche,
    che non espongono sempre la stessa suddivisione a livelli, mantiene gli shape
    set non-`orig`. Non implica equivalenza metodologica perfetta tra 1.x e 2/3.x.
    """
    selected: list[str] = []
    for p in profiles:
        low = p.lower()
        if "orig" in low:
            continue
        if "level1" in low:
            selected.append(p)
            continue
        if not any(tag in low for tag in ("level2", "level3")):
            # Evita di aggiungere il profilo completo di una versione moderna se
            # esiste gia' il corrispondente level1.
            base = re.sub(r"level[123]$", "", low)
            has_level1 = any(q.lower() == f"{base}level1" for q in profiles)
            if not has_level1:
                selected.append(p)
    return selected
