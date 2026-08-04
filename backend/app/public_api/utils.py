from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from fastapi import Request
from sqlmodel import Session

from app.db.models import APIAuditLog
from app.public_api.auth import PublicPrincipal


def etag_for(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f'"{hashlib.sha256(canonical.encode("utf-8")).hexdigest()}"'


def encode_cursor(created_at: Any, row_id: str) -> str:
    raw = json.dumps([str(created_at), row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if isinstance(value, list) and len(value) == 2:
            return str(value[0]), str(value[1])
    except Exception:
        return None
    return None


def audit_request(
    db: Session,
    request: Request,
    principal: PublicPrincipal | None,
    *,
    status_code: int,
    duration_ms: float,
) -> None:
    row = APIAuditLog(
        tenant_id=principal.tenant_id if principal else None,
        credential_id=principal.credential_id if principal else None,
        request_id=str(getattr(request.state, "request_id", "")),
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        duration_ms=duration_ms,
    )
    db.add(row)
    db.commit()
