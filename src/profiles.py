from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)


PROFILE_RE = re.compile(r"^(?:dcatap|shapes)[A-Za-z0-9_-]+$", re.IGNORECASE)
LEVEL_RE = re.compile(r"^(?P<base>.+)level(?P<level>[123])$", re.IGNORECASE)
DEFAULT_OPENAPI_PATH = Path("openapi-shacl.yaml")


@dataclass(frozen=True)
class ProfileSource:
    profiles: list[str]
    source: str


def is_german_profile(profile: str) -> bool:
    """Return whether a profile identifier contains the German marker `de`."""
    return "de" in profile.casefold()


def is_orig_profile(profile: str) -> bool:
    """Return whether a profile identifier contains the marker `orig`."""
    return "orig" in profile.casefold()


def _unique(values: list[str]) -> list[str]:
    """Deduplicate strings case-insensitively, preserving original order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _filter_profiles(profiles: list[str]) -> list[str]:
    """Exclude German and historical `orig` profiles, preserving source order."""
    excluded = list(dict.fromkeys(
        value for value in profiles
        if is_german_profile(value) or is_orig_profile(value)
    ))
    if excluded:
        logger.info("Excluding shapeModel profile(s): %s", ", ".join(excluded))
    return _unique([
        value for value in profiles
        if not is_german_profile(value) and not is_orig_profile(value)
    ])


def _extract_shape_models(document: object) -> list[str]:
    """Extract and filter shapeModel enum values from the local OpenAPI YAML."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("name") == "shapeModel":
                schema = node.get("schema")
                if isinstance(schema, dict):
                    enum = schema.get("enum")
                    if isinstance(enum, list):
                        for value in enum:
                            if isinstance(value, str) and PROFILE_RE.fullmatch(value):
                                if value.casefold() != "customized":
                                    found.append(value)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    profiles = _filter_profiles(found)
    if not profiles:
        raise ValueError("Nessun shapeModel DCAT-AP valido trovato nell'OpenAPI locale")
    return profiles


def find_openapi_path(explicit: Path | None = None) -> Path:
    """Resolve the one authoritative local OpenAPI YAML file.

    MQA_SHACL_OPENAPI can point to the file in the local validating-shacl
    checkout. Without it, the expected path is ./openapi-shacl.yaml.
    No network lookup or built-in profile fallback is performed.
    """
    configured = explicit
    if configured is None:
        env_path = os.environ.get("MQA_SHACL_OPENAPI")
        configured = Path(env_path).expanduser() if env_path else DEFAULT_OPENAPI_PATH
    path = configured.expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"File OpenAPI SHACL non trovato: {path}. "
            "Imposta MQA_SHACL_OPENAPI sul percorso locale di openapi-shacl.yaml."
        )
    return path


def discover_profiles(openapi_path: Path | None = None) -> ProfileSource:
    """Load profiles exclusively from the local validating-shacl OpenAPI YAML."""
    path = find_openapi_path(openapi_path)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"OpenAPI YAML non valido in {path}: {exc}") from exc
    profiles = _extract_shape_models(document)
    logger.info("Profile source: local OpenAPI %s (%d profiles)", path, len(profiles))
    return ProfileSource(profiles, f"file:{path}")


def _chain_key(profile: str) -> tuple[str, str | None]:
    """Return (chain key, level) for validation scheduling.

    Level profiles are grouped with their base profile. Profiles excluded by
    the configured filters never enter the validation plan.
    """
    match = LEVEL_RE.fullmatch(profile)
    if match:
        return match.group("base"), match.group("level")
    return profile, None


def ordered_validation_groups(profiles: list[str]) -> list[list[str]]:
    """Return independent validation chains with Level 1 -> 2 -> 3 -> base order.

    Profiles without numbered levels form a one-item chain.
    """
    groups: list[tuple[str, list[str]]] = []
    by_key: dict[str, int] = {}

    for profile in _filter_profiles(profiles):
        key, _ = _chain_key(profile)
        if key not in by_key:
            by_key[key] = len(groups)
            groups.append((key, []))
        groups[by_key[key]][1].append(profile)

    ordered: list[list[str]] = []
    for _, members in groups:
        levels: dict[int, str] = {}
        bases: list[str] = []
        for profile in members:
            match = LEVEL_RE.fullmatch(profile)
            if match:
                levels[int(match.group("level"))] = profile
            else:
                bases.append(profile)

        chain = [levels[level] for level in (1, 2, 3) if level in levels]
        chain.extend(bases)
        ordered.append(chain)

    return ordered


def ordered_validation_plan(profiles: list[str]) -> list[str]:
    """Flatten ordered chains for display; chains themselves are used by runner."""
    return [profile for group in ordered_validation_groups(profiles) for profile in group]
