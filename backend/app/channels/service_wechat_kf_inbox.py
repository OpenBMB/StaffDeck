from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment
from app.channels.service_durable_inbox import StageDisposition, StageResult
from app.db.models import ChannelBinding, ChannelInboundEvent, WeChatKfAccount, new_id

ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024


def encode_replay_envelope(inbound: ChannelInbound, *, account_scope: str) -> dict[str, Any]:
    return {
        "schema_version": ENVELOPE_VERSION,
        "account": {"scope": account_scope.strip()},
        "inbound": asdict(inbound),
    }


def decode_replay_envelope(payload: object) -> ChannelInbound:
    if not isinstance(payload, dict) or payload.get("schema_version") != ENVELOPE_VERSION:
        raise ValueError("unsupported_envelope_version")
    normalized = payload.get("inbound")
    allowed_fields = set(ChannelInbound.__dataclass_fields__)
    if not isinstance(normalized, dict) or not set(normalized) <= allowed_fields:
        raise ValueError("invalid_envelope_inbound")
    raw_attachments = normalized.get("attachments") or []
    if not isinstance(raw_attachments, list):
        raise ValueError("invalid_envelope_attachments")  # noqa: TRY004
    if any(not isinstance(attachment, dict) for attachment in raw_attachments):
        raise ValueError("invalid_envelope_attachments")
    try:
        normalized = dict(normalized)
        normalized["attachments"] = [
            ChannelInboundAttachment(**attachment) for attachment in raw_attachments
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_envelope_attachments") from exc
    try:
        inbound = ChannelInbound(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_envelope_inbound") from exc
    if inbound.channel != "wechat_kf":
        raise ValueError("invalid_envelope_channel")
    return inbound


def stage_wechat_kf_inbound(
    *,
    db_engine,
    binding_id: str,
    expected_revision: int,
    account_scope: str,
    inbound: ChannelInbound,
) -> StageResult:
    if inbound.channel != "wechat_kf" or not inbound.event_id or not account_scope:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_identity")
    envelope = encode_replay_envelope(inbound, account_scope=account_scope)
    if len(json.dumps(envelope, ensure_ascii=False).encode()) > MAX_ENVELOPE_BYTES:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="event_payload_too_large")
    try:
        with Session(db_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            from app.channels.service_identity import external_account_scope

            expected_scope = external_account_scope(None, binding) if binding else ""
            if binding and binding.channel == "wechat_kf":
                corp_id = str((binding.config_json or {}).get("corp_id") or "").strip()
                expected_scope = (
                    f"{corp_id}:{inbound.to_user_id}"
                    if corp_id and inbound.to_user_id
                    else expected_scope
                )
            if (
                not binding
                or binding.channel != "wechat_kf"
                or binding.status != "active"
                or binding.config_revision != expected_revision
                or expected_scope != account_scope
            ):
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="binding_fence_mismatch",
                )
            account = db.exec(
                select(WeChatKfAccount).where(
                    WeChatKfAccount.binding_id == binding.id,
                    WeChatKfAccount.open_kfid == inbound.to_user_id,
                    WeChatKfAccount.status == "active",
                )
            ).first()
            if not account:
                return StageResult(
                    StageDisposition.SECURITY_DROP,
                    error_code="account_fence_mismatch",
                )
            event = ChannelInboundEvent(
                id=new_id("chevt"),
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                channel="wechat_kf",
                event_id=inbound.event_id,
                payload_json=envelope,
                config_revision=expected_revision,
                target_json={
                    "to_user_id": inbound.from_user_id,
                    "open_kfid": inbound.to_user_id,
                },
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
