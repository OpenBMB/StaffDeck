from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/rps", tags=["rps-downloads"])

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_STORAGE_ENV = "STAFFDECK_RPS_STORAGE_DIR"


def rps_storage_dir() -> Path:
    override = os.environ.get(_STORAGE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "runtime" / "rps-storage"


@router.get("/download/{token}")
def download_signed_rps_package(token: str) -> FileResponse:
    """Serve one short-lived RPS package referenced by an fsstore signed URL."""

    if not _TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=404, detail="Download link not found")

    storage_root = rps_storage_dir().resolve()
    try:
        signed_root = (storage_root / ".signed").resolve(strict=True)
        signed_root.relative_to(storage_root)
        token_path = (signed_root / f"{token}.json").resolve(strict=True)
        token_path.relative_to(signed_root)
        if not token_path.is_file():
            raise FileNotFoundError(token_path)
        token_record = json.loads(token_path.read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        raise HTTPException(status_code=404, detail="Download link not found") from None
    if not isinstance(token_record, dict):
        raise HTTPException(status_code=404, detail="Download link not found")

    expires_at = token_record.get("expires_at")
    try:
        expires_at_timestamp = float(expires_at)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Download link not found") from None
    if not math.isfinite(expires_at_timestamp):
        raise HTTPException(status_code=404, detail="Download link not found")
    if expires_at_timestamp <= time.time():
        raise HTTPException(status_code=410, detail="Download link expired")

    key = token_record.get("key")
    if not isinstance(key, str):
        raise HTTPException(status_code=404, detail="Download link not found")
    relative_key = PurePosixPath(key.replace("\\", "/"))
    if relative_key.is_absolute() or relative_key.parts[:2] != ("rps", "packages"):
        raise HTTPException(status_code=404, detail="Download link not found")
    if any(part in {"", ".", ".."} for part in relative_key.parts):
        raise HTTPException(status_code=404, detail="Download link not found")

    try:
        package_path = (storage_root / Path(*relative_key.parts)).resolve(strict=True)
        package_path.relative_to(storage_root)
        if not package_path.is_file():
            raise HTTPException(status_code=404, detail="Download link not found")
    except (FileNotFoundError, OSError, ValueError):
        raise HTTPException(status_code=404, detail="Download link not found") from None

    filename = _safe_filename(package_path.name)
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "package.zip"
    media_type = (
        "application/zip"
        if Path(filename).suffix.lower() == ".zip"
        else mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )
    return FileResponse(
        package_path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="{fallback}"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _safe_filename(value: str) -> str:
    cleaned = "".join(
        character
        for character in value
        if character not in {"\r", "\n", "\x00"}
        and (character.isprintable() or character == "\t")
    ).strip()
    return cleaned[:180] or "package.zip"
