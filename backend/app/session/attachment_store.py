from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePath
from typing import Any

from app import paths
from app.core.harness_session_cleanup import harness_path_segment
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.session.session_schema import ChatAttachmentRead


def sandbox_attachment_path(attachment: ChatAttachmentRead, index: int = 1) -> str:
    basename = PurePath(attachment.filename or f"attachment-{index}").name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip(".-")
    safe_name = safe_name[:96] or f"attachment-{index}.bin"
    relative = (
        "attachments/"
        f"{harness_path_segment(attachment.id)[:32]}-{safe_name}"
    )
    return f"{SANDBOX_WORKSPACE}/{relative}"


def stage_chat_attachment(
    attachment: ChatAttachmentRead,
    data: bytes,
    *,
    tenant_id: str,
    user_id: str,
) -> ChatAttachmentRead:
    """Persist upload bytes outside TaskFrame workspaces without exposing the host path."""

    directory = _attachment_directory(
        tenant_id=tenant_id,
        user_id=user_id,
        attachment_id=attachment.id,
    )
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise OSError("attachment staging directory cannot be a symbolic link")
    digest = hashlib.sha256(data).hexdigest()
    _atomic_write(directory / "payload", data)
    metadata = {
        "attachment_id": attachment.id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": len(data),
        "sha256": digest,
    }
    _atomic_write(
        directory / "metadata.json",
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    return attachment.model_copy(
        update={
            "sha256": digest,
            "sandbox_path": sandbox_attachment_path(attachment),
        }
    )


def read_staged_chat_attachment(
    attachment: ChatAttachmentRead,
    *,
    tenant_id: str,
    user_id: str,
) -> bytes | None:
    directory = _attachment_directory(
        tenant_id=tenant_id,
        user_id=user_id,
        attachment_id=attachment.id,
    )
    payload_path = directory / "payload"
    metadata_path = directory / "metadata.json"
    try:
        if directory.is_symlink() or payload_path.is_symlink() or metadata_path.is_symlink():
            return None
        metadata: dict[str, Any] = json.loads(metadata_path.read_text(encoding="utf-8"))
        data = payload_path.read_bytes()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    digest = hashlib.sha256(data).hexdigest()
    expected_sha = str(attachment.sha256 or "").lower()
    if (
        metadata.get("attachment_id") != attachment.id
        or metadata.get("filename") != attachment.filename
        or metadata.get("content_type") != attachment.content_type
        or int(metadata.get("size") or -1) != attachment.size
        or str(metadata.get("sha256") or "").lower() != digest
        or expected_sha != digest
    ):
        return None
    return data


def _attachment_directory(*, tenant_id: str, user_id: str, attachment_id: str) -> Path:
    root = paths.user_data_dir().resolve() / "harness_uploads"
    return (
        root
        / harness_path_segment(tenant_id)
        / harness_path_segment(user_id)
        / harness_path_segment(attachment_id)
    )


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


__all__ = [
    "read_staged_chat_attachment",
    "sandbox_attachment_path",
    "stage_chat_attachment",
]
