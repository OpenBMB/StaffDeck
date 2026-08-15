from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.channels.adapters.base import ChannelInbound, evaluate_allowlist
from app.channels.service_durable_inbox import StageDisposition, StageResult
from app.db.models import ChannelBinding, ChannelInboundEvent, new_id

logger = logging.getLogger(__name__)

DISCORD_ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 256 * 1024


def discord_account_key(bot_id: str) -> str:
    bot_id = bot_id.strip()
    return f"discord:bot:{len(bot_id)}:{bot_id}"


def encode_replay_envelope(inbound: ChannelInbound, *, bot_id: str) -> dict[str, Any]:
    return {
        "schema_version": DISCORD_ENVELOPE_VERSION,
        "account": {"bot_id": bot_id},
        "inbound": asdict(inbound),
    }


def decode_replay_envelope(payload: object) -> ChannelInbound:
    if not isinstance(payload, dict) or payload.get("schema_version") != DISCORD_ENVELOPE_VERSION:
        raise ValueError("unsupported_envelope_version")
    normalized = payload.get("inbound")
    if not isinstance(normalized, dict):
        raise ValueError("invalid_envelope_inbound")
    allowed = set(ChannelInbound.__dataclass_fields__)
    if not set(normalized) <= allowed:
        raise ValueError("invalid_envelope_fields")
    inbound = ChannelInbound(**normalized)
    if inbound.channel != "discord":
        raise ValueError("invalid_envelope_channel")
    return inbound


def stage_discord_inbound(
    *,
    db_engine,
    binding_id: str,
    expected_revision: int,
    bot_id: str,
    inbound: ChannelInbound,
) -> StageResult:
    bot_id = bot_id.strip()
    if inbound.channel != "discord" or not inbound.event_id or not bot_id:
        return StageResult(StageDisposition.SECURITY_DROP, error_code="invalid_event_identity")
    envelope = encode_replay_envelope(inbound, bot_id=bot_id)
    try:
        if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_ENVELOPE_BYTES:
            return StageResult(StageDisposition.SECURITY_DROP, error_code="event_payload_too_large")
        with Session(db_engine) as db:
            binding = db.get(ChannelBinding, binding_id)
            expected_account = discord_account_key(bot_id)
            if (
                not binding
                or binding.channel != "discord"
                or binding.status != "active"
                or binding.config_revision != expected_revision
                or binding.external_account_key != expected_account
                or str((binding.config_json or {}).get("bot_id") or "").strip() != bot_id
            ):
                return StageResult(StageDisposition.SECURITY_DROP, error_code="binding_fence_mismatch")
            # Discord 没有企业租户维度,identity_scope 为空,无需修补。
            raw = inbound.raw if isinstance(inbound.raw, dict) else {}
            config = dict(binding.config_json or {})
            if not _allowlist_allows(config, raw, inbound.from_user_id):
                return _drop_rejected(db, binding, inbound, envelope, expected_revision)
            target = {
                # 兼容现有 intake/outbox 的通用目标校验;channel_id 用于 REST 出站定位。
                "to_user_id": inbound.conv_key if inbound.is_group else inbound.from_user_id,
                "channel_id": str(raw.get("channel_id") or "").strip(),
                "guild_id": str(raw.get("guild_id") or "").strip(),
                "message_id": inbound.event_id,
            }
            event = ChannelInboundEvent(
                id=new_id("chevt"), tenant_id=binding.tenant_id, binding_id=binding.id,
                channel="discord", event_id=inbound.event_id, payload_json=envelope,
                config_revision=expected_revision, target_json=target, status="received",
                thread_id=_clean(raw.get("thread_id")),
                mention_user_ids=_mention_ids(raw, bot_id),
                command=_clean(raw.get("command")),
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


def _allowlist_allows(config: dict[str, Any], raw: dict[str, Any], author_id: str) -> bool:
    """第六重校验:config_json.allowlist 白名单判定(功能5)。

    线程消息按父频道判定(parent_id 优先),角色白名单需 members intent,首版不参与。
    """
    message_ctx = {
        "guild_id": str(raw.get("guild_id") or "").strip(),
        "channel_id": str(raw.get("parent_id") or raw.get("channel_id") or "").strip(),
        "author_id": author_id,
    }
    return evaluate_allowlist(message_ctx, config.get("allowlist"))


def _drop_rejected(
    db: Session,
    binding: ChannelBinding,
    inbound: ChannelInbound,
    envelope: dict[str, Any],
    expected_revision: int,
) -> StageResult:
    """白名单拒绝:静默丢弃 + 落库 rejected 可审计(R6);拒绝幂等去重。"""
    logger.warning(
        "discord 入站被白名单拒绝 binding=%s event=%s channel=%s author=%s",
        binding.id,
        inbound.event_id,
        str((inbound.raw or {}).get("channel_id") or ""),
        inbound.from_user_id,
    )
    rejected = ChannelInboundEvent(
        id=new_id("chevt"), tenant_id=binding.tenant_id, binding_id=binding.id,
        channel="discord", event_id=inbound.event_id, payload_json=envelope,
        config_revision=expected_revision, status="rejected", error="allowlist_denied",
    )
    db.add(rejected)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return StageResult(StageDisposition.SECURITY_DROP, error_code="allowlist_denied")


def _clean(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _mention_ids(raw: dict[str, Any], bot_id: str) -> str | None:
    mentions = [str(item) for item in (raw.get("mentions") or [])]
    mentions = [user_id for user_id in mentions if user_id and user_id != bot_id]
    if not mentions:
        return None
    return json.dumps(mentions, ensure_ascii=False)
