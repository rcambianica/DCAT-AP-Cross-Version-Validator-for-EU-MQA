from __future__ import annotations

import os
import re
from pathlib import Path

# Fallback only. The authoritative list should come from the local validator's
# OpenAPI whenever possible.
BUILTIN_PROFILES = [
    "dcatap301level1", "dcatap301level2", "dcatap301level3", "dcatap301",
    "dcatap300level1", "dcatap300level2", "dcatap300level3", "dcatap300",
    "dcatap211level1", "dcatap211level2", "dcatap211level3", "dcatap211",
    "dcatap210level1", "dcatap210", "dcatap201", "dcatap200",
    "dcatap200conventions", "dcatap121", "dcatap121orig", "dcatap12",
    "dcatap11", "dcatap11orig",
]

PROFILES = BUILTIN_PROFILES

PROFILE_LABELS = {
    "dcatap200": "DCAT-AP 2.0.0",
    "dcatap200conventions": "DCAT-AP 2.0.0 Conventions",
}


def profile_label(profile: str) -> str:
    return PROFILE_LABELS.get(profile, profile)

PROFILE_RE = re.compile(
    r"\b(?:dcatap|shapes)[A-Za-z0-9_-]*(?:level[123]|orig|conventions)?\b",
    re.IGNORECASE,
)

DEFAULT_OPENAPI_PATHS = [
    Path("src/main/resources/webroot/openapi-shacl.yaml"),
    Path("src/main/resources/webroot/openapi-shacl.yml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yml"),
]


def _extract_profiles(text: str) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()
    for match in PROFILE_RE.finditer(text):
        profile = match.group(0)
        key = profile.lower()
        if key not in seen:
            seen.add(key)
            profiles.append(profile)
    return profiles


def find_openapi_path(explicit: Path | None = None) -> Path | None:
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
    path = find_openapi_path(openapi_path)
    if path is None:
        return BUILTIN_PROFILES.copy()
    profiles = _extract_profiles(path.read_text(encoding="utf-8"))
    if not profiles:
        raise ValueError(f"No DCAT-AP shapeModel found in {path}")
    return profiles


def level1_profiles(profiles: list[str]) -> list[str]:
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
            if f"{base}level1" not in lower:
                selected.append(p)
    return selected
