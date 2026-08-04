from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Any

from fastapi import Request
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import APIIdempotencyRecord, utc_now
from app.public_api.auth import PublicPrincipal
from app.public_api.errors import PublicAPIError


def request_fingerprint(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_idempotent_response(
    db: Session,
    principal: PublicPrincipal,
    request: Request,
    payload: Any,
) -> tuple[int, dict[str, Any]] | None:
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return None
    if len(key) > 200:
        raise PublicAPIError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key is too long.")
    if not principal.credential_id:
        return None
    row = db.exec(
        select(APIIdempotencyRecord).where(
            APIIdempotencyRecord.credential_id == principal.credential_id,
            APIIdempotencyRecord.method == request.method,
            APIIdempotencyRecord.path == request.url.path,
            APIIdempotencyRecord.idempotency_key == key,
        )
    ).first()
    if not row:
        return None
    if row.expires_at <= utc_now():
        db.delete(row)
        db.commit()
        return None
    if row.request_hash != request_fingerprint(payload):
        raise PublicAPIError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "The Idempotency-Key was already used with a different request.",
        )
    return row.status_code, dict(row.response_json or {})


def store_idempotent_response(
    db: Session,
    principal: PublicPrincipal,
    request: Request,
    payload: Any,
    response: dict[str, Any],
    *,
    status_code: int,
    resource_id: str | None = None,
) -> None:
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not key or not principal.credential_id:
        return
    row = APIIdempotencyRecord(
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        method=request.method,
        path=request.url.path,
        idempotency_key=key,
        request_hash=request_fingerprint(payload),
        response_json=response,
        status_code=status_code,
        resource_id=resource_id,
        expires_at=utc_now()
        + timedelta(seconds=get_settings().public_api_idempotency_ttl_seconds),
    )
    db.add(row)
    db.commit()
