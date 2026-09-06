from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from app import paths


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _data_root() -> Path:
    return paths.user_data_dir().resolve()


def _audit_cases_root() -> Path:
    return (_data_root() / "audit_cases").resolve()


def _assert_inside_root(target: Path, root: Path) -> None:
    if not target.is_relative_to(root):
        raise ValueError("audit case storage path escapes root")


def _assert_no_symlink_components(candidate: Path, root: Path) -> None:
    """Reject symlinks before reading or deleting controlled storage."""

    try:
        relative_parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("audit case storage path escapes root") from exc

    current = root
    for part in relative_parts:
        current /= part
        if current.is_symlink():
            raise ValueError("audit case storage path contains a symlink")


def _atomic_write(target: Path, data: bytes) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _safe_name(name: str) -> str:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("invalid audit case blob name")
    return name


def write_case_blob(
    *, tenant_id: str, audit_case_id: str, material_id: str, name: str, data: bytes
) -> str:
    data_root = _data_root()
    root = _audit_cases_root()
    safe_name = _safe_name(name)
    target = root / _digest(tenant_id) / _digest(audit_case_id) / _digest(material_id) / safe_name
    target = target.resolve()
    _assert_inside_root(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(target, root)
    _atomic_write(target, data)
    return target.relative_to(data_root).as_posix()


def read_case_blob(storage_key: str) -> bytes:
    data_root = _data_root()
    root = _audit_cases_root()
    candidate = data_root / storage_key
    target = candidate.resolve()
    _assert_inside_root(target, root)
    _assert_no_symlink_components(candidate, root)
    if not target.is_file():
        raise FileNotFoundError(storage_key)
    return target.read_bytes()


def delete_case_storage(tenant_id: str, audit_case_id: str) -> None:
    root = _audit_cases_root()
    case_dir = root / _digest(tenant_id) / _digest(audit_case_id)
    target = case_dir.resolve()
    _assert_inside_root(target, root)
    if not case_dir.exists():
        return
    _assert_no_symlink_components(case_dir, root)
    for directory, directory_names, file_names in os.walk(case_dir):
        directory_path = Path(directory)
        for name in [*directory_names, *file_names]:
            entry = directory_path / name
            if entry.is_symlink():
                raise ValueError("audit case storage contains a symlink")
    shutil.rmtree(case_dir)
