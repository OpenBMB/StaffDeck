from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

HarnessWorkspaceSnapshot = dict[str, tuple[int, int, int, int]]


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


_NOISE_ARTIFACT_SUFFIXES = (".tmp", ".part", ".partial", ".cache", ".log", ".lock")


def is_noise_artifact_path(relative_path: str) -> bool:
    """缓存/中间产物判定(自动补登时排除):点开头文件、__pycache__、临时/日志后缀。"""
    for part in relative_path.replace("\\", "/").split("/"):
        if part.startswith(".") or part == "__pycache__":
            return True
    lower = relative_path.lower()
    return lower.endswith(_NOISE_ARTIFACT_SUFFIXES)


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
    if sys.platform == "win32":
        return _open_harness_artifact_windows(root, relative_path)
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
        if metadata.st_nlink != 1:
            raise HarnessArtifactAccessError("Harness artifact hard links are denied.")
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


def publish_harness_artifacts(
    workspace_root: Path,
    task_frame_id: str,
    declarations: Sequence[object],
    *,
    operation: str = "publish_artifact",
    max_artifacts: int = 20,
    max_file_bytes: int = 50 * 1024 * 1024,
) -> list[dict[str, object]]:
    """Validate explicitly declared workspace files and build public metadata."""
    published: list[dict[str, object]] = []
    seen: set[str] = set()
    for declaration in declarations[:max_artifacts]:
        raw_path = (
            declaration.get("path")
            if isinstance(declaration, Mapping)
            else declaration
        )
        path = normalize_harness_artifact_path(str(raw_path or ""))
        if path in seen:
            continue
        opened = open_harness_artifact(workspace_root, path)
        try:
            if opened.size > max_file_bytes:
                raise HarnessArtifactAccessError("Harness artifact exceeds the file-size limit.")
            published.append(
                {
                    "type": "workspace_file",
                    "task_frame_id": task_frame_id,
                    "path": path,
                    "sha256": opened.sha256(),
                    "size": opened.size,
                    "operation": operation,
                }
            )
            seen.add(path)
        finally:
            opened.close()
    return published


def snapshot_harness_workspace(
    workspace_root: Path,
    *,
    max_entries: int = 1000,
) -> HarnessWorkspaceSnapshot:
    """Capture bounded regular-file identities for command artifact discovery."""

    root = Path(workspace_root).resolve(strict=True)
    snapshot: HarnessWorkspaceSnapshot = {}
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = [
            name
            for name in directory_names
            if name != ".harness-trash" and not (current / name).is_symlink()
        ]
        for name in file_names:
            path = current / name
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                continue
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
            if len(snapshot) > max_entries:
                raise HarnessArtifactAccessError(
                    "Harness workspace exceeds the artifact discovery entry limit."
                )
    return snapshot


def publish_changed_harness_artifacts(
    workspace_root: Path,
    task_frame_id: str,
    before: Mapping[str, tuple[int, int, int, int]],
    *,
    operation: str = "exec_command",
    max_entries: int = 1000,
    max_artifacts: int = 20,
    max_file_bytes: int = 50 * 1024 * 1024,
    path_filter: Callable[[str], bool] | None = None,
) -> list[dict[str, object]]:
    """Publish regular files created or modified by one sandboxed command."""

    after = snapshot_harness_workspace(workspace_root, max_entries=max_entries)
    changed = [
        {"path": path}
        for path, identity in sorted(after.items())
        if before.get(path) != identity
        and (path_filter is None or path_filter(path))
    ]
    return publish_harness_artifacts(
        workspace_root,
        task_frame_id,
        changed,
        operation=operation,
        max_artifacts=max_artifacts,
        max_file_bytes=max_file_bytes,
    )


def _open_harness_artifact_windows(root: Path, relative_path: str) -> OpenedHarnessArtifact:
    """Open a Windows artifact after rejecting every reparse/symlink component."""
    if os.name == "nt":
        return _open_harness_artifact_windows_native(root, relative_path)
    try:
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in PurePosixPath(relative_path).parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or current.is_symlink():
                raise HarnessArtifactAccessError("Harness artifact symlinks are denied.")
        resolved_file = current.resolve(strict=True)
        resolved_file.relative_to(resolved_root)
        descriptor = os.open(resolved_file, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise HarnessArtifactAccessError("Harness artifacts must be regular files.")
        if metadata.st_nlink != 1:
            os.close(descriptor)
            raise HarnessArtifactAccessError("Harness artifact hard links are denied.")
        return OpenedHarnessArtifact(
            descriptor,
            filename=resolved_file.name,
            size=metadata.st_size,
        )
    except HarnessArtifactAccessError:
        raise
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, ValueError) as exc:
        raise HarnessArtifactAccessError(
            "Harness artifact is unavailable or unsafe to open."
        ) from exc


def _open_harness_artifact_windows_native(
    root: Path, relative_path: str
) -> OpenedHarnessArtifact:
    """Open by handle and validate the final Windows file identity."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    file_attribute_directory = 0x10
    file_attribute_reparse_point = 0x400
    file_flag_open_reparse_point = 0x00200000
    generic_read = 0x80000000
    open_existing = 3
    share_all = 0x1 | 0x2 | 0x4
    invalid_handle = wintypes.HANDLE(-1).value

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(FileInformation)
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root.joinpath(*PurePosixPath(relative_path).parts)
    handle = kernel32.CreateFileW(
        str(candidate), generic_read, share_all, None, open_existing,
        file_flag_open_reparse_point, None,
    )
    if handle == invalid_handle:
        raise HarnessArtifactAccessError("Harness artifact is unavailable or unsafe to open.")
    try:
        info = FileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise HarnessArtifactAccessError("Harness artifact identity could not be verified.")
        if info.dwFileAttributes & (file_attribute_directory | file_attribute_reparse_point):
            raise HarnessArtifactAccessError("Harness artifacts must be non-reparse regular files.")
        if info.nNumberOfLinks != 1:
            raise HarnessArtifactAccessError("Harness artifact hard links are denied.")

        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise HarnessArtifactAccessError("Harness artifact final path could not be verified.")
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        try:
            Path(final_path).relative_to(resolved_root)
        except ValueError as exc:
            raise HarnessArtifactAccessError(
                "Harness artifact escapes the task workspace."
            ) from exc

        descriptor = msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        handle = invalid_handle
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        return OpenedHarnessArtifact(descriptor, filename=candidate.name, size=size)
    finally:
        if handle != invalid_handle:
            kernel32.CloseHandle(handle)


def _required_os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    if not isinstance(value, int) or value == 0:
        raise HarnessArtifactAccessError(
            f"Secure artifact access requires operating-system support for {name}."
        )
    return value


__all__ = [
    "HarnessArtifactAccessError",
    "HarnessWorkspaceSnapshot",
    "OpenedHarnessArtifact",
    "normalize_harness_artifact_path",
    "open_harness_artifact",
    "publish_changed_harness_artifacts",
    "publish_harness_artifacts",
    "snapshot_harness_workspace",
]
