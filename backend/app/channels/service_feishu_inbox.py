from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment
from app.channels.service_durable_inbox import StageDisposition, StageResult
from app.db.models import ChannelBinding, ChannelInboundEvent, new_id, utc_now

FEISHU_ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024


def feishu_account_key(app_id: str) -> str:
    app_id = app_id.strip()
    return f"feishu:app:{len(app_id)}:{app_id}"


def feishu_identity_scope(app_id: str, tenant_key: str) -> str:
    app_id = app_id.strip()
    tenant_key = tenant_key.strip()
    return f"app:{len(app_id)}:{app_id}:tenant:{len(tenant_key)}:{tenant_key}"


def encode_replay_envelope(
    inbound: ChannelInbound,
    *,
    app_id: str,
    tenant_key: str,
) -> dict[str, Any]:
    normalized = asdict(inbound)
    return {
        "schema_version": FEISHU_ENVELOPE_VERSION,
        "account": {"app_id": app_id, "tenant_key": tenant_key},
        "inbound": normalized,
    }


def decode_replay_envelope(payload: object) -> ChannelInbound:
    if not isinstance(payload, dict) or payload.get("schema_version") != FEISHU_ENVELOPE_VERSION:
        raise ValueError("unsupported_envelope_version")
    normalized = payload.get("inbound")
    if not isinstance(normalized, dict):
        raise ValueError("invalid_envelope_inbound")
    allowed = set(ChannelInbound.__dataclass_fields__)
    if not set(normalized) <= allowed:
        raise ValueError("invalid_envelope_fields")
    copy = dict(normalized)
    raw_attachments = copy.get("attachments") or []
    if raw_attachments:
        copy["attachments"] = [
            ChannelInboundAttachment(**att) if isinstance(att, dict) else att
            for att in raw_attachments
        ]
    try:
        return ChannelInbound(**copy)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_envelope_inbound") from exc


def stage_feishu_inbound(
    *,
    db_engine,
    binding_id: str,
    expected_revision: int,
    event_app_id: str,
    tenant_key: str,
    inbound: ChannelInbound,
    target: dict[str, Any],
) -> StageResult:
    """Persist a Feishu event and pin its provider tenant before wire ACK.

    Security mismatches are acknowledged and dropped. Persistence failures are NACKed
    so the provider can retry. Duplicate delivery never mutates the original row.
    """
    event_app_id = event_app_id.strip()
    tenant_key = tenant_key.strip()
    if (
        inbound.channel != "feishu"
        or not event_app_id
        or not tenant_key
        or not inbound.event_id
    ):
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_identity")

    envelope = encode_replay_envelope(
        inbound,
        app_id=event_app_id,
        tenant_key=tenant_key,
    )
    try:
        encoded_size = len(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_payload")
    if encoded_size > MAX_ENVELOPE_BYTES:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="event_payload_too_large")

    expected_account_key = feishu_account_key(event_app_id)
    expected_scope = feishu_identity_scope(event_app_id, tenant_key)
    try:
        with Session(db_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            if (
                not binding
                or binding.channel != "feishu"
                or binding.status != "active"
                or binding.config_revision != expected_revision
                or binding.external_account_key != expected_account_key
                or str((binding.config_json or {}).get("app_id") or "").strip()
                != event_app_id
            ):
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="binding_fence_mismatch",
                )

            if binding.provider_tenant_key is None:
                db.exec(
                    update(ChannelBinding)
                    .where(
                        ChannelBinding.id == binding_id,
                        ChannelBinding.channel == "feishu",
                        ChannelBinding.status == "active",
                        ChannelBinding.config_revision == expected_revision,
                        ChannelBinding.provider_tenant_key.is_(None),
                    )
                    .values(provider_tenant_key=tenant_key, updated_at=utc_now())
                )
                db.flush()
                db.refresh(binding)
            if binding.provider_tenant_key != tenant_key:
                db.rollback()
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="provider_tenant_mismatch",
                )

            if binding.identity_scope_key in (None, ""):
                db.exec(
                    update(ChannelBinding)
                    .where(
                        ChannelBinding.id == binding_id,
                        ChannelBinding.config_revision == expected_revision,
                        or_(
                            ChannelBinding.identity_scope_key.is_(None),
                            ChannelBinding.identity_scope_key == "",
                        ),
                    )
                    .values(identity_scope_key=expected_scope, updated_at=utc_now())
                )
                db.flush()
                db.refresh(binding)
            if binding.identity_scope_key != expected_scope:
                db.rollback()
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="identity_scope_mismatch",
                )

            event = ChannelInboundEvent(
                id=new_id("chevt"),
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                channel="feishu",
                event_id=inbound.event_id,
                payload_json=envelope,
                config_revision=expected_revision,
                target_json=dict(target),
                status="received",
            )
            db.add(event)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.exec(
                    select(ChannelInboundEvent).where(
                        ChannelInboundEvent.binding_id == binding_id,
                        ChannelInboundEvent.event_id == inbound.event_id,
                    )
                ).first()
                if existing:
                    return StageResult(StageDisposition.DUPLICATE, event_pk=existing.id)
                return StageResult(StageDisposition.NACK, error_code="inbox_integrity_error")
            return StageResult(StageDisposition.STAGED, event_pk=event.id)
    except SQLAlchemyError:
        return StageResult(StageDisposition.NACK, error_code="inbox_database_error")
