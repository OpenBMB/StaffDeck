from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.capabilities.contracts import GeneralSkillPackage
from app.capabilities.local_general_skill import package_from_row
from app.db.models import GeneralSkill


def materialize_general_skill_package(
    skill: GeneralSkill,
    target_dir: Path,
) -> GeneralSkillPackage:
    """Restore one stored GeneralSkill package below a trusted workspace path.

    The package data is stored in the database rather than as a persistent ZIP
    on disk. Both the legacy Runner and Harness-native execution need the same
    safe, on-disk view of that snapshot.
    """

    package = package_from_row(skill)
    target_dir.mkdir(parents=True, exist_ok=True)

    metadata = skill.metadata_json if isinstance(skill.metadata_json, Mapping) else {}
    directory_values = metadata.get("skill_directories", [])
    if isinstance(directory_values, Sequence) and not isinstance(
        directory_values, (str, bytes)
    ):
        for value in directory_values:
            relative_path = safe_package_path(str(value or ""))
            if relative_path:
                (target_dir / relative_path).mkdir(parents=True, exist_ok=True)

    for file in package.files:
        relative_path = safe_package_path(file.path)
        if not relative_path:
            continue
        output_path = target_dir / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(file.content, encoding="utf-8")

    return package


def safe_package_path(path: str) -> str:
    """Normalize a package-relative path and reject traversal or unsafe parts."""

    cleaned = path.replace("\\", "/").strip().strip("/")
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def skill_package_directory_name(slug: str, digest: str) -> str:
    """Return a short, human-readable immutable package directory name."""

    normalized_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-")
    normalized_slug = normalized_slug[:80] or "general-skill"
    normalized_digest = digest.removeprefix("sha256:")
    short_digest = re.sub(r"[^A-Fa-f0-9]", "", normalized_digest)[:12]
    if not short_digest:
        raise ValueError("GeneralSkill package digest is invalid.")
    return f"{normalized_slug}--{short_digest.lower()}"
