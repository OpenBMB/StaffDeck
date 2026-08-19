from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.harness.contracts import HarnessToolContext
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.harness.errors import HarnessExecutionError
from app.harness.registry import HarnessRegistry
from app.knowledge.parser import KnowledgeParseError, extract_text

_TRASH_DIRECTORY = ".harness-trash"
_SHA256_PATTERN = r"^[A-Fa-f0-9]{64}$"
_MAX_GREP_LINE_CHARS = 400


class _FileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileArguments(_FileArguments):
    path: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    max_bytes: int | None = Field(default=None, ge=1)


class ExtractDocumentTextArguments(_FileArguments):
    path: str = Field(min_length=1)
    output_path: str | None = Field(default=None, min_length=1)


class WriteFileArguments(_FileArguments):
    path: str = Field(min_length=1)
    content: str
    create_parents: bool = False
    expected_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class EditFileArguments(_FileArguments):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False
    expected_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class ListDirectoryArguments(_FileArguments):
    path: str = "."
    recursive: bool = False
    max_entries: int | None = Field(default=None, ge=1)


class GlobArguments(_FileArguments):
    pattern: str = Field(min_length=1)
    path: str = "."
    max_results: int | None = Field(default=None, ge=1)


class GrepArguments(_FileArguments):
    pattern: str = Field(min_length=1)
    path: str = "."
    file_glob: str | None = None
    # Python's backtracking ``re`` engine has no per-match timeout. Keep the
    # first Harness version literal-only so model-provided patterns cannot
    # stall a worker through catastrophic backtracking.
    regex: Literal[False] = False
    case_sensitive: bool = True
    max_results: int | None = Field(default=None, ge=1)


class FileInfoArguments(_FileArguments):
    path: str = Field(min_length=1)


class PublishArtifactArguments(_FileArguments):
    path: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=180)
    description: str | None = Field(default=None, max_length=500)


class MakeDirectoryArguments(_FileArguments):
    path: str = Field(min_length=1)
    parents: bool = True
    exist_ok: bool = True


class DeleteFileArguments(_FileArguments):
    path: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class MoveFileArguments(_FileArguments):
    source_path: str = Field(min_length=1)
    destination_path: str = Field(min_length=1)
    create_parents: bool = False
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    expected_destination_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )


class CopyFileArguments(MoveFileArguments):
    pass


class _Workspace:
    def __init__(self, context: HarnessToolContext) -> None:
        root = context.workspace_root
        try:
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise HarnessExecutionError(
                    "INVALID_WORKSPACE",
                    "Harness workspace root cannot be a symbolic link.",
                )
            self.root = root.resolve(strict=True)
        except HarnessExecutionError:
            raise
        except OSError as exc:
            raise HarnessExecutionError(
                "INVALID_WORKSPACE",
                "Harness workspace root is unavailable.",
                details={"exception_type": type(exc).__name__},
            ) from exc
        if not self.root.is_dir():
            raise HarnessExecutionError(
                "INVALID_WORKSPACE",
                "Harness workspace root is not a directory.",
            )
        self.context = context

    def resolve(
        self,
        raw_path: str,
        *,
        allow_root: bool = False,
    ) -> Path:
        candidate = _normalize_access_path(raw_path, root=self.root, allow_root=allow_root)
        if _TRASH_DIRECTORY in candidate.parts:
            raise HarnessExecutionError(
                "RESERVED_PATH",
                "The Harness internal trash directory is not directly accessible.",
                details={"path": str(candidate)},
            )
        self._reject_symlink_components(candidate)
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(path)

    def is_inside_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def require_file(self, path: Path) -> os.stat_result:
        self._reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise HarnessExecutionError(
                "NOT_FOUND",
                "File does not exist.",
                details={"path": self.relative(path)},
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            self._raise_symlink(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessExecutionError(
                "NOT_A_FILE",
                "Path is not a regular file.",
                details={"path": self.relative(path)},
            )
        return metadata

    def require_directory(self, path: Path) -> os.stat_result:
        self._reject_symlink_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise HarnessExecutionError(
                "NOT_FOUND",
                "Directory does not exist.",
                details={"path": self.relative(path)},
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            self._raise_symlink(path)
        if not stat.S_ISDIR(metadata.st_mode):
            raise HarnessExecutionError(
                "NOT_A_DIRECTORY",
                "Path is not a directory.",
                details={"path": self.relative(path)},
            )
        return metadata

    def prepare_parent(self, path: Path, *, create: bool) -> None:
        parent = path.parent
        self._reject_symlink_components(parent)
        if not parent.exists():
            if not create:
                raise HarnessExecutionError(
                    "PARENT_NOT_FOUND",
                    "Parent directory does not exist.",
                    details={"path": self.relative(parent)},
                )
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HarnessExecutionError(
                    "IO_ERROR",
                    "Parent directory could not be created.",
                    details={"exception_type": type(exc).__name__},
                ) from exc
        self.require_directory(parent)
        self._reject_symlink_components(parent)

    def assert_expected_hash(
        self,
        path: Path,
        expected_sha256: str | None,
        *,
        actual_sha256: str | None = None,
    ) -> str:
        actual = actual_sha256 or _sha256(path)
        if expected_sha256 and actual.lower() != expected_sha256.lower():
            raise HarnessExecutionError(
                "HASH_MISMATCH",
                "File changed since the caller last read it.",
                retryable=True,
                details={
                    "path": self.relative(path),
                    "expected_sha256": expected_sha256.lower(),
                    "actual_sha256": actual,
                },
            )
        return actual

    def ensure_file_size(self, size: int) -> None:
        if size > self.context.limits.max_file_bytes:
            raise HarnessExecutionError(
                "FILE_TOO_LARGE",
                "File exceeds the Harness per-file size limit.",
                details={
                    "actual_bytes": size,
                    "max_bytes": self.context.limits.max_file_bytes,
                },
            )

    def ensure_workspace_capacity(
        self,
        *,
        path: Path,
        replacing_bytes: int,
        new_bytes: int,
    ) -> None:
        if not self.is_inside_workspace(path):
            return
        current = self.workspace_bytes()
        projected = current - replacing_bytes + new_bytes
        if projected > self.context.limits.max_workspace_bytes:
            raise HarnessExecutionError(
                "WORKSPACE_QUOTA_EXCEEDED",
                "Operation would exceed the Harness workspace quota.",
                details={
                    "current_bytes": current,
                    "projected_bytes": projected,
                    "max_bytes": self.context.limits.max_workspace_bytes,
                },
            )

    def workspace_bytes(self) -> int:
        total = 0
        for directory, directory_names, file_names in os.walk(
            self.root,
            topdown=True,
            followlinks=False,
        ):
            base = Path(directory)
            directory_names[:] = [
                name
                for name in directory_names
                if name != _TRASH_DIRECTORY
                and not (base / name).is_symlink()
            ]
            for name in file_names:
                path = base / name
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
        return total

    def trash_target(self, relative: Path, trash_id: str) -> Path:
        trash_root = self.root / _TRASH_DIRECTORY
        self._reject_symlink_components(trash_root)
        try:
            trash_root.mkdir(exist_ok=True)
        except OSError as exc:
            raise _io_error("Harness trash could not be created.", exc) from exc
        self.require_directory(trash_root)

        target = trash_root / trash_id / relative
        self._reject_symlink_components(target)
        try:
            target.parent.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            self.require_directory(target.parent)
        except OSError as exc:
            raise _io_error("Harness trash destination could not be created.", exc) from exc
        self.require_directory(target.parent)
        return target

    def iter_files(self, start: Path) -> Iterator[Path]:
        metadata = start.lstat()
        if stat.S_ISREG(metadata.st_mode):
            yield start
            return
        self.require_directory(start)
        for directory, directory_names, file_names in os.walk(
            start,
            topdown=True,
            followlinks=False,
        ):
            base = Path(directory)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name != _TRASH_DIRECTORY and not (base / name).is_symlink()
            )
            for name in sorted(file_names):
                path = base / name
                try:
                    child_metadata = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(child_metadata.st_mode):
                    yield path

    def _reject_symlink_components(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
            current = self.root
        except ValueError:
            if not path.is_absolute():
                raise HarnessExecutionError(
                    "INVALID_PATH",
                    "Harness file-tool paths must resolve to an absolute host path.",
                )
            anchor = Path(path.anchor)
            current = anchor
            relative = Path(*path.parts[1:])
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                self._raise_symlink(current)

    def _raise_symlink(self, path: Path) -> None:
        raise HarnessExecutionError(
            "SYMLINK_NOT_ALLOWED",
            "Symbolic links are not allowed in Harness file-tool paths.",
            details={"path": self.relative(path)},
        )


def read_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, ReadFileArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    metadata = workspace.require_file(path)
    workspace.ensure_file_size(metadata.st_size)
    if args.offset > metadata.st_size:
        raise HarnessExecutionError(
            "INVALID_OFFSET",
            "Read offset is past the end of the file.",
            details={"offset": args.offset, "size": metadata.st_size},
        )

    safe_result_bytes = max(1, context.limits.max_result_bytes // 8)
    requested_bytes = args.max_bytes or min(context.limits.max_read_bytes, safe_result_bytes)
    if requested_bytes > context.limits.max_read_bytes:
        raise HarnessExecutionError(
            "READ_LIMIT_EXCEEDED",
            "Requested read exceeds the per-call read limit.",
            details={
                "requested_bytes": requested_bytes,
                "max_bytes": context.limits.max_read_bytes,
            },
        )
    read_bytes = min(requested_bytes, safe_result_bytes)
    try:
        with path.open("rb") as handle:
            handle.seek(args.offset)
            content_bytes = handle.read(read_bytes)
    except OSError as exc:
        raise _io_error("File could not be read.", exc) from exc

    content, consumed_bytes = _decode_utf8_chunk(content_bytes)
    next_offset = args.offset + consumed_bytes
    return {
        "path": workspace.relative(path),
        "content": content,
        "offset": args.offset,
        "next_offset": next_offset,
        "truncated": next_offset < metadata.st_size,
        "size": metadata.st_size,
        "sha256": _sha256(path),
    }


def extract_document_text(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    """Extract a supported document into a UTF-8 workspace file.

    Binary documents deliberately do not flow through ``read_file``.  The
    extracted text is persisted so the model can page through it with the
    existing bounded text reader instead of receiving an unbounded payload.
    """

    args = _as(arguments, ExtractDocumentTextArguments)
    workspace = _Workspace(context)
    source = workspace.resolve(args.path)
    source_metadata = workspace.require_file(source)
    workspace.ensure_file_size(source_metadata.st_size)
    try:
        source_bytes = source.read_bytes()
    except OSError as exc:
        raise _io_error("Document could not be read.", exc) from exc

    try:
        text, document_format = extract_text(source.name, source_bytes)
    except KnowledgeParseError as exc:
        raise HarnessExecutionError(
            "DOCUMENT_EXTRACTION_FAILED",
            str(exc),
            details={"path": workspace.relative(source)},
        ) from exc
    except Exception as exc:
        raise HarnessExecutionError(
            "DOCUMENT_EXTRACTION_FAILED",
            "Document content could not be extracted.",
            details={
                "path": workspace.relative(source),
                "exception_type": type(exc).__name__,
            },
        ) from exc

    output_raw = args.output_path or f"{workspace.relative(source)}.extracted.txt"
    output = workspace.resolve(output_raw)
    if output == source:
        raise HarnessExecutionError(
            "INVALID_PATH",
            "Extracted text output must differ from the source document.",
        )
    workspace.prepare_parent(output, create=True)
    content_bytes = text.encode("utf-8")
    workspace.ensure_file_size(len(content_bytes))
    previous_size = 0
    if output.exists():
        previous_size = workspace.require_file(output).st_size
    workspace.ensure_workspace_capacity(
        path=output,
        replacing_bytes=previous_size,
        new_bytes=len(content_bytes),
    )
    _atomic_write(output, content_bytes)
    return {
        "source_path": workspace.relative(source),
        "extracted_text_path": workspace.relative(output),
        "format": document_format,
        "characters": len(text),
        "size": len(content_bytes),
        "empty": not bool(text.strip()),
        "sha256": _sha256(output),
    }


def write_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, WriteFileArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    workspace.prepare_parent(path, create=args.create_parents)
    content_bytes = args.content.encode("utf-8")
    workspace.ensure_file_size(len(content_bytes))

    existed = path.exists()
    previous_size = 0
    previous_sha256: str | None = None
    if existed:
        metadata = workspace.require_file(path)
        previous_size = metadata.st_size
        previous_sha256 = workspace.assert_expected_hash(path, args.expected_sha256)
    elif args.expected_sha256:
        raise HarnessExecutionError(
            "HASH_MISMATCH",
            "Expected an existing file, but the destination does not exist.",
            retryable=True,
            details={"path": workspace.relative(path)},
        )

    workspace.ensure_workspace_capacity(
        path=path,
        replacing_bytes=previous_size,
        new_bytes=len(content_bytes),
    )
    _atomic_write(path, content_bytes)
    return {
        "path": workspace.relative(path),
        "created": not existed,
        "size": len(content_bytes),
        "sha256": _sha256(path),
        "previous_sha256": previous_sha256,
    }


def edit_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, EditFileArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    metadata = workspace.require_file(path)
    workspace.ensure_file_size(metadata.st_size)
    previous_sha256 = workspace.assert_expected_hash(path, args.expected_sha256)
    content = _read_utf8_file(path, context.limits.max_file_bytes)
    occurrences = content.count(args.old_text)
    if occurrences == 0:
        raise HarnessExecutionError(
            "EDIT_TARGET_NOT_FOUND",
            "old_text was not found in the target file.",
            details={"path": workspace.relative(path)},
        )
    if occurrences > 1 and not args.replace_all:
        raise HarnessExecutionError(
            "AMBIGUOUS_EDIT",
            "old_text occurs more than once; set replace_all or provide more context.",
            details={"path": workspace.relative(path), "occurrences": occurrences},
        )

    replacements = occurrences if args.replace_all else 1
    updated = content.replace(
        args.old_text,
        args.new_text,
        -1 if args.replace_all else 1,
    )
    updated_bytes = updated.encode("utf-8")
    workspace.ensure_file_size(len(updated_bytes))
    workspace.ensure_workspace_capacity(
        path=path,
        replacing_bytes=metadata.st_size,
        new_bytes=len(updated_bytes),
    )
    _atomic_write(path, updated_bytes)
    return {
        "path": workspace.relative(path),
        "replacements": replacements,
        "size": len(updated_bytes),
        "sha256": _sha256(path),
        "previous_sha256": previous_sha256,
    }


def list_directory(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, ListDirectoryArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path, allow_root=True)
    workspace.require_directory(path)
    limit = _entry_limit(args.max_entries, context)
    entries: list[dict[str, Any]] = []
    truncated = False

    if args.recursive:
        iterator = _iter_directory_entries(workspace, path)
    else:
        iterator = iter(sorted(path.iterdir(), key=lambda item: item.name))

    for child in iterator:
        if child.name == _TRASH_DIRECTORY and child.parent == workspace.root:
            continue
        if len(entries) >= limit:
            truncated = True
            break
        try:
            metadata = child.lstat()
        except FileNotFoundError:
            continue
        entries.append(_entry_payload(workspace, child, metadata))

    return {
        "path": workspace.relative(path),
        "entries": entries,
        "truncated": truncated,
    }


def glob_files(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, GlobArguments)
    workspace = _Workspace(context)
    start = workspace.resolve(args.path, allow_root=True)
    workspace.require_directory(start)
    pattern = _normalize_glob(args.pattern)
    limit = _entry_limit(args.max_results, context)
    matches: list[dict[str, Any]] = []
    truncated = False

    for child in _iter_directory_entries(workspace, start):
        relative_to_start = child.relative_to(start).as_posix()
        if not _glob_matches(relative_to_start, pattern):
            continue
        if len(matches) >= limit:
            truncated = True
            break
        try:
            metadata = child.lstat()
        except FileNotFoundError:
            continue
        matches.append(_entry_payload(workspace, child, metadata))

    return {
        "path": workspace.relative(start),
        "pattern": pattern,
        "matches": matches,
        "truncated": truncated,
    }


def grep_files(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, GrepArguments)
    workspace = _Workspace(context)
    start = workspace.resolve(args.path, allow_root=True)
    if not start.exists():
        raise HarnessExecutionError(
            "NOT_FOUND",
            "Search path does not exist.",
            details={"path": workspace.relative(start)},
        )
    workspace._reject_symlink_components(start)
    file_pattern = _normalize_glob(args.file_glob) if args.file_glob else None
    matcher = _grep_matcher(args.pattern, regex=args.regex, case_sensitive=args.case_sensitive)
    limit = min(
        _entry_limit(args.max_results, context),
        max(1, context.limits.max_result_bytes // 1024),
    )
    matches: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_binary_files = 0
    truncated_files = 0
    truncated = False

    for path in workspace.iter_files(start):
        relative_to_start = path.name if start.is_file() else path.relative_to(start).as_posix()
        if file_pattern and not _glob_matches(relative_to_start, file_pattern):
            continue
        workspace.require_file(path)
        scanned_files += 1
        try:
            with path.open("rb") as handle:
                raw = handle.read(context.limits.max_read_bytes + 1)
        except OSError as exc:
            raise _io_error("File could not be searched.", exc) from exc
        if len(raw) > context.limits.max_read_bytes:
            raw = raw[: context.limits.max_read_bytes]
            truncated_files += 1
        try:
            content, _ = _decode_utf8_chunk(raw)
        except HarnessExecutionError:
            skipped_binary_files += 1
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            column = matcher(line)
            if column is None:
                continue
            if len(matches) >= limit:
                truncated = True
                break
            display_line = line[:_MAX_GREP_LINE_CHARS]
            matches.append(
                {
                    "path": workspace.relative(path),
                    "line": line_number,
                    "column": column + 1,
                    "text": display_line,
                    "text_truncated": len(display_line) < len(line),
                }
            )
        if truncated:
            break

    return {
        "path": workspace.relative(start),
        "pattern": args.pattern,
        "matches": matches,
        "scanned_files": scanned_files,
        "skipped_binary_files": skipped_binary_files,
        "truncated_files": truncated_files,
        "truncated": truncated,
    }


def file_info(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, FileInfoArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    workspace._reject_symlink_components(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise HarnessExecutionError(
            "NOT_FOUND",
            "Path does not exist.",
            details={"path": workspace.relative(path)},
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        workspace._raise_symlink(path)
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise HarnessExecutionError(
            "UNSUPPORTED_FILE_TYPE",
            "Path is not a regular file or directory.",
            details={"path": workspace.relative(path)},
        )
    is_file = stat.S_ISREG(metadata.st_mode)
    return {
        "path": workspace.relative(path),
        "type": "file" if is_file else "directory",
        "size": metadata.st_size if is_file else None,
        "sha256": _sha256(path) if is_file else None,
        "modified_at": datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
    }


def publish_artifact(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    """Explicitly expose one verified final workspace file to the user."""

    args = _as(arguments, PublishArtifactArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    metadata = workspace.require_file(path)
    workspace.ensure_file_size(metadata.st_size)
    if not workspace.is_inside_workspace(path):
        staged = workspace.root / "published" / f"{uuid.uuid4().hex}-{path.name}"
        workspace.prepare_parent(staged, create=True)
        workspace.ensure_workspace_capacity(
            path=staged,
            replacing_bytes=0,
            new_bytes=metadata.st_size,
        )
        try:
            shutil.copyfile(path, staged)
        except OSError as exc:
            raise _io_error("External artifact could not be staged for download.", exc) from exc
        path = staged
        metadata = workspace.require_file(path)
    display_name = _safe_artifact_text(args.display_name, 180) or path.name
    description = _safe_artifact_text(args.description, 500)
    return {
        "path": workspace.relative(path),
        "display_name": display_name,
        "description": description,
        "size": metadata.st_size,
        "sha256": _sha256(path),
        "content_type": (
            mimetypes.guess_type(display_name)[0]
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        ),
    }


def make_directory(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, MakeDirectoryArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    existed = path.exists()
    if existed:
        workspace.require_directory(path)
        if not args.exist_ok:
            raise HarnessExecutionError(
                "ALREADY_EXISTS",
                "Directory already exists.",
                details={"path": workspace.relative(path)},
            )
    else:
        parent = path.parent
        if not args.parents:
            workspace.require_directory(parent)
        else:
            workspace._reject_symlink_components(parent)
        try:
            path.mkdir(parents=args.parents, exist_ok=args.exist_ok)
        except OSError as exc:
            raise _io_error("Directory could not be created.", exc) from exc
        workspace.require_directory(path)
    return {
        "path": workspace.relative(path),
        "created": not existed,
    }


def delete_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, DeleteFileArguments)
    workspace = _Workspace(context)
    path = workspace.resolve(args.path)
    metadata = workspace.require_file(path)
    sha256 = workspace.assert_expected_hash(path, args.expected_sha256)
    trash_id = uuid.uuid4().hex
    display_path = workspace.relative(path)
    trash_relative = (
        path.relative_to(workspace.root)
        if workspace.is_inside_workspace(path)
        else Path(path.name)
    )
    trash_target = workspace.trash_target(trash_relative, trash_id)
    try:
        os.replace(path, trash_target)
    except OSError:
        try:
            shutil.move(str(path), str(trash_target))
        except OSError as move_exc:
            raise _io_error("File could not be moved to Harness trash.", move_exc) from move_exc
    return {
        "path": display_path,
        "deleted": True,
        "recoverable": True,
        "trash_id": trash_id,
        "size": metadata.st_size,
        "sha256": sha256,
    }


def move_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, MoveFileArguments)
    workspace = _Workspace(context)
    source = workspace.resolve(args.source_path)
    destination = workspace.resolve(args.destination_path)
    _different_paths(source, destination)
    source_metadata = workspace.require_file(source)
    source_sha256 = workspace.assert_expected_hash(source, args.expected_sha256)
    workspace.prepare_parent(destination, create=args.create_parents)
    destination_previous_sha256 = _prepare_destination(
        workspace,
        destination,
        overwrite=args.overwrite,
        expected_sha256=args.expected_destination_sha256,
    )
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise _io_error("File could not be moved.", exc) from exc
    return {
        "source_path": workspace.relative(source),
        "destination_path": workspace.relative(destination),
        "size": source_metadata.st_size,
        "sha256": source_sha256,
        "destination_previous_sha256": destination_previous_sha256,
    }


def copy_file(
    context: HarnessToolContext,
    arguments: BaseModel,
) -> dict[str, Any]:
    args = _as(arguments, CopyFileArguments)
    workspace = _Workspace(context)
    source = workspace.resolve(args.source_path)
    destination = workspace.resolve(args.destination_path)
    _different_paths(source, destination)
    source_metadata = workspace.require_file(source)
    workspace.ensure_file_size(source_metadata.st_size)
    source_sha256 = workspace.assert_expected_hash(source, args.expected_sha256)
    workspace.prepare_parent(destination, create=args.create_parents)
    destination_previous_sha256 = _prepare_destination(
        workspace,
        destination,
        overwrite=args.overwrite,
        expected_sha256=args.expected_destination_sha256,
    )
    destination_size = destination.stat().st_size if destination.exists() else 0
    workspace.ensure_workspace_capacity(
        path=destination,
        replacing_bytes=destination_size,
        new_bytes=source_metadata.st_size,
    )
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise _io_error("Source file could not be read.", exc) from exc
    _atomic_write(destination, content)
    return {
        "source_path": workspace.relative(source),
        "destination_path": workspace.relative(destination),
        "size": source_metadata.st_size,
        "sha256": source_sha256,
        "destination_previous_sha256": destination_previous_sha256,
    }


def register_file_tools(registry: HarnessRegistry) -> HarnessRegistry:
    """Register the built-in typed filesystem tools on ``registry``."""

    path_contract = (
        "Relative paths start in the current TaskFrame workspace; absolute paths and "
        "parent-directory paths are accepted subject to host permissions and any enabled OS sandbox. "
    )

    registry.register(
        name="read_file",
        description=(
            path_contract
            + "Read UTF-8 text from a file. "
            "Use the returned byte offset to continue a truncated read."
        ),
        argument_model=ReadFileArguments,
        handler=read_file,
        side_effect="read",
    )
    registry.register(
        name="extract_document_text",
        description=(
            path_contract
            + "Extract PDF, DOCX, HTML, Markdown, or plain-text content into a UTF-8 "
            "file, then use read_file to inspect the extracted text. Use this "
            "instead of read_file for binary documents such as PDF and DOCX."
        ),
        argument_model=ExtractDocumentTextArguments,
        handler=extract_document_text,
        side_effect="write",
    )
    registry.register(
        name="write_file",
        description=path_contract + "Atomically create or replace a UTF-8 file.",
        argument_model=WriteFileArguments,
        handler=write_file,
        side_effect="write",
    )
    registry.register(
        name="edit_file",
        description=path_contract + "Atomically replace exact text in a UTF-8 file.",
        argument_model=EditFileArguments,
        handler=edit_file,
        side_effect="write",
    )
    registry.register(
        name="list_directory",
        description=path_contract + "List files and directories without following symbolic links.",
        argument_model=ListDirectoryArguments,
        handler=list_directory,
        side_effect="read",
    )
    registry.register(
        name="glob",
        description=path_contract + "Find paths under a start directory matching a glob pattern.",
        argument_model=GlobArguments,
        handler=glob_files,
        side_effect="read",
    )
    registry.register(
        name="grep",
        description=path_contract + "Search UTF-8 files using literal text.",
        argument_model=GrepArguments,
        handler=grep_files,
        side_effect="read",
    )
    registry.register(
        name="file_info",
        description=path_contract + "Return type, size, SHA-256, and modification time.",
        argument_model=FileInfoArguments,
        handler=file_info,
        side_effect="read",
    )
    registry.register(
        name="publish_artifact",
        description=(
            path_contract
            + "Explicitly publish one verified final file for user download; external files "
            "are copied into the TaskFrame artifact area. "
            "Use only for final deliverables, never for inputs, caches, logs, temporary "
            "files, runner code, or build intermediates."
        ),
        argument_model=PublishArtifactArguments,
        handler=publish_artifact,
        side_effect="write",
    )
    registry.register(
        name="mkdir",
        description=path_contract + "Create a directory.",
        argument_model=MakeDirectoryArguments,
        handler=make_directory,
        side_effect="write",
    )
    registry.register(
        name="delete_file",
        description=(
            path_contract
            + "Soft-delete a file into private Harness trash so it remains recoverable."
        ),
        argument_model=DeleteFileArguments,
        handler=delete_file,
        side_effect="delete",
    )
    registry.register(
        name="move_file",
        description=path_contract + "Move a regular file.",
        argument_model=MoveFileArguments,
        handler=move_file,
        side_effect="write",
    )
    registry.register(
        name="copy_file",
        description=path_contract + "Atomically copy a regular file.",
        argument_model=CopyFileArguments,
        handler=copy_file,
        side_effect="write",
    )
    return registry


def build_file_tool_registry() -> HarnessRegistry:
    return register_file_tools(HarnessRegistry())


def _as(arguments: BaseModel, expected: type[BaseModel]) -> Any:
    if not isinstance(arguments, expected):
        raise HarnessExecutionError(
            "INVALID_ARGUMENTS",
            f"Handler expected {expected.__name__}.",
        )
    return arguments


def _normalize_access_path(raw_path: str, *, root: Path, allow_root: bool) -> Path:
    if "\x00" in raw_path:
        raise HarnessExecutionError("INVALID_PATH", "Path cannot contain a null byte.")
    if not raw_path.strip():
        raise HarnessExecutionError("INVALID_PATH", "Path cannot be empty.")
    portable = raw_path.replace("\\", "/")
    normalized_alias = _strip_sandbox_workspace_prefix(portable)
    normalized_raw = normalized_alias if normalized_alias != portable else raw_path
    try:
        candidate = Path(normalized_raw).expanduser()
    except (KeyError, RuntimeError) as exc:
        raise HarnessExecutionError(
            "INVALID_PATH",
            "Home-relative path could not be expanded on this host.",
        ) from exc
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(os.path.normpath(candidate)))
    if candidate == root and not allow_root:
        raise HarnessExecutionError(
            "INVALID_PATH",
            "A file or child-directory path is required.",
        )
    if not candidate.parts:
        if allow_root:
            return candidate
        raise HarnessExecutionError("INVALID_PATH", "A file or child-directory path is required.")
    return candidate


def _normalize_glob(raw_pattern: str | None) -> str:
    if raw_pattern is None or not raw_pattern.strip() or "\x00" in raw_pattern:
        raise HarnessExecutionError("INVALID_PATTERN", "Glob pattern cannot be empty.")
    if PureWindowsPath(raw_pattern).drive:
        raise HarnessExecutionError("INVALID_PATTERN", "Absolute glob patterns are denied.")
    pattern = _strip_sandbox_workspace_prefix(raw_pattern.replace("\\", "/"))
    parsed = PurePosixPath(pattern)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise HarnessExecutionError("INVALID_PATTERN", "Glob traversal outside the workspace is denied.")
    if _TRASH_DIRECTORY in parsed.parts:
        raise HarnessExecutionError("RESERVED_PATH", "Harness trash cannot be searched.")
    return pattern


def _strip_sandbox_workspace_prefix(raw_path: str) -> str:
    if raw_path == SANDBOX_WORKSPACE:
        return "."
    prefix = f"{SANDBOX_WORKSPACE}/"
    if raw_path.startswith(prefix):
        return raw_path[len(prefix) :]
    return raw_path


def _glob_matches(relative_path: str, pattern: str) -> bool:
    path = PurePosixPath(relative_path)
    if path.match(pattern):
        return True
    if pattern.startswith("**/") and path.match(pattern[3:]):
        return True
    return fnmatch.fnmatchcase(relative_path, pattern)


def _iter_directory_entries(workspace: _Workspace, start: Path) -> Iterator[Path]:
    for directory, directory_names, file_names in os.walk(
        start,
        topdown=True,
        followlinks=False,
    ):
        base = Path(directory)
        allowed_directories: list[str] = []
        for name in sorted(directory_names):
            child = base / name
            if name == _TRASH_DIRECTORY and child == workspace.root / _TRASH_DIRECTORY:
                continue
            if child.is_symlink():
                yield child
                continue
            allowed_directories.append(name)
            yield child
        directory_names[:] = allowed_directories
        for name in sorted(file_names):
            yield base / name


def _entry_payload(
    workspace: _Workspace,
    path: Path,
    metadata: os.stat_result,
) -> dict[str, Any]:
    if stat.S_ISLNK(metadata.st_mode):
        kind = "symlink"
        size: int | None = None
    elif stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
        size = None
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
        size = metadata.st_size
    else:
        kind = "other"
        size = None
    return {
        "path": workspace.relative(path),
        "type": kind,
        "size": size,
    }


def _entry_limit(requested: int | None, context: HarnessToolContext) -> int:
    return min(requested or context.limits.max_entries, context.limits.max_entries)


def _grep_matcher(
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> Any:
    if regex:
        raise HarnessExecutionError(
            "REGEX_NOT_SUPPORTED",
            "Harness grep currently supports literal matching only.",
        )

    needle = pattern if case_sensitive else pattern.casefold()

    def literal_match(line: str) -> int | None:
        haystack = line if case_sensitive else line.casefold()
        column = haystack.find(needle)
        return column if column >= 0 else None

    return literal_match


def _decode_utf8_chunk(raw: bytes) -> tuple[str, int]:
    try:
        return raw.decode("utf-8"), len(raw)
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(raw):
            complete = raw[: exc.start]
            if not complete and raw:
                raise HarnessExecutionError(
                    "READ_CHUNK_TOO_SMALL",
                    "Read chunk ends inside the first UTF-8 character; request more bytes.",
                ) from exc
            return complete.decode("utf-8"), len(complete)
        raise HarnessExecutionError(
            "UNSUPPORTED_ENCODING",
            "File is not valid UTF-8 text.",
        ) from exc


def _read_utf8_file(path: Path, max_bytes: int) -> str:
    try:
        metadata = path.stat()
        if metadata.st_size > max_bytes:
            raise HarnessExecutionError(
                "FILE_TOO_LARGE",
                "File exceeds the Harness per-file size limit.",
                details={"actual_bytes": metadata.st_size, "max_bytes": max_bytes},
            )
        return path.read_text(encoding="utf-8")
    except HarnessExecutionError:
        raise
    except UnicodeDecodeError as exc:
        raise HarnessExecutionError(
            "UNSUPPORTED_ENCODING",
            "File is not valid UTF-8 text.",
        ) from exc
    except OSError as exc:
        raise _io_error("File could not be read.", exc) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _io_error("File hash could not be calculated.", exc) from exc
    return digest.hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise _io_error("File could not be written atomically.", exc) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _prepare_destination(
    workspace: _Workspace,
    destination: Path,
    *,
    overwrite: bool,
    expected_sha256: str | None,
) -> str | None:
    if not destination.exists():
        if expected_sha256:
            raise HarnessExecutionError(
                "HASH_MISMATCH",
                "Expected an existing destination, but it does not exist.",
                retryable=True,
                details={"path": workspace.relative(destination)},
            )
        return None
    workspace.require_file(destination)
    actual = workspace.assert_expected_hash(destination, expected_sha256)
    if not overwrite:
        raise HarnessExecutionError(
            "ALREADY_EXISTS",
            "Destination already exists; set overwrite to replace it.",
            details={"path": workspace.relative(destination), "sha256": actual},
        )
    return actual


def _different_paths(source: Path, destination: Path) -> None:
    if source == destination:
        raise HarnessExecutionError(
            "INVALID_ARGUMENTS",
            "Source and destination must be different paths.",
        )


def _safe_artifact_text(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = "".join(
        character
        for character in str(value).strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:max_length] or None


def _io_error(message: str, exc: OSError) -> HarnessExecutionError:
    if isinstance(exc, PermissionError):
        code = "PERMISSION_DENIED"
    elif isinstance(exc, FileExistsError):
        code = "ALREADY_EXISTS"
    elif isinstance(exc, FileNotFoundError):
        code = "NOT_FOUND"
    else:
        code = "IO_ERROR"
    return HarnessExecutionError(
        code,
        message,
        retryable=isinstance(exc, (BlockingIOError, TimeoutError)),
        details={"exception_type": type(exc).__name__},
    )


__all__ = [
    "CopyFileArguments",
    "DeleteFileArguments",
    "EditFileArguments",
    "ExtractDocumentTextArguments",
    "FileInfoArguments",
    "GlobArguments",
    "GrepArguments",
    "ListDirectoryArguments",
    "MakeDirectoryArguments",
    "MoveFileArguments",
    "PublishArtifactArguments",
    "ReadFileArguments",
    "WriteFileArguments",
    "build_file_tool_registry",
    "copy_file",
    "delete_file",
    "edit_file",
    "extract_document_text",
    "file_info",
    "glob_files",
    "grep_files",
    "list_directory",
    "make_directory",
    "move_file",
    "publish_artifact",
    "read_file",
    "register_file_tools",
    "write_file",
]
