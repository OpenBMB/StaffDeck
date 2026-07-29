from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound
from app.channels.service_durable_inbox import StageDisposition, StageResult
from app.db.models import ChannelBinding, ChannelInboundEvent, new_id, utc_now

DINGTALK_ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024


def dingtalk_account_key(client_id: str) -> str:
    client_id = client_id.strip()
    return f"dingtalk:app:{len(client_id)}:{client_id}"


def dingtalk_identity_scope(client_id: str, tenant_key: str) -> str:
    return f"app:{len(client_id)}:{client_id}:tenant:{len(tenant_key)}:{tenant_key}"


def encode_replay_envelope(inbound: ChannelInbound, *, client_id: str, tenant_key: str) -> dict[str, Any]:
    return {
        "schema_version": DINGTALK_ENVELOPE_VERSION,
        "account": {"client_id": client_id, "tenant_key": tenant_key},
        "inbound": asdict(inbound),
    }


def decode_replay_envelope(payload: object) -> ChannelInbound:
    if not isinstance(payload, dict) or payload.get("schema_version") != DINGTALK_ENVELOPE_VERSION:
        raise ValueError("unsupported_envelope_version")
    normalized = payload.get("inbound")
    if not isinstance(normalized, dict):
        raise ValueError("invalid_envelope_inbound")
    allowed = set(ChannelInbound.__dataclass_fields__)
    if not set(normalized) <= allowed:
        raise ValueError("invalid_envelope_fields")
    inbound = ChannelInbound(**normalized)
    if inbound.channel != "dingtalk":
        raise ValueError("invalid_envelope_channel")
    return inbound


def stage_dingtalk_inbound(
    *,
    db_engine,
    binding_id: str,
    expected_revision: int,
    client_id: str,
    tenant_key: str,
    inbound: ChannelInbound,
) -> StageResult:
    client_id, tenant_key = client_id.strip(), tenant_key.strip()
    if inbound.channel != "dingtalk" or not inbound.event_id or not client_id or not tenant_key:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_identity")
    envelope = encode_replay_envelope(inbound, client_id=client_id, tenant_key=tenant_key)
    try:
        if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_ENVELOPE_BYTES:
            return StageResult(StageDisposition.SECURITY_DROP, error_code="event_payload_too_large")
        with Session(db_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            expected_account = dingtalk_account_key(client_id)
            expected_scope = dingtalk_identity_scope(client_id, tenant_key)
            if (
                not binding
                or binding.channel != "dingtalk"
                or binding.status != "active"
                or binding.config_revision != expected_revision
                or binding.external_account_key != expected_account
                or str((binding.config_json or {}).get("client_id") or "").strip() != client_id
            ):
                return StageResult(StageDisposition.SECURITY_DROP, error_code="binding_fence_mismatch")
            if binding.provider_tenant_key is None:
                db.exec(
                    update(ChannelBinding)
                    .where(
                        ChannelBinding.id == binding_id,
                        or_(
                            ChannelBinding.provider_tenant_key.is_(None),
                            ChannelBinding.provider_tenant_key == "",
                        ),
                    )
                    .values(provider_tenant_key=tenant_key, updated_at=utc_now())
                )
                db.flush()
                db.refresh(binding)
            if binding.provider_tenant_key != tenant_key:
                db.rollback()
                return StageResult(StageDisposition.SECURITY_DROP, error_code="provider_tenant_mismatch")
            if not binding.identity_scope_key:
                db.exec(
                    update(ChannelBinding)
                    .where(
                        ChannelBinding.id == binding_id,
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
                return StageResult(StageDisposition.SECURITY_DROP, error_code="identity_scope_mismatch")
            target = {
                # 兼容现有 intake/outbox 的通用目标校验；provider-specific 字段同时保留。
                "to_user_id": inbound.conv_key if inbound.is_group else inbound.from_user_id,
                "context_token": inbound.context_token,
                "session_webhook": inbound.context_token,
                "session_webhook_expired_time": (inbound.raw or {}).get("sessionWebhookExpiredTime"),
                "conversation_id": inbound.session_id,
                "conversation_type": (inbound.raw or {}).get("conversationType"),
                "message_id": inbound.event_id,
            }
            event = ChannelInboundEvent(
                id=new_id("chevt"), tenant_id=binding.tenant_id, binding_id=binding.id,
                channel="dingtalk", event_id=inbound.event_id, payload_json=envelope,
                config_revision=expected_revision, target_json=target, status="received",
            )
            db.add(event)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.exec(select(ChannelInboundEvent).where(
                    ChannelInboundEvent.binding_id == binding_id,
                    ChannelInboundEvent.event_id == inbound.event_id,
                )).first()
                if existing:
                    return StageResult(StageDisposition.DUPLICATE, event_pk=existing.id)
                return StageResult(StageDisposition.NACK, error_code="inbox_integrity_error")
            return StageResult(StageDisposition.STAGED, event_pk=event.id)
    except SQLAlchemyError:
        return StageResult(StageDisposition.NACK, error_code="inbox_database_error")
