from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.async_jobs import enqueue_async_job
from app.config import get_settings
from app.db import engine, get_session
from app.db.models import (
    APICredential,
    WebhookDelivery,
    WebhookEndpoint,
    utc_now,
)
from app.public_api.auth import PublicPrincipal, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.schemas import WebhookCreate, WebhookRead
from app.security.encryption import decrypt_secret, encrypt_secret


router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _read(row: WebhookEndpoint) -> WebhookRead:
    return WebhookRead(
        id=row.id,
        name=row.name,
        url=row.url,
        events=list(row.events_json or []),
        status=row.status,
        secret_masked="whsec_********",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_webhook_url(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PublicAPIError(422, "WEBHOOK_URL_INVALID", "Webhook URL must be HTTP or HTTPS.")
    if parsed.username or parsed.password or parsed.fragment:
        raise PublicAPIError(422, "WEBHOOK_URL_INVALID", "Webhook URL contains forbidden parts.")
    return raw.strip()


@router.get("", response_model=list[WebhookRead])
def list_webhooks(
    principal: PublicPrincipal = Depends(require_scopes("webhooks:read")),
    db: Session = Depends(get_session),
) -> list[WebhookRead]:
    if not principal.client_id:
        return []
    rows = db.exec(
        select(WebhookEndpoint)
        .where(
            WebhookEndpoint.tenant_id == principal.tenant_id,
            WebhookEndpoint.client_id == principal.client_id,
        )
        .order_by(WebhookEndpoint.created_at.desc())
    ).all()
    return [_read(row) for row in rows]


@router.post("", response_model=dict, status_code=201)
def create_webhook(
    request: WebhookCreate,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> dict:
    if not principal.client_id or principal.agent_id:
        raise PublicAPIError(403, "TENANT_KEY_REQUIRED", "A tenant key is required.")
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    row = WebhookEndpoint(
        tenant_id=principal.tenant_id,
        client_id=principal.client_id,
        name=request.name.strip(),
        url=_validate_webhook_url(str(request.url)),
        secret_encrypted=encrypt_secret(secret),
        events_json=sorted(set(request.events)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {**_read(row).model_dump(mode="json"), "secret": secret}


@router.post("/{endpoint_id}:pause", response_model=WebhookRead)
def pause_webhook(
    endpoint_id: str,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> WebhookRead:
    row = _owned_endpoint(db, principal, endpoint_id)
    row.status = "paused"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row)


@router.patch("/{endpoint_id}", response_model=WebhookRead)
def update_webhook(
    endpoint_id: str,
    request: WebhookCreate,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> WebhookRead:
    row = _owned_endpoint(db, principal, endpoint_id)
    row.name = request.name.strip()
    row.url = _validate_webhook_url(str(request.url))
    row.events_json = sorted(set(request.events))
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row)


@router.post("/{endpoint_id}:resume", response_model=WebhookRead)
def resume_webhook(
    endpoint_id: str,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> WebhookRead:
    row = _owned_endpoint(db, principal, endpoint_id)
    row.status = "active"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row)


@router.post("/{endpoint_id}:test", response_model=dict, status_code=202)
def test_webhook(
    endpoint_id: str,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_endpoint(db, principal, endpoint_id)
    event_id = f"evt_test_{secrets.token_hex(12)}"
    delivery = WebhookDelivery(
        tenant_id=principal.tenant_id,
        endpoint_id=row.id,
        event_id=event_id,
        event_type="webhook.test",
        payload_json={
            "id": event_id,
            "type": "webhook.test",
            "created_at": utc_now().isoformat() + "Z",
            "data": {"endpoint_id": row.id},
        },
        next_attempt_at=utc_now(),
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    enqueue_webhook_deliveries([delivery.id])
    return {"delivery_id": delivery.id, "event_id": event_id, "status": delivery.status}


@router.delete("/{endpoint_id}", status_code=204)
def delete_webhook(
    endpoint_id: str,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:write")),
    db: Session = Depends(get_session),
) -> None:
    row = _owned_endpoint(db, principal, endpoint_id)
    db.delete(row)
    db.commit()


@router.get("/{endpoint_id}/deliveries", response_model=list[dict])
def list_webhook_deliveries(
    endpoint_id: str,
    principal: PublicPrincipal = Depends(require_scopes("webhooks:read")),
    db: Session = Depends(get_session),
) -> list[dict]:
    _owned_endpoint(db, principal, endpoint_id)
    rows = db.exec(
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": row.id,
            "event_id": row.event_id,
            "event_type": row.event_type,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "last_status_code": row.last_status_code,
            "last_error": row.last_error,
            "created_at": row.created_at,
            "delivered_at": row.delivered_at,
        }
        for row in rows
    ]


def _owned_endpoint(
    db: Session, principal: PublicPrincipal, endpoint_id: str
) -> WebhookEndpoint:
    row = db.get(WebhookEndpoint, endpoint_id)
    if (
        not row
        or row.tenant_id != principal.tenant_id
        or row.client_id != principal.client_id
    ):
        raise PublicAPIError(404, "WEBHOOK_NOT_FOUND", "Webhook endpoint not found.")
    return row


def event_matches(patterns: list[str], event_type: str) -> bool:
    for pattern in patterns:
        if pattern == "*" or pattern == event_type:
            return True
        if pattern.endswith(".*") and event_type.startswith(pattern[:-1]):
            return True
    return False


def stage_webhook_deliveries(
    db: Session,
    *,
    tenant_id: str,
    credential_id: str,
    event_id: str,
    event_type: str,
    payload: dict,
) -> list[str]:
    credential = db.get(APICredential, credential_id)
    if not credential:
        return []
    endpoints = db.exec(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.client_id == credential.client_id,
            WebhookEndpoint.status == "active",
        )
    ).all()
    delivery_ids: list[str] = []
    for endpoint in endpoints:
        if not event_matches(list(endpoint.events_json or []), event_type):
            continue
        delivery = WebhookDelivery(
            tenant_id=tenant_id,
            endpoint_id=endpoint.id,
            event_id=event_id,
            event_type=event_type,
            payload_json=payload,
            next_attempt_at=utc_now(),
        )
        db.add(delivery)
        db.flush()
        delivery_ids.append(delivery.id)
    return delivery_ids


def enqueue_webhook_deliveries(delivery_ids: list[str]) -> None:
    for delivery_id in delivery_ids:
        enqueue_async_job("public_api.webhook", deliver_webhook, delivery_id)


def deliver_webhook(delivery_id: str) -> None:
    with Session(engine) as db:
        owner = f"whlease_{secrets.token_hex(12)}"
        now = utc_now()
        claim = db.exec(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.status.in_(["queued", "retrying", "sending"]),
                or_(
                    WebhookDelivery.next_attempt_at.is_(None),
                    WebhookDelivery.next_attempt_at <= now,
                ),
                or_(
                    WebhookDelivery.lease_expires_at.is_(None),
                    WebhookDelivery.lease_expires_at <= now,
                ),
            )
            .values(
                status="sending",
                delivery_owner=owner,
                lease_expires_at=now
                + timedelta(seconds=get_settings().public_api_webhook_timeout_seconds + 30),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(claim, "rowcount", 0) != 1:
            db.rollback()
            return
        db.commit()
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
        if not endpoint or endpoint.status != "active":
            _finish_webhook_delivery(
                db,
                delivery,
                owner,
                {
                    "status": "abandoned",
                    "last_error": "Webhook endpoint is inactive",
                    "next_attempt_at": None,
                },
            )
            return
        body = json.dumps(delivery.payload_json, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(datetime_now_timestamp()))
        signature = hmac.new(
            decrypt_secret(endpoint.secret_encrypted).encode("utf-8"),
            f"{timestamp}.{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        delivery.attempt_count += 1
        try:
            response = httpx.post(
                endpoint.url,
                content=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-StaffDeck-Event-ID": delivery.event_id,
                    "X-StaffDeck-Timestamp": timestamp,
                    "X-StaffDeck-Signature": f"v1={signature}",
                },
                timeout=get_settings().public_api_webhook_timeout_seconds,
            )
            delivery.last_status_code = response.status_code
            if 200 <= response.status_code < 300:
                delivery.status = "delivered"
                delivery.delivered_at = utc_now()
                delivery.last_error = None
            else:
                _schedule_retry(delivery, f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - persisted delivery boundary.
            _schedule_retry(delivery, str(exc))
        _finish_webhook_delivery(
            db,
            delivery,
            owner,
            {
                "status": delivery.status,
                "attempt_count": delivery.attempt_count,
                "next_attempt_at": delivery.next_attempt_at,
                "last_status_code": delivery.last_status_code,
                "last_error": delivery.last_error,
                "delivered_at": delivery.delivered_at,
            },
        )


def _finish_webhook_delivery(
    db: Session,
    delivery: WebhookDelivery,
    owner: str,
    values: dict[str, object],
) -> bool:
    """Fence completion so an expired/duplicate worker cannot overwrite it."""

    result = db.exec(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == delivery.id,
            WebhookDelivery.status == "sending",
            WebhookDelivery.delivery_owner == owner,
        )
        .values(
            **values,
            delivery_owner=None,
            lease_expires_at=None,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _schedule_retry(delivery: WebhookDelivery, error: str) -> None:
    delivery.last_error = error[:1000]
    if delivery.attempt_count >= get_settings().public_api_webhook_max_attempts:
        delivery.status = "abandoned"
        delivery.next_attempt_at = None
        return
    delay_seconds = min(8 * 60 * 60, 60 * (5 ** max(0, delivery.attempt_count - 1)))
    delivery.status = "retrying"
    delivery.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)


def datetime_now_timestamp() -> int:
    return int(datetime.now(UTC).timestamp())


def enqueue_due_webhook_deliveries() -> None:
    with Session(engine) as db:
        rows = db.exec(
            select(WebhookDelivery).where(
                WebhookDelivery.status.in_(["queued", "retrying"]),  # type: ignore[attr-defined]
                WebhookDelivery.next_attempt_at <= utc_now(),
            )
        ).all()
    enqueue_webhook_deliveries([row.id for row in rows])
