"""Resolve the immutable GitHub repository trusted by a packaged StaffDeck build."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_RELEASE_REPOSITORY = "OpenBMB/StaffDeck"
DISTRIBUTION_METADATA_FILENAME = "staffdeck-distribution.json"
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?")


def validate_release_repository(value: str | None) -> str | None:
    """Return a canonical owner/repository identity, or None for any URL-like or invalid input."""
    if not isinstance(value, str) or value != value.strip() or value.count("/") != 1:
        return None
    owner, repository = value.split("/", 1)
    if not _OWNER_PATTERN.fullmatch(owner) or not _REPOSITORY_PATTERN.fullmatch(repository):
        return None
    if repository in {".", ".."} or repository.endswith(".git") or ".." in repository:
        return None
    return value


def _bundled_metadata_candidates() -> list[Path]:
    """List packaged metadata locations in loader priority order without reading the filesystem."""
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / DISTRIBUTION_METADATA_FILENAME)
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        candidates.append(executable.parent / DISTRIBUTION_METADATA_FILENAME)
        if sys.platform == "darwin" and len(executable.parents) >= 2:
            candidates.append(executable.parents[1] / "Resources" / DISTRIBUTION_METADATA_FILENAME)
    return candidates


def _repository_from_metadata(payload: Any) -> str | None:
    """Validate the exact bundled JSON object and return its sole repository identity."""
    if not isinstance(payload, dict) or set(payload) != {"release_repository"}:
        return None
    return validate_release_repository(payload.get("release_repository"))


def _bundled_release_repository() -> str | None:
    """Read the first present metadata resource; malformed content fails closed without fallback."""
    for candidate in _bundled_metadata_candidates():
        try:
            content = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        return _repository_from_metadata(payload)
    return None


def release_repository() -> str | None:
    """Resolve runtime identity while preventing frozen apps from accepting mutable overrides."""
    if getattr(sys, "frozen", False):
        return _bundled_release_repository()
    configured = os.environ.get("STAFFDECK_RELEASE_REPOSITORY")
    if configured is None or configured == "":
        return DEFAULT_RELEASE_REPOSITORY
    return validate_release_repository(configured)


def _repository_from_remote_url(remote_url: str) -> str | None:
    """Convert supported GitHub HTTPS and SSH remotes into a canonical repository identity."""
    if remote_url.startswith("git@github.com:"):
        repository = remote_url.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(remote_url)
        try:
            port = parsed.port
        except ValueError:
            return None
        valid_https = (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )
        valid_ssh = (
            parsed.scheme == "ssh"
            and parsed.hostname == "github.com"
            and parsed.username == "git"
            and parsed.password is None
            and port in {None, 22}
        )
        if not (valid_https or valid_ssh) or parsed.query or parsed.fragment:
            return None
        repository = parsed.path.lstrip("/")
    repository = repository.removesuffix(".git")
    return validate_release_repository(repository)


def _origin_release_repository(repository_root: Path) -> str | None:
    """Read the local origin with a bounded Git command and ignore missing or foreign remotes."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _repository_from_remote_url(result.stdout.strip())


def _configured_build_repository(variable_name: str) -> str | None:
    """Validate one present build variable; malformed explicit input raises instead of falling back."""
    configured = os.environ.get(variable_name)
    if configured is None or configured == "":
        return None
    validated = validate_release_repository(configured)
    if validated is None:
        raise ValueError(f"{variable_name} must be a canonical owner/repository")
    return validated


def resolve_build_release_repository(repository_root: Path) -> str:
    """Resolve package identity from explicit input, CI, GitHub origin, then the safe default."""
    # A caller-supplied identity is authoritative for controlled local and test builds.
    explicit = _configured_build_repository("STAFFDECK_RELEASE_REPOSITORY")
    if explicit is not None:
        return explicit

    # GitHub Actions identifies the repository actually distributing the release artifact.
    workflow_repository = _configured_build_repository("GITHUB_REPOSITORY")
    if workflow_repository is not None:
        return workflow_repository

    # Local packaging follows a validated GitHub origin without trusting other hosting services.
    origin_repository = _origin_release_repository(repository_root)
    if origin_repository is not None:
        return origin_repository

    # Source archives without Git metadata preserve the historical upstream release default.
    return DEFAULT_RELEASE_REPOSITORY


def write_distribution_metadata(destination: Path, repository: str) -> Path:
    """Validate and write immutable package metadata; invalid build input raises ValueError."""
    validated = validate_release_repository(repository)
    if validated is None:
        raise ValueError(f"invalid StaffDeck release repository: {repository!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"release_repository": validated}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return destination


__all__ = [
    "DEFAULT_RELEASE_REPOSITORY",
    "DISTRIBUTION_METADATA_FILENAME",
    "release_repository",
    "resolve_build_release_repository",
    "validate_release_repository",
    "write_distribution_metadata",
]
