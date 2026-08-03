from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator


class HarnessArtifactAccessError(RuntimeError):
    """Raised when a workspace artifact cannot be opened without escaping its root."""


class OpenedHarnessArtifact:
    """A regular workspace file held by descriptor for race-safe streaming."""

    def __init__(self, descriptor: int, *, filename: str, size: int) -> None:
        self._descriptor: int | None = descriptor
        self.filename = filename
        self.size = size

    def sha256(self) -> str:
        descriptor = self._require_descriptor()
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        descriptor = self._require_descriptor()
        try:
            while True:
                block = os.read(descriptor, chunk_size)
                if not block:
                    break
                yield block
        finally:
            self.close()

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _require_descriptor(self) -> int:
        if self._descriptor is None:
            raise HarnessArtifactAccessError("Harness artifact is already closed.")
        return self._descriptor


def normalize_harness_artifact_path(raw_path: str) -> str:
    """Return a canonical relative artifact path or fail closed."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HarnessArtifactAccessError("Artifact path cannot be empty.")
    if "\x00" in raw_path:
        raise HarnessArtifactAccessError("Artifact path cannot contain a null byte.")
    if PureWindowsPath(raw_path).drive:
        raise HarnessArtifactAccessError(
            "Absolute or drive-qualified artifact paths are denied."
        )
    normalized = PurePosixPath(raw_path.replace("\\", "/"))
    if normalized.is_absolute():
        raise HarnessArtifactAccessError("Absolute artifact paths are denied.")
    parts = tuple(part for part in normalized.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise HarnessArtifactAccessError("Artifact path traversal is denied.")
    if ".harness-trash" in parts:
        raise HarnessArtifactAccessError("Harness internal paths are denied.")
    return PurePosixPath(*parts).as_posix()


def open_harness_artifact(
    workspace_root: Path,
    raw_path: str,
) -> OpenedHarnessArtifact:
    """Open one regular file beneath ``workspace_root`` without following symlinks.

    Every directory from the filesystem root through the requested file is opened
    relative to its parent descriptor with ``O_NOFOLLOW``. The returned descriptor
    remains bound to the verified file even if a path is renamed after this check.
    """

    root = Path(workspace_root)
    if not root.is_absolute():
        raise HarnessArtifactAccessError("Harness workspace root must be absolute.")
    relative_path = normalize_harness_artifact_path(raw_path)
    no_follow = _required_os_flag("O_NOFOLLOW")
    directory = _required_os_flag("O_DIRECTORY")
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | directory | no_follow | close_on_exec
    file_flags = os.O_RDONLY | no_follow | close_on_exec
    opened_directories: list[int] = []
    file_descriptor: int | None = None

    try:
        current = os.open(Path(root.anchor).as_posix(), directory_flags)
        opened_directories.append(current)
        for part in root.parts[1:]:
            current = os.open(part, directory_flags, dir_fd=current)
            opened_directories.append(current)
        path_parts = PurePosixPath(relative_path).parts
        for part in path_parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            opened_directories.append(current)
        file_descriptor = os.open(path_parts[-1], file_flags, dir_fd=current)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessArtifactAccessError(
                "Harness artifacts must be regular files."
            )
        opened = OpenedHarnessArtifact(
            file_descriptor,
            filename=path_parts[-1],
            size=metadata.st_size,
        )
        file_descriptor = None
        return opened
    except HarnessArtifactAccessError:
        raise
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        raise HarnessArtifactAccessError(
            "Harness artifact is unavailable or unsafe to open."
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(opened_directories):
            os.close(descriptor)


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    if not isinstance(value, int) or value == 0:
        raise HarnessArtifactAccessError(
            f"Secure artifact access requires operating-system support for {name}."
        )
    return value


__all__ = [
    "HarnessArtifactAccessError",
    "OpenedHarnessArtifact",
    "normalize_harness_artifact_path",
    "open_harness_artifact",
]
