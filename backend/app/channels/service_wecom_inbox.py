from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment
from app.channels.service_durable_inbox import StageDisposition, StageResult
from app.db.models import ChannelBinding, ChannelInboundEvent, new_id

WECOM_ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024


def encode_wecom_replay_envelope(
    inbound: ChannelInbound,
    *,
    account_scope: str,
) -> dict[str, Any]:
    return {
        "schema_version": WECOM_ENVELOPE_VERSION,
        "account": {"scope": account_scope.strip()},
        "inbound": asdict(inbound),
    }


def decode_wecom_replay_envelope(payload: object) -> ChannelInbound:
    if not isinstance(payload, dict) or payload.get("schema_version") != WECOM_ENVELOPE_VERSION:
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
        inbound = ChannelInbound(**copy)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_envelope_inbound") from exc
    if inbound.channel != "wecom":
        raise ValueError("invalid_envelope_channel")
    return inbound


def stage_wecom_inbound(
    *,
    db_engine,
    binding_id: str,
    expected_revision: int,
    account_scope: str,
    inbound: ChannelInbound,
) -> StageResult:
    """Persist a normalized WeCom message before returning from the SDK callback."""
    account_scope = account_scope.strip()
    if inbound.channel != "wecom" or not inbound.event_id or not account_scope:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_identity")
    envelope = encode_wecom_replay_envelope(inbound, account_scope=account_scope)
    try:
        encoded_size = len(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_payload")
    if encoded_size > MAX_ENVELOPE_BYTES:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="event_payload_too_large")

    try:
        with Session(db_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            from app.channels.service_identity import external_account_scope

            current_scope = external_account_scope(None, binding) if binding else ""
            if (
                not binding
                or binding.channel != "wecom"
                or binding.status != "active"
                or binding.config_revision != expected_revision
                or current_scope != account_scope
            ):
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="binding_fence_mismatch",
                )
            target = {
                "to_user_id": inbound.conv_key if inbound.is_group else inbound.from_user_id,
                "context_token": inbound.context_token,
            }
            event = ChannelInboundEvent(
                id=new_id("chevt"),
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                channel="wecom",
                event_id=inbound.event_id,
                payload_json=envelope,
                config_revision=expected_revision,
                target_json=target,
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
