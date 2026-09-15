"""Presets are declarative selections, never an alternate execution implementation."""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import config_path


ASSEMBLY_FIELDS = {"engine", "selections", "disabled_modules", "enabled_modules", "security_profile", "module_configs"}


def _custom_store(settings):
    # Deployment control storage, not the selected business Runtime database.
    path = config_path(settings).with_suffix(".presets.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS assembly_presets (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL, assembly TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by TEXT NOT NULL, source TEXT NOT NULL
    )""")
    return connection


def _public_row(row):
    return {key: row[key] for key in ("id", "name", "description", "created_at", "source")} | {
        "assembly": json.loads(row["assembly"]), "custom": True,
    }


def save_preset(settings, *, name, description, assembly, source, by):
    name = name.strip()
    if not name or len(name) > 80 or len(description) > 500:
        raise ValueError("预设名称不能为空且不超过 80 字，说明不超过 500 字")
    if any(item["name"].casefold() == name.casefold() for item in load_presets(settings)):
        raise FileExistsError("同名预设已存在，请使用其他名称")
    # Snapshot only public choices. Never copy Base credentials, paths, package specs,
    # user data or deployment connection settings into a reusable preset.
    patch = {key: value for key, value in assembly.items() if key in ASSEMBLY_FIELDS}
    from .parameters import validate_public_parameters
    validate_public_parameters(patch.get("module_configs", {}))
    identifier = "custom-" + uuid4().hex
    connection = _custom_store(settings)
    try:
        with connection:
            connection.execute("INSERT INTO assembly_presets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                identifier, name, name.casefold(), description.strip(),
                json.dumps(patch, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), by, source,
            ))
        return _public_row(connection.execute("SELECT * FROM assembly_presets WHERE id = ?", (identifier,)).fetchone())
    except sqlite3.IntegrityError:
        raise FileExistsError("同名预设已存在，请使用其他名称") from None
    finally:
        connection.close()


def load_presets(settings):
    directory = str(getattr(settings, "harness_presets_dir", "") or "")
    result = []
    for path in sorted(Path(directory).glob("*.json")) if directory else []:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError(f"invalid preset schema: {path.name}")
        # No paths, credentials, or arbitrary Python package specifications in UI presets.
        patch = raw.get("assembly", {})
        if not isinstance(patch, dict):
            raise ValueError(f"invalid preset assembly: {path.name}")
        if set(patch) - ASSEMBLY_FIELDS:
            raise ValueError(f"unsupported preset fields: {path.name}")
        from staffdeck_harness.modules.parameters import validate_public_parameters
        validate_public_parameters(patch.get("module_configs", {}))
        result.append({"id": path.stem, "name": raw.get("name", path.stem),
                       "description": raw.get("description", ""), "assembly": patch, "custom": False})
    path = config_path(settings).with_suffix(".presets.sqlite3")
    if path.exists():
        connection = _custom_store(settings)
        try:
            result.extend(_public_row(row) for row in connection.execute("SELECT * FROM assembly_presets ORDER BY created_at, id"))
        finally:
            connection.close()
    return result
