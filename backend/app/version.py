from __future__ import annotations

import os
import sys
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _read_repo_version() -> str | None:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


# Single source of truth for the "no better signal available" version: backend/VERSION
# (also what backend/pyproject.toml's dynamic version reads). Packaged builds never
# reach this — _bundled_version() wins first.
DEFAULT_APP_VERSION = _read_repo_version() or "0.0.0-dev"


def _bundled_version() -> str | None:
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "staffdeck-version.txt")
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        candidates.append(executable.parent / "staffdeck-version.txt")
        if sys.platform == "darwin" and len(executable.parents) >= 2:
            candidates.append(executable.parents[1] / "Resources" / "staffdeck-version.txt")
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def app_version() -> str:
    return (
        os.environ.get("STAFFDECK_VERSION", "").strip()
        or _bundled_version()
        or DEFAULT_APP_VERSION
    )


def update_check_enabled() -> bool:
    configured = os.environ.get("STAFFDECK_UPDATE_CHECK", "").strip().lower()
    if configured in _TRUE_VALUES:
        return True
    if configured in _FALSE_VALUES:
        return False
    return bool(getattr(sys, "frozen", False))


__all__ = ["app_version", "update_check_enabled"]
