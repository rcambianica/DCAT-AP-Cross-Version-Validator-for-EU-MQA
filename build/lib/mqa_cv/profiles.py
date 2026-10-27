from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
import yaml

logger = logging.getLogger(__name__)


# Fallback deliberately kept stable with the v0.4.1 built-in set. It is used
# only when the validator's OpenAPI cannot be read and no valid profile list
# can be obtained from the local validator installation.
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
    "dcatap121",
    "dcatap121orig",
    "dcatap12",
    "dcatap11",
    "dcatap11orig",
]

# Backward-compatible alias used by older callers.
PROFILES = BUILTIN_PROFILES

PROFILE_RE = re.compile(r"^(?:dcatap|shapes)[A-Za-z0-9_-]+$", re.IGNORECASE)
LEVEL_RE = re.compile(r"^(?P<base>.+)level(?P<level>[123])$", re.IGNORECASE)
# Paths used when the validator source tree is available on the same machine.
DEFAULT_OPENAPI_PATHS = [
    Path("openapi-shacl.yaml"),
    Path("openapi-shacl.yml"),
    Path("src/main/resources/webroot/openapi-shacl.yaml"),
    Path("src/main/resources/webroot/openapi-shacl.yml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml"),
    Path("/opt/mqa/validating-shacl/src/main/resources/webroot/openapi-shacl.yml"),
    Path("/opt/validating-shacl/src/main/resources/webroot/openapi-shacl.yaml"),
    Path("/opt/validating-shacl/src/main/resources/webroot/openapi-shacl.yml"),
]


@dataclass(frozen=True)
class ProfileSource:
    profiles: list[str]
    source: str
    fallback: bool


def is_german_profile(profile: str) -> bool:
    """Return whether a profile identifier contains the German marker `de`.

    The exclusion is intentionally based on the identifier, as requested, not
    on translated labels or unrelated OpenAPI descriptions.
    """
    return "de" in profile.casefold()


def _unique(values: list[str]) -> list[str]:
    """Deduplicate strings case-insensitively, preserving original order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _filter_profiles(profiles: list[str]) -> list[str]:
    """Deduplicate profiles and exclude identifiers containing the German `de` marker."""
    excluded = list(dict.fromkeys(value for value in profiles if is_german_profile(value)))
    if excluded:
        logger.info("Excluding German shapeModel profile(s): %s", ", ".join(excluded))
    return _unique([value for value in profiles if not is_german_profile(value)])


def _extract_shape_models(document: object) -> list[str]:
    """Extract shapeModel enum values from an OpenAPI YAML/JSON document.

    We intentionally inspect the OpenAPI structure instead of applying a regex
    to arbitrary text. This avoids accidentally treating examples, descriptions
    or unrelated parameter values as validation profiles.
    """
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
                                if value.lower() != "customized":
                                    found.append(value)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    return _filter_profiles(found)


def _load_openapi_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    profiles = _extract_shape_models(document)
    if not profiles:
        raise ValueError(f"Nessun shapeModel DCAT-AP trovato in {path}")
    return profiles


def find_openapi_path(explicit: Path | None = None) -> Path | None:
    """Find the validator OpenAPI file on the local filesystem."""
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


def _default_openapi_urls(validator_endpoint: str) -> list[str]:
    """Candidate URLs for the OpenAPI served by the local validator."""
    parsed = urlsplit(validator_endpoint)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    return [
        f"{origin}/openapi-shacl.yaml",
        f"{origin}/openapi-shacl.yml",
    ]


def _load_openapi_url(url: str, timeout: float = 5.0) -> list[str]:
    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "Accept": "application/yaml, text/yaml, application/x-yaml, application/json, text/plain",
            "User-Agent": "MQA-Cross-Version-Validator/0.4.3 (+research; profile discovery)",
        },
    )
    response.raise_for_status()
    document = yaml.safe_load(response.text)
    profiles = _extract_shape_models(document)
    if not profiles:
        raise ValueError(f"Nessun shapeModel DCAT-AP trovato in {url}")
    return profiles


def discover_profiles(
    openapi_path: Path | None = None,
    *,
    validator_endpoint: str | None = None,
    openapi_url: str | None = None,
    timeout: float = 2.0,
) -> ProfileSource:
    """Discover supported profiles from the validator's OpenAPI.

    Priority:
      1. explicit OpenAPI path / MQA_SHACL_OPENAPI;
      2. standard local validator checkout paths;
      3. explicit MQA_SHACL_OPENAPI_URL;
      4. OpenAPI served by the validator endpoint;
      5. stable built-in fallback.

    A malformed *explicit* OpenAPI is an error rather than a silent fallback,
    because silently ignoring a configured validator specification would make
    the experiment difficult to reproduce.
    """
    path = find_openapi_path(openapi_path)
    if path is not None:
        profiles = _load_openapi_file(path)
        logger.info("Profile source: local OpenAPI %s (%d profiles)", path, len(profiles))
        return ProfileSource(profiles, f"file:{path}", False)

    env_url = os.environ.get("MQA_SHACL_OPENAPI_URL")
    candidates: list[str] = []
    if openapi_url:
        candidates.append(openapi_url)
    elif env_url:
        candidates.append(env_url)

    if validator_endpoint:
        candidates.extend(_default_openapi_urls(validator_endpoint))

    # De-duplicate while preserving order.
    candidates = _unique(candidates)
    for url in candidates:
        try:
            profiles = _load_openapi_url(url, timeout=timeout)
            logger.info("Profile source: validator OpenAPI %s (%d profiles)", url, len(profiles))
            return ProfileSource(profiles, f"url:{url}", False)
        except (requests.RequestException, OSError, ValueError, yaml.YAMLError) as exc:
            logger.debug("Could not use OpenAPI %s: %s", url, exc)

    fallback_profiles = _filter_profiles(BUILTIN_PROFILES)
    logger.warning(
        "Validator OpenAPI unavailable; falling back to built-in profile list (%d profiles)",
        len(fallback_profiles),
    )
    return ProfileSource(fallback_profiles, "builtin", True)


def _chain_key(profile: str) -> tuple[str, str | None]:
    """Return (chain key, level) for validation scheduling.

    Level profiles are grouped with their base profile. Historical `orig` and
    `conventions` variants are deliberately kept as independent chains, since
    they are not Level 1/2/3 continuations of the ordinary profile.
    """
    match = LEVEL_RE.fullmatch(profile)
    if match:
        return match.group("base"), match.group("level")
    return profile, None


def ordered_validation_groups(profiles: list[str]) -> list[list[str]]:
    """Return independent validation chains with Level 1 -> 2 -> 3 -> base order.

    Profiles without numbered levels form a one-item chain. Historical `orig` and
    `conventions` variants therefore remain independent from the normal profile.
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
