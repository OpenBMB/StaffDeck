#!/usr/bin/env python3
"""Propagate backend/VERSION into files that can't read it directly.

Run this once, as the last step before tagging a release, after backend/VERSION
has been edited to the new version number:

    scripts/sync_version.py

backend/pyproject.toml reads backend/VERSION directly (setuptools dynamic
version) and needs no sync. frontend-enterprise/package.json can't — npm has
no mechanism to read a version from an external file — so this script copies
the value in. CI (.github/workflows/release.yml) fails the release build if
package.json ever drifts from backend/VERSION, so a forgotten run here is
caught before it ships, not silently.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read_version() -> str:
    value = (REPO / "backend" / "VERSION").read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("backend/VERSION is empty")
    return value


def sync_package_json(version: str) -> bool:
    path = REPO / "frontend-enterprise" / "package.json"
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'"version": "[^"]*"', f'"version": "{version}"', text, count=1
    )
    if count == 0:
        raise SystemExit(f"could not find a version field in {path}")
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    version = read_version()
    if sync_package_json(version):
        print(f"synced {version} into frontend-enterprise/package.json")
    else:
        print(f"already in sync at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
