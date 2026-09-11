from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import threading
from datetime import timedelta
from pathlib import PosixPath
from typing import Any

from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from app.channels.adapters.base import channel_reaction_token
from app.channels.adapters.discord import DiscordPermanentError, DiscordTransientError
from app.channels.service_durable_inbox import reaction_target
from app.channels.service_identity import external_account_scope
from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelBinding,
    ChannelBindingAgent,
    ChannelDelivery,
    ChannelIdentity,
    ChannelInboundEvent,
    ChatSession,
    HumanHandoffRequest,
    Message,
    User,
    new_id,
    utc_now,
)
from app.session.origin import PILOTDECK_GROUP_CHAT_CHANNEL

logger = logging.getLogger(__name__)

_DELIVERY_BATCH_SIZE = 20
_REACTION_KINDS = {"reaction_add", "reaction_remove"}
# 启动重置卡死投递的阈值:仅重置 sending_since 为空或早于此秒数的 sending 行,
# 阈值内的视为仍被在飞 daemon 持有(避免交错启动重复投递)
SENDING_STALE_SECONDS = 120
_delivery_thread: threading.Thread | None = None
_reaction_delivery_thread: threading.Thread | None = None
_delivery_stop = threading.Event()
_FEISHU_DEDUP_RECOVERY_SECONDS = 55 * 60
_NON_DELIVERY_CHANNELS = {
    "public_api",
    PILOTDECK_GROUP_CHAT_CHANNEL,
    "skill_test",
}
# handoff 问题描述里要过滤掉的内部 slot 键。
_INTERNAL_SLOT_KEYS = frozenset(
    {"handoff_confirmed", "message_content", "_tool_results"}
)
# harness 产物桥接为 discord 附件载荷的上限:与 DiscordAdapter._MAX_ATTACHMENT_BYTES 对齐
_DISCORD_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_MAX_HARNESS_ARTIFACT_FILES = 10


def _channel_payload_from_harness_artifacts(
    db: Session,
    chat_session: ChatSession,
    message: Message,
) -> dict[str, list[dict[str, str]]] | None:
    """把 agent 运行生成的 harness 产物桥接为 discord 的 channel_payload.files 载荷。

    生成侧补齐(功能8,§4.8 D8-1):生成方未写 channel_payload 时,把
    assistant_metadata.harness_artifacts 中的 workspace_file 转成 base64 附件,
    使 Discord 富媒体路径(embeds/files)真正生效。任一文件超限或读取失败都
    静默跳过——纯降级,绝不让渠道投递登记因产物问题失败。
    """
    artifacts = (message.metadata_json or {}).get("harness_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    files: list[dict[str, str]] = []
    for entry in artifacts[:_MAX_HARNESS_ARTIFACT_FILES]:
        if not isinstance(entry, dict) or entry.get("type") != "workspace_file":
            continue
        raw_path = str(entry.get("path") or "").strip()
        task_frame_id = str(entry.get("task_frame_id") or "").strip()
        size = entry.get("size")
        if (
            not raw_path
            or not task_frame_id
            or not isinstance(size, int)
            or size < 0
        ):
            continue
        if size > _DISCORD_MAX_ATTACHMENT_BYTES:
            continue
        # app.core.harness_session_cleanup 会经 app.core 触发本模块导入,
        # 桥接是低频路径,函数内延迟导入避免模块级循环依赖。
        from app.core.harness_session_cleanup import harness_task_workspace_path
        from app.harness.artifacts import HarnessArtifactAccessError, open_harness_artifact

        try:
            workspace_root = harness_task_workspace_path(
                tenant_id=chat_session.tenant_id,
                session_id=chat_session.id,
                task_frame_id=task_frame_id,
                db=db,
            )
            opened = open_harness_artifact(workspace_root, raw_path)
            data = b"".join(opened.iter_bytes())
        except (HarnessArtifactAccessError, OSError):
            logger.warning(
                "harness 产物桥接跳过:文件不可读 path=%s frame=%s session=%s",
                raw_path,
                task_frame_id,
                chat_session.id,
            )
            continue
        content_type = mimetypes.guess_type(raw_path)[0] or "application/octet-stream"
        files.append(
            {
                "filename": PosixPath(raw_path).name,
                "data": base64.b64encode(data).decode("ascii"),
                "content_type": content_type,
            }
        )
    return {"files": files} if files else None


def _register_channel_payload_attachments(
    message: Message,
    payload: dict[str, Any],
) -> None:
    """把显式 channel_payload.files 附件镜像登记进 harness_artifacts(幂等)。

    生成方直接写 channel_payload.files(不经过 harness 工作区)时,这些附件不会
    出现在 harness_artifacts,员工后续检索"已发布交付物"时不可见——渠道与
    artifact 账目脱节。投递登记时把 files 补登记为 workspace_file 条目,后续
    轮次即可枚举。同名 path 已存在时跳过,重复登记不产生脏数据。
    """
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return
    metadata = message.metadata_json or {}
    existing = metadata.get("harness_artifacts")
    artifacts = list(existing) if isinstance(existing, list) else []
    existing_paths = {
        str(entry.get("path") or "")
        for entry in artifacts
        if isinstance(entry, dict)
    }
    frame_ids = metadata.get("task_frame_ids")
    task_frame_id = ""
    if isinstance(frame_ids, list) and frame_ids:
        task_frame_id = str(frame_ids[0])
    for file_entry in raw_files:
        if not isinstance(file_entry, dict):
            continue
        filename = str(file_entry.get("filename") or "").strip()
        if not filename or filename in existing_paths:
            continue
        raw_data = str(file_entry.get("data") or "")
        try:
            content = base64.b64decode(raw_data)
        except (ValueError, TypeError):
            logger.warning(
                "channel_payload 附件登记跳过:base64 解码失败 filename=%s",
                filename,
            )
            continue
        content_type = str(file_entry.get("content_type") or "").strip()
        if not content_type:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        artifacts.append(
            {
                "type": "workspace_file",
                "task_frame_id": task_frame_id,
                "path": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "display_name": filename,
                "content_type": content_type,
                "operation": "channel_delivery",
                "source": "channel_delivery",
            }
        )
        existing_paths.add(filename)
    # SQLAlchemy 对 JSON 列按内容比较判定变更;必须整体重建新 dict,
    # 否则原地修改同一引用不触发 flush。
    if artifacts != existing:
        message.metadata_json = {**metadata, "harness_artifacts": artifacts}


def _stage_failed_delivery(
    db: Session,
    chat_session: ChatSession,
    message: Message,
    *,
    binding_id: str,
    target: dict,
    error: str,
    idempotency_key: str | None = None,
) -> None:
    stable_key = idempotency_key or message.id
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == stable_key)
    ).first()
    if existing:
        return
    db.add(
        ChannelDelivery(
            tenant_id=chat_session.tenant_id,
            binding_id=binding_id,
            session_id=chat_session.id,
            message_id=message.id,
            target_json=dict(target),
            kind="reply",
            text=message.content,
            status="failed",
            next_attempt_at=None,
            last_error=error,
            idempotency_key=stable_key,
        )
    )


def _message_client_turn_id(db: Session, message: Message) -> str:
    metadata = message.metadata_json or {}
    user_message_id = str(metadata.get("user_message_id") or "").strip()
    user_message = db.get(Message, user_message_id) if user_message_id else None
    return str(
        ((user_message.metadata_json or {}) if user_message else {}).get("client_turn_id")
        or metadata.get("client_turn_id")
        or ""
    ).strip()


def _reply_idempotency_key(db: Session, binding_id: str, message: Message) -> str:
    client_turn_id = _message_client_turn_id(db, message)
    if client_turn_id:
        return f"channel-reply:{binding_id}:{client_turn_id}"
    return message.id


def _immutable_delivery_target(
    db: Session,
    binding: ChannelBinding,
    chat_session: ChatSession,
    message: Message,
) -> dict:
    client_turn_id = _message_client_turn_id(db, message)
    if not client_turn_id:
        # Legacy sessions may not carry a turn id. Snapshot the target at staging time;
        # delivery workers must never read the mutable session target later.
        return dict(chat_session.channel_target_json or {})
    event = db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.binding_id == binding.id,
            ChannelInboundEvent.event_id == client_turn_id,
        )
    ).first()
    if event:
        return dict(event.target_json or {})
    return dict(chat_session.channel_target_json or {})


def _find_active_binding_for_agent(db: Session, chat_session: ChatSession) -> ChannelBinding | None:
    """仅为无 binding_id 的 legacy 会话按稳定账号键恢复 active binding。"""
    account_key = str(chat_session.channel_account_key or "").strip()
    if not account_key:
        return None
    candidates = db.exec(
        select(ChannelBinding)
        .where(
            ChannelBinding.tenant_id == chat_session.tenant_id,
            ChannelBinding.channel == chat_session.channel,
            ChannelBinding.status == "active",
            ChannelBinding.external_account_key == account_key,
        )
        .order_by(ChannelBinding.created_at)
    ).all()
    if not candidates:
        return None
    binding_ids = [row.id for row in candidates]
    mount_rows = db.exec(
        select(ChannelBindingAgent).where(ChannelBindingAgent.binding_id.in_(binding_ids))
    ).all()
    mounts_by_binding: dict[str, set[str]] = {}
    for row in mount_rows:
        mounts_by_binding.setdefault(row.binding_id, set()).add(row.agent_id)
    for candidate in candidates:
        agent_ids = mounts_by_binding.get(candidate.id) or {candidate.agent_id}
        if chat_session.agent_id in agent_ids:
            return candidate
    return None


def _valid_delivery_target(channel: str, target: dict) -> bool:
    """按渠道校验投递目标是否足以发出一条消息。"""
    if channel == "feishu":
        return bool(target.get("message_id") or target.get("receive_id"))
    if channel == "wechat_kf":
        return bool(target.get("to_user_id") and target.get("open_kfid"))
    if channel == "discord":
        return bool(target.get("channel_id"))
    return bool(target.get("to_user_id") and target.get("context_token"))


def stage_user_message_mirror(
    db: Session,
    chat_session: ChatSession,
    message: Message,
    *,
    web_origin: bool,
) -> None:
    """把 Web 端提问镜像登记为渠道 outbox 投递（随主事务提交，不单独 commit）。

    与 stage_channel_delivery 的区别：
    - 仅 web_origin=True（Web 来源提问）时登记，渠道自身入站一律跳过，避免回声；
    - 仅在会话已完整锚定渠道（binding + account_key + target）时登记，纯 Web
      会话无锚定自然跳过；
    - 任何校验失败都静默跳过并只记日志，绝不让镜像影响 Web 主流程；
    - 投递目标直接取会话锚定（channel_target_json），不走 feishu 事件上下文
      推导（Web 提问没有对应 ChannelInboundEvent）。
    投递端无需改动：kind=user_mirror 走 _deliver_one_locked 的通用发送分支，
    且不会被误判为最终回复（is_final 只认 reply/error_notice）。
    """
    if not web_origin:
        return
    try:
        channel = str(getattr(chat_session, "channel", None) or "").strip()
        if not channel or channel in _NON_DELIVERY_CHANNELS:
            return
        text = str(message.content or "").strip()
        if not text:
            return
        binding_id = str(getattr(chat_session, "channel_binding_id", None) or "").strip()
        account_key = str(getattr(chat_session, "channel_account_key", None) or "").strip()
        target = dict(chat_session.channel_target_json or {})
        if not binding_id or not account_key or not target:
            return
        binding = db.get(ChannelBinding, binding_id)
        if (
            not binding
            or binding.status != "active"
            or binding.tenant_id != chat_session.tenant_id
            or binding.channel != channel
            or account_key != binding.external_account_key
        ):
            return
        if not _valid_delivery_target(channel, target):
            return
        existing = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.idempotency_key == message.id)
        ).first()
        if existing:
            return
        db.add(
            ChannelDelivery(
                tenant_id=chat_session.tenant_id,
                binding_id=binding.id,
                session_id=chat_session.id,
                message_id=message.id,
                target_json=target,
                kind="user_mirror",
                text=text,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=message.id,
            )
        )
    except Exception:
        logger.exception("用户消息镜像登记失败 session=%s", getattr(chat_session, "id", None))


def stage_channel_delivery(db: Session, chat_session: ChatSession, message: Message) -> None:
    """把 assistant 回复登记为渠道 outbox 投递（随主事务提交，不单独 commit）。

    Web 会话不受渠道 staging 影响；渠道会话必须留下 delivery 或让事务失败。
    """
    try:
        channel = str(getattr(chat_session, "channel", None) or "").strip()
        if not channel or channel in _NON_DELIVERY_CHANNELS:
            return
        # 已锚定会话绝不跨 binding 回退，避免携带旧 target/context_token 串 Bot。
        binding = None
        binding_id = getattr(chat_session, "channel_binding_id", None)
        if binding_id:
            binding = db.get(ChannelBinding, binding_id)
            if not binding or binding.status != "active":
                _stage_failed_delivery(
                    db,
                    chat_session,
                    message,
                    binding_id=binding_id,
                    target=dict(chat_session.channel_target_json or {}),
                    error="binding_missing_or_inactive",
                )
                return
            if binding.tenant_id != chat_session.tenant_id or binding.channel != chat_session.channel:
                raise RuntimeError("渠道会话与绑定租户或渠道不一致")
            if (
                not chat_session.channel_account_key
                or chat_session.channel_account_key != binding.external_account_key
            ):
                raise RuntimeError("渠道会话与绑定账号不一致")
        else:
            binding = _find_active_binding_for_agent(db, chat_session)
        if not binding:
            raise RuntimeError("渠道会话无法定位有效绑定")
        if not binding_id:
            # 精确恢复成功后持久化归属，后续 staging/delivery 不再走 legacy 分支。
            conflicting_session = db.exec(
                select(ChatSession).where(
                    ChatSession.id != chat_session.id,
                    ChatSession.agent_id == chat_session.agent_id,
                    ChatSession.channel == chat_session.channel,
                    ChatSession.channel_binding_id == binding.id,
                    ChatSession.external_conv_id == chat_session.external_conv_id,
                )
            ).first()
            if conflicting_session:
                logger.warning(
                    "legacy 渠道会话认领冲突，跳过投递 session=%s existing=%s binding=%s",
                    chat_session.id,
                    conflicting_session.id,
                    binding.id,
                )
                raise RuntimeError("legacy 渠道会话认领冲突")
            chat_session.channel_binding_id = binding.id
            db.add(chat_session)
            db.flush()
        target = _immutable_delivery_target(db, binding, chat_session, message)
        idempotency_key = _reply_idempotency_key(db, binding.id, message)
        existing = db.exec(
            select(ChannelDelivery).where(
                ChannelDelivery.idempotency_key == idempotency_key
            )
        ).first()
        if existing:
            return
        if not _valid_delivery_target(binding.channel, target):
            _stage_failed_delivery(
                db,
                chat_session,
                message,
                binding_id=binding.id,
                target=target,
                error="delivery_target_missing",
                idempotency_key=idempotency_key,
            )
            return
        # 富媒体结构化载荷(功能8,§4.8 D8-1):优先取生成方写入的 channel_payload
        # 键(embeds/files);discord 且无显式载荷时,把 harness 产物桥接为 files。
        payload = (message.metadata_json or {}).get("channel_payload")
        if not (isinstance(payload, dict) and payload) and binding.channel == "discord":
            payload = _channel_payload_from_harness_artifacts(db, chat_session, message)
        payload_json = (
            json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, dict) and payload
            else None
        )
        # 显式 channel_payload(生成方直接写 files)附件镜像进 harness_artifacts,
        # 让后续轮次可枚举到这些已发布交付物;桥接路径产物天然已登记,不重复。
        if (
            isinstance(payload, dict)
            and (message.metadata_json or {}).get("channel_payload")
        ):
            _register_channel_payload_attachments(message, payload)
            db.add(message)
        db.add(
            ChannelDelivery(
                tenant_id=chat_session.tenant_id,
                binding_id=binding.id,
                session_id=chat_session.id,
                message_id=message.id,
                target_json=target,
                kind="reply",
                text=message.content,
                payload_json=payload_json,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=idempotency_key,
            )
        )
    except Exception:
        logger.exception("渠道投递登记失败 session=%s", getattr(chat_session, "id", None))
        if getattr(chat_session, "channel", None):
            raise


def _deliver_due(db: Session, *, reaction_lane: bool = False) -> int:
    now = utc_now()
    statement = (
        select(ChannelDelivery)
        .where(ChannelDelivery.status == "pending")
        .where(ChannelDelivery.next_attempt_at.is_not(None))
        .where(ChannelDelivery.next_attempt_at <= now)
        .order_by(ChannelDelivery.created_at)
        .limit(_DELIVERY_BATCH_SIZE)
    )
    if reaction_lane:
        statement = statement.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        statement = statement.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    due_ids = db.exec(statement.with_only_columns(ChannelDelivery.id)).all()
    claimed = 0
    for delivery_id in due_ids:
        delivery = _claim_delivery(db, delivery_id, now=now, reaction_lane=reaction_lane)
        if delivery is None:
            continue
        claimed += 1
        _deliver_one(db, delivery)
    return claimed


def _claim_delivery(
    db: Session,
    delivery_id: str,
    *,
    now,
    reaction_lane: bool,
) -> ChannelDelivery | None:
    owner = new_id("delivery-owner")
    claim = (
        update(ChannelDelivery)
        .where(
            ChannelDelivery.id == delivery_id,
            ChannelDelivery.status == "pending",
            ChannelDelivery.next_attempt_at.is_not(None),
            ChannelDelivery.next_attempt_at <= now,
        )
        .values(
            status="sending",
            attempts=ChannelDelivery.attempts + 1,
            first_attempt_at=func.coalesce(ChannelDelivery.first_attempt_at, now),
            # 标记领取时刻:_reset_stuck_deliveries 据此区分在飞与卡死(120s 阈值)
            sending_since=now,
            delivery_owner=owner,
            delivery_generation=ChannelDelivery.delivery_generation + 1,
            updated_at=now,
        )
    )
    if reaction_lane:
        claim = claim.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        claim = claim.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    result = db.exec(claim)
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(ChannelDelivery, delivery_id)


def _finish_delivery_claim(
    db: Session,
    delivery: ChannelDelivery,
    *,
    status: str,
    last_error: str | None,
    next_attempt_at=None,
    delivered_at=None,
) -> bool:
    """Commit a delivery outcome only for the worker generation that owns it."""
    result = db.exec(
        update(ChannelDelivery)
        .where(
            ChannelDelivery.id == delivery.id,
            ChannelDelivery.status == "sending",
            ChannelDelivery.delivery_owner == delivery.delivery_owner,
            ChannelDelivery.delivery_generation == delivery.delivery_generation,
        )
        .values(
            status=status,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
            delivered_at=delivered_at,
            sending_since=None,
            delivery_owner=None,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _reaction_event_for_delivery(
    db: Session,
    delivery: ChannelDelivery,
    channel: str,
) -> ChannelInboundEvent | None:
    target = dict(delivery.target_json or {})
    message_id = str(target.get("message_id") or "").strip()
    if not message_id:
        return None

    def valid(event: ChannelInboundEvent | None) -> bool:
        return bool(
            event
            and event.binding_id == delivery.binding_id
            and event.tenant_id == delivery.tenant_id
            and event.channel == channel
            and event.event_id == message_id
        )

    event_pk = str(target.get("event_pk") or "").strip()
    event = db.get(ChannelInboundEvent, event_pk) if event_pk else None
    if event_pk:
        return event if valid(event) else None
    return db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.tenant_id == delivery.tenant_id,
            ChannelInboundEvent.binding_id == delivery.binding_id,
            ChannelInboundEvent.channel == channel,
            ChannelInboundEvent.event_id == message_id,
        )
    ).first()


def _stage_reaction_removal(
    db: Session,
    delivery: ChannelDelivery,
    event: ChannelInboundEvent,
) -> None:
    reaction_id = str(event.reaction_id or "").strip()
    if not reaction_id:
        return
    idempotency_key = f"{event.channel}-reaction-remove:{event.id}:{reaction_id}"
    existing = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.idempotency_key == idempotency_key)
    ).first()
    if existing:
        return
    token = channel_reaction_token(event.channel) or ""
    # 撤回接口没有 token 参数,渠道若需要标记名(如钉钉 emotion)只能从目标里读。
    removal_target = {**reaction_target(event), "reaction_id": reaction_id}
    if token:
        removal_target["reaction_token"] = token
    db.add(
        ChannelDelivery(
            tenant_id=delivery.tenant_id,
            binding_id=delivery.binding_id,
            session_id=f"event:{event.id}",
            message_id=None,
            target_json=removal_target,
            kind="reaction_remove",
            text=token,
            status="pending",
            next_attempt_at=utc_now(),
            idempotency_key=idempotency_key,
        )
    )


def _event_has_delivered_response(
    db: Session,
    event: ChannelInboundEvent,
) -> bool:
    deliveries = db.exec(
        select(ChannelDelivery).where(
            ChannelDelivery.binding_id == event.binding_id,
            ChannelDelivery.status == "delivered",
        )
    ).all()
    for row in deliveries:
        if row.kind in _REACTION_KINDS:
            continue
        target = row.target_json or {}
        is_final = row.kind in {"reply", "error_notice"} or bool(
            target.get("reaction_final")
        )
        if is_final and str(target.get("message_id") or "") == event.event_id:
            return True
    return False


def _should_auto_thread(
    binding: ChannelBinding,
    delivery: ChannelDelivery,
    target: dict,
) -> bool:
    """判定是否应为该投递自动创建 discord 公开线程(D2-4)。

    仅 discord 群聊(reply + guild_id)且特性显式开启时触发;已在线程内的会话
    (target 带 thread_id)、语音投递、以及建线程永久失败过的会话(blocked 标记)
    一律跳过,默认关闭保持存量行为。
    """
    if binding.channel != "discord":
        return False
    if delivery.kind != "reply":
        return False
    if delivery.delivery_kind == "voice":
        return False
    if target.get("thread_id"):
        return False
    if not target.get("guild_id"):
        return False
    if target.get("auto_thread_blocked"):
        return False
    features = (binding.config_json or {}).get("features") or {}
    return features.get("auto_thread") is True


def _thread_name_for(db: Session, delivery: ChannelDelivery) -> str:
    """解析自动建线程的线程名:metadata.thread_name > 会话首条用户消息 > 默认值。"""
    message = db.get(Message, delivery.message_id) if delivery.message_id else None
    if message:
        thread_name = str((message.metadata_json or {}).get("thread_name") or "").strip()
        if thread_name:
            return thread_name
    first_user = db.exec(
        select(Message)
        .where(Message.session_id == delivery.session_id, Message.role == "user")
        .order_by(Message.created_at, Message.id)
        .limit(1)
    ).first()
    if first_user:
        cleaned = " ".join(str(first_user.content or "").split())
        if cleaned:
            return cleaned[:40]
    return "对话"


def _deliver_one(db: Session, delivery: ChannelDelivery) -> None:
    from app.channels import binding_lifecycle_lock

    with binding_lifecycle_lock(delivery.binding_id):
        db.expire(delivery)
        db.refresh(delivery)
        if delivery.status != "sending":
            return
        _deliver_one_locked(db, delivery)


def _deliver_one_locked(db: Session, delivery: ChannelDelivery) -> None:
    from app.channels.adapters import get_channel_adapter

    settings = get_settings()
    binding = db.get(ChannelBinding, delivery.binding_id)
    # 绑定停用后仍要允许收尾:撤回残留标记,以及重试可能已挂上的标记。
    allow_inactive_reaction = bool(
        binding
        and channel_reaction_token(binding.channel)
        and binding.credentials_enc
        and (
            delivery.kind == "reaction_remove"
            or (delivery.kind == "reaction_add" and delivery.attempts > 1)
        )
    )
    invalid_binding = (
        not binding
        or binding.tenant_id != delivery.tenant_id
        or (binding.status != "active" and not allow_inactive_reaction)
    )
    if invalid_binding:
        _finish_delivery_claim(
            db,
            delivery,
            status="failed",
            last_error="渠道绑定不存在或已停用",
        )
        return
    reaction_event = None
    if delivery.kind in _REACTION_KINDS:
        if not channel_reaction_token(binding.channel):
            _finish_delivery_claim(
                db,
                delivery,
                status="failed",
                last_error="reaction 渠道无效",
            )
            return
        reaction_event = _reaction_event_for_delivery(db, delivery, binding.channel)
        if not reaction_event:
            _finish_delivery_claim(
                db,
                delivery,
                status="failed",
                last_error="reaction 事件边界无效",
            )
            return
    if (
        binding.channel == "feishu"
        and delivery.kind not in _REACTION_KINDS
        and delivery.attempts > 0
        and delivery.first_attempt_at is not None
        and (utc_now() - delivery.first_attempt_at).total_seconds()
        > _FEISHU_DEDUP_RECOVERY_SECONDS
    ):
        _finish_delivery_claim(
            db,
            delivery,
            status="failed",
            last_error="remote_state_unknown",
        )
        return
    if delivery.kind == "reply":
        chat_session = db.get(ChatSession, delivery.session_id)
        invalid_session = (
            not chat_session
            or chat_session.tenant_id != delivery.tenant_id
            or binding.tenant_id != delivery.tenant_id
            or chat_session.channel_binding_id != binding.id
            or chat_session.channel != binding.channel
            or not chat_session.channel_account_key
            or chat_session.channel_account_key != binding.external_account_key
        )
        if invalid_session:
            _finish_delivery_claim(
                db,
                delivery,
                status="failed",
                last_error="渠道会话与绑定账号不一致",
            )
            return
    try:
        adapter = get_channel_adapter(binding.channel)
        target = dict(delivery.target_json or {})
        if _should_auto_thread(binding, delivery, target):
            # D2-4:建线程是外部副作用,成功立即提交 thread_id 到 target/session;
            # 后续 send 失败重试经 refresh 读回 target_json,幂等跳过再建线程。
            create_thread = getattr(adapter, "create_thread", None)
            if not callable(create_thread):
                logger.warning(
                    "discord 适配器缺少 create_thread,自动建线程跳过 binding=%s",
                    binding.id,
                )
            else:
                try:
                    thread_id = create_thread(binding, target, _thread_name_for(db, delivery))
                except DiscordPermanentError:
                    logger.warning(
                        "discord 自动建线程永久失败,降级发主频道 binding=%s delivery=%s",
                        binding.id,
                        delivery.id,
                    )
                    blocked = dict(chat_session.channel_target_json or {})
                    blocked["auto_thread_blocked"] = True
                    chat_session.channel_target_json = blocked
                    # blocked 同步写回 delivery:本投递后续重试经 refresh 读回
                    # target_json 短路 _should_auto_thread,避免每次重试重复建线程
                    delivery.target_json = dict(blocked)
                    db.add(delivery)
                    db.add(chat_session)
                    db.commit()
                except DiscordTransientError:
                    raise
                else:
                    target["thread_id"] = thread_id
                    delivery.target_json = dict(target)
                    chat_session.channel_target_json = dict(target)
                    db.add(delivery)
                    db.add(chat_session)
                    db.commit()
        sent_message_id: str | None = None
        if delivery.delivery_kind == "voice":
            send_voice = getattr(adapter, "send_voice", None)
            if not callable(send_voice):
                raise RuntimeError("渠道适配器不支持语音投递")
            voice_payload = json.loads(delivery.payload_json) if delivery.payload_json else None
            audio = dict((voice_payload or {}).get("audio") or {})
            if not audio:
                raise RuntimeError("语音投递缺少 audio 载荷")
            send_voice(binding, target, audio)
        elif delivery.kind == "reaction_add":
            add_reaction = getattr(adapter, "add_reaction", None)
            if not callable(add_reaction):
                raise RuntimeError("渠道适配器不支持 reaction")
            reaction_id = None
            # 重挂会产生第二个标记的渠道必须先回查;声明重挂幂等的渠道直接重发。
            if delivery.attempts > 1 and not getattr(
                adapter, "reaction_attach_idempotent", False
            ):
                find_reaction = getattr(adapter, "find_own_reaction", None)
                if not callable(find_reaction):
                    raise RuntimeError("渠道适配器不支持 reaction 恢复")
                reaction_id = find_reaction(binding, target, delivery.text)
            if not reaction_id and binding.status == "active":
                reaction_id = add_reaction(binding, target, delivery.text)
            if reaction_id:
                reaction_event.reaction_id = reaction_id
                reaction_event.updated_at = utc_now()
                db.add(reaction_event)
                if binding.status != "active" or _event_has_delivered_response(
                    db, reaction_event
                ):
                    _stage_reaction_removal(db, delivery, reaction_event)
        elif delivery.kind == "reaction_remove":
            remove_reaction = getattr(adapter, "remove_reaction", None)
            if not callable(remove_reaction):
                raise RuntimeError("渠道适配器不支持 reaction 清理")
            remove_reaction(binding, target, str(target.get("reaction_id") or ""))
            if reaction_event.reaction_id == str(target.get("reaction_id") or ""):
                reaction_event.reaction_id = None
                reaction_event.updated_at = utc_now()
                db.add(reaction_event)
        else:
            send_kwargs: dict[str, Any] = {"idempotency_key": delivery.idempotency_key}
            if delivery.payload_json and binding.channel == "discord":
                # 仅 DiscordAdapter.send 声明 payload_json 命名参数(功能8富媒体);
                # 存量渠道签名无此参数,条件传递保持零影响
                send_kwargs["payload_json"] = delivery.payload_json
            sent_message_id = adapter.send(binding, target, delivery.text, **send_kwargs)
    except Exception as exc:
        last_error = str(exc)[:500]
        retryable = bool(getattr(exc, "retryable", True))
        if not retryable or delivery.attempts >= settings.channel_delivery_max_attempts:
            status = "failed"
            next_attempt_at = None
        else:
            delay = min(2**delivery.attempts, 300)
            status = "pending"
            next_attempt_at = utc_now() + timedelta(seconds=delay)
        _finish_delivery_claim(
            db,
            delivery,
            status=status,
            last_error=last_error,
            next_attempt_at=next_attempt_at,
        )
        logger.warning("渠道投递失败(第 %s 次) delivery=%s: %s", delivery.attempts, delivery.id, exc)
        return
    # handoff_notice/handoff_ack 投递成功后,把飞书返回的 message_id 回写到 delivery;
    # handoff_notice 额外同步到 HumanHandoffRequest.notify_message_id。阶段 4 据此
    # 关联处理人的飞书引用回复(含对确认消息的再次回复)。
    if delivery.kind in {"handoff_notice", "handoff_ack"} and sent_message_id:
        delivery.message_id = sent_message_id
        if delivery.kind == "handoff_notice":
            _write_handoff_notify_message_id(db, delivery, sent_message_id)
    if channel_reaction_token(binding.channel) and delivery.kind not in _REACTION_KINDS:
        event = _reaction_event_for_delivery(db, delivery, binding.channel)
        target = delivery.target_json or {}
        is_final = delivery.kind in {"reply", "error_notice"} or bool(
            target.get("reaction_final")
        )
        if event and is_final:
            _stage_reaction_removal(db, delivery, event)
    _finish_delivery_claim(
        db,
        delivery,
        status="delivered",
        last_error=None,
        delivered_at=utc_now(),
    )


def cleanup_channel_reactions_before_binding_delete(
    db: Session,
    binding: ChannelBinding,
) -> None:
    """Delete known remote reactions before their binding credentials are removed."""
    token = channel_reaction_token(binding.channel)
    if not token:
        return
    from app.channels.adapters import get_channel_adapter

    uncertain_adds = db.exec(
        select(ChannelDelivery).where(
            ChannelDelivery.tenant_id == binding.tenant_id,
            ChannelDelivery.binding_id == binding.id,
            ChannelDelivery.kind == "reaction_add",
            ChannelDelivery.attempts > 0,
            ChannelDelivery.status.in_({"pending", "sending", "failed"}),
        )
    ).all()
    adapter = get_channel_adapter(binding.channel)
    remove_reaction = getattr(adapter, "remove_reaction", None)
    if not callable(remove_reaction):
        raise RuntimeError("渠道适配器不支持 reaction 清理")
    # 重挂幂等的渠道(钉钉)没有回查接口,但撤回参数与挂上完全对称,可以无条件撤回。
    attach_idempotent = bool(getattr(adapter, "reaction_attach_idempotent", False))
    find_reaction = getattr(adapter, "find_own_reaction", None)
    if not attach_idempotent and not callable(find_reaction):
        raise RuntimeError("渠道适配器不支持 reaction 恢复")
    event_by_id = {
        event.id: event
        for event in db.exec(
            select(ChannelInboundEvent).where(
                ChannelInboundEvent.tenant_id == binding.tenant_id,
                ChannelInboundEvent.binding_id == binding.id,
                ChannelInboundEvent.channel == binding.channel,
            )
        ).all()
    }
    for delivery in uncertain_adds:
        event = event_by_id.get(str((delivery.target_json or {}).get("event_pk") or ""))
        if not event:
            raise RuntimeError("reaction 事件边界无效")
        if event.reaction_id:
            continue
        message_id = str((delivery.target_json or {}).get("message_id") or "").strip()
        if not message_id or message_id != event.event_id:
            raise RuntimeError("reaction 事件边界无效")
        target = reaction_target(event)
        if attach_idempotent:
            remove_reaction(binding, target, "")
            continue
        reaction_id = find_reaction(binding, target, delivery.text)
        if reaction_id:
            remove_reaction(binding, target, reaction_id)
    events = db.exec(
        select(ChannelInboundEvent).where(
            ChannelInboundEvent.tenant_id == binding.tenant_id,
            ChannelInboundEvent.binding_id == binding.id,
            ChannelInboundEvent.channel == binding.channel,
            ChannelInboundEvent.reaction_id.is_not(None),
        )
    ).all()
    for event in events:
        reaction_id = str(event.reaction_id or "").strip()
        if not reaction_id:
            continue
        remove_reaction(binding, reaction_target(event), reaction_id)
        event.reaction_id = None
        event.updated_at = utc_now()
        db.add(event)
    db.flush()


def _reset_stuck_deliveries(db: Session, *, reaction_lane: bool = False) -> None:
    now = utc_now()
    stale_before = now - timedelta(seconds=SENDING_STALE_SECONDS)
    statement = select(ChannelDelivery).where(
        ChannelDelivery.status == "sending",
        or_(
            ChannelDelivery.sending_since.is_(None),
            ChannelDelivery.sending_since <= stale_before,
        ),
    )
    if reaction_lane:
        statement = statement.where(ChannelDelivery.kind.in_(_REACTION_KINDS))
    else:
        statement = statement.where(ChannelDelivery.kind.notin_(_REACTION_KINDS))
    stuck = db.exec(statement).all()
    for row in stuck:
        # A reply send that outlived its lease has an unknown remote outcome. Replaying it
        # is not safe for providers that do not offer a verifiable idempotent receipt.
        if row.kind not in _REACTION_KINDS:
            row.status = "failed"
            row.last_error = "remote_state_unknown"
            row.next_attempt_at = None
            row.delivery_owner = None
            row.delivery_generation += 1
            row.updated_at = now
            db.add(row)
            continue
        # Reaction adapters have explicit recovery semantics (lookup or idempotent attach),
        # so a new generation may safely take over.
        row.status = "pending"
        row.sending_since = None
        row.delivery_owner = None
        row.delivery_generation += 1
        row.next_attempt_at = now
        row.updated_at = now
        db.add(row)
    if stuck:
        db.commit()


def run_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    _run_delivery_lane(
        once=once,
        poll_seconds=poll_seconds,
        db_engine=db_engine,
        reaction_lane=False,
    )


def run_reaction_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    _run_delivery_lane(
        once=once,
        poll_seconds=poll_seconds,
        db_engine=db_engine,
        reaction_lane=True,
    )


def _run_delivery_lane(
    *,
    once: bool,
    poll_seconds: float | None,
    db_engine,
    reaction_lane: bool,
) -> None:
    use_engine = db_engine or engine
    interval = poll_seconds if poll_seconds is not None else get_settings().channel_delivery_poll_seconds
    with Session(use_engine) as db:
        _reset_stuck_deliveries(db, reaction_lane=reaction_lane)
    while True:
        try:
            with Session(use_engine) as db:
                _deliver_due(db, reaction_lane=reaction_lane)
        except Exception:
            logger.exception("渠道投递守护轮询失败")
        if once or _delivery_stop.is_set():
            return
        if _delivery_stop.wait(max(0.2, interval)):
            return


def start_delivery_daemon(*, db_engine=None) -> None:
    global _delivery_thread, _reaction_delivery_thread
    _delivery_stop.clear()
    if not (_delivery_thread and _delivery_thread.is_alive()):
        _delivery_thread = threading.Thread(
            target=run_delivery_daemon,
            kwargs={"db_engine": db_engine},
            name="staffdeck-channel-delivery",
            daemon=True,
        )
        _delivery_thread.start()
    if not (_reaction_delivery_thread and _reaction_delivery_thread.is_alive()):
        _reaction_delivery_thread = threading.Thread(
            target=run_reaction_delivery_daemon,
            kwargs={"db_engine": db_engine},
            name="staffdeck-channel-reaction-delivery",
            daemon=True,
        )
        _reaction_delivery_thread.start()


def stop_delivery_daemon(timeout_seconds: float = 5.0) -> bool:
    global _delivery_thread, _reaction_delivery_thread
    _delivery_stop.set()
    threads = [_delivery_thread, _reaction_delivery_thread]
    deadline = utc_now() + timedelta(seconds=max(0.0, timeout_seconds))
    for thread in threads:
        if thread and thread.is_alive():
            remaining = max(0.0, (deadline - utc_now()).total_seconds())
            thread.join(timeout=remaining)
    stopped = not any(thread and thread.is_alive() for thread in threads)
    if stopped:
        _delivery_thread = None
        _reaction_delivery_thread = None
    return stopped


def notify_binding_creator(db: Session, binding: ChannelBinding, text: str) -> None:
    """渠道异常主动告警:给绑定创建者发一条 kind=admin_alert 的渠道消息。

    创建者在该渠道且与本 binding 同 scope(external_account_scope)下已有身份时才
    投递——多企业身份不跨 scope 发送;优先取其最近私聊会话的 channel_target_json
    (含有效 context_token);无会话则按身份基本信息构造(微信侧缺 context_token
    时投递会重试后失败,仅记日志可接受)。任何异常仅记日志。
    """
    try:
        if not binding.created_by_user_id:
            return
        scope = external_account_scope(db, binding)
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.tenant_id == binding.tenant_id,
                ChannelIdentity.channel == binding.channel,
                ChannelIdentity.external_account_scope == scope,
                ChannelIdentity.staffdeck_user_id == binding.created_by_user_id,
            )
        ).first()
        if not identity:
            logger.info(
                "渠道告警跳过:创建者在该渠道同 scope 下无身份 binding=%s scope=%s",
                binding.id,
                scope,
            )
            return
        chat_session = db.exec(
            select(ChatSession)
            .where(
                ChatSession.tenant_id == binding.tenant_id,
                ChatSession.channel == binding.channel,
                # 限定本绑定:同一用户多账号时不得拿 A 账号的目标经 B 账号发送
                ChatSession.channel_binding_id == binding.id,
                ChatSession.user_id == binding.created_by_user_id,
                ChatSession.external_conv_id.is_not(None),
            )
            .order_by(ChatSession.updated_at.desc())
            .limit(1)
        ).first()
        if chat_session and (chat_session.channel_target_json or {}).get("to_user_id"):
            target = dict(chat_session.channel_target_json)
            session_id = chat_session.id
        elif binding.channel == "discord":
            # discord 无 context_token 体系,fallback 缺 channel_id 必然永久失败,跳过
            logger.info(
                "渠道告警跳过:discord 创建者无可用会话目标 binding=%s", binding.id
            )
            return
        else:
            target = {"to_user_id": identity.external_user_id, "context_token": ""}
            session_id = f"alert:{identity.id}"
        db.add(
            ChannelDelivery(
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                session_id=session_id,
                message_id=None,
                target_json=target,
                kind="admin_alert",
                text=text,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=new_id("chalert"),
            )
        )
        db.commit()
    except Exception:
        logger.exception("渠道告警投递登记失败 binding=%s", binding.id)


def _resolve_assignee_feishu_open_id(
    db: Session,
    binding: ChannelBinding,
    assignee_user_id: str | None,
) -> str | None:
    """确定 handoff assignee 的飞书 open_id。

    主链路:用当前 binding 的 external_account_scope 查 ChannelIdentity
    (staffdeck_user_id → external_user_id),保证跨 binding/scope 隔离。
    未命中返回 None(由调用方兜底,网页收件箱仍可用)。

    注:手机号/邮箱反查需要 User 表存储手机号/邮箱,当前 User 模型无此字段,
    故反查 fallback 暂不启用;待前端用户档案补齐后可在此接入。
    """
    scope = external_account_scope(db, binding)
    if assignee_user_id:
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.tenant_id == binding.tenant_id,
                ChannelIdentity.channel == "feishu",
                ChannelIdentity.external_account_scope == scope,
                ChannelIdentity.staffdeck_user_id == assignee_user_id,
            )
        ).first()
        if identity and identity.external_user_id:
            return identity.external_user_id
    return None


def resolve_assignee_channel_identity(
    db: Session,
    binding: ChannelBinding,
    assignee_user_id: str | None,
) -> ChannelIdentity | None:
    """按当前 binding 渠道与 scope 解析 assignee 的非群聊渠道身份。

    与 _resolve_assignee_feishu_open_id 同一套 scope 隔离逻辑,
    但渠道随 binding 走,供通用 handoff 通知投递使用(飞书/企微等)。
    未命中返回 None(网页收件箱兜底)。
    """
    scope = external_account_scope(db, binding)
    if not assignee_user_id:
        return None
    identity = db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == binding.tenant_id,
            ChannelIdentity.channel == binding.channel,
            ChannelIdentity.external_account_scope == scope,
            ChannelIdentity.staffdeck_user_id == assignee_user_id,
            ~ChannelIdentity.external_user_id.startswith("group:"),
        )
    ).first()
    return identity or None


# 支持 handoff 通知私聊投递的渠道:适配器具备"指定用户主动私聊"能力。
# 钉钉/微信适配器只能回会话内消息(依赖 session_webhook/context_token),
# 无法主动私聊处理人,故不在集合内。
HANDOFF_NOTIFY_CHANNELS = frozenset({"feishu", "wecom"})
# 用户重复触发同一个 pending handoff 时,避免短时间内刷屏,但允许长期未处理的
# 请求重新通知处理人。
HANDOFF_NOTIFY_RETRY_SECONDS = 300

# 各渠道 handoff 通知的投递 target 构造。
_HANDOFF_NOTIFY_TARGET_BUILDERS = {
    "feishu": lambda external_user_id, handoff_id: {
        "receive_id_type": "open_id",
        "receive_id": external_user_id,
        "handoff_id": handoff_id,
    },
    "wecom": lambda external_user_id, handoff_id: {
        "to_user_id": external_user_id,
        "handoff_id": handoff_id,
    },
}


def resolve_handoff_notify_binding(
    db: Session,
    tenant_id: str,
    notify_channel: str,
) -> ChannelBinding | None:
    """按通知渠道偏好解析可达的投递 binding。

    notify_channel 为具体渠道(如 feishu)时,在租户内找该渠道的 active binding
    (排除团队绑定与非交付渠道);找不到返回 None。
    """
    notify_channel = str(notify_channel or "").strip()
    if not notify_channel or notify_channel == "web":
        return None
    bindings = db.exec(
        select(ChannelBinding).where(
            ChannelBinding.tenant_id == tenant_id,
            ChannelBinding.channel == notify_channel,
            ChannelBinding.status == "active",
        )
    ).all()
    return next((row for row in bindings if not row.team_id), None)


def _write_handoff_notify_message_id(
    db: Session,
    delivery: ChannelDelivery,
    message_id: str,
) -> None:
    """handoff_notice 投递成功后,把飞书 message_id 回写到关联的 HumanHandoffRequest。

    delivery.target_json.handoff_id 指向待更新的 handoff;阶段 4 据此关联处理人回复。
    """
    target = delivery.target_json or {}
    handoff_id = str(target.get("handoff_id") or "").strip()
    if not handoff_id:
        return
    handoff = db.get(HumanHandoffRequest, handoff_id)
    if not handoff or handoff.tenant_id != delivery.tenant_id:
        return
    handoff.notify_message_id = message_id
    handoff.updated_at = utc_now()
    db.add(handoff)


def _resolve_inquirer_display_name(
    db: Session,
    session: ChatSession,
    binding: ChannelBinding,
) -> str:
    """查找提问人显示名:优先 ChannelIdentity.display_name,回退 User.display_name。"""
    if not session.user_id:
        return ""
    scope = external_account_scope(db, binding)
    identity = db.exec(
        select(ChannelIdentity).where(
            ChannelIdentity.staffdeck_user_id == session.user_id,
            ChannelIdentity.channel == binding.channel,
            ChannelIdentity.external_account_scope == scope,
        )
    ).first()
    if identity and identity.display_name:
        return identity.display_name.strip()
    user = db.get(User, session.user_id)
    if user:
        return str(user.display_name or user.username or "").strip()
    return ""


def _build_handoff_problem_description(
    db: Session,
    handoff: HumanHandoffRequest,
    binding: ChannelBinding,
) -> str:
    """构造给处理人看的问题描述:提问人 + 用户原始消息 + 已收集 slots + step 名称。

    找不到 session/message 时回退到 handoff.pending_question。
    """
    parts: list[str] = []
    # step name
    metadata = handoff.metadata_json or {}
    step = metadata.get("step") if isinstance(metadata, dict) else None
    if isinstance(step, dict):
        step_name = str(step.get("name") or "").strip()
        if step_name:
            parts.append(f"[{step_name}]")
    # 提问人 + 用户最后一条消息 + slots
    session = db.get(ChatSession, handoff.session_id)
    if session:
        inquirer = _resolve_inquirer_display_name(db, session, binding)
        if inquirer:
            parts.append(f"提问人:{inquirer}")
        user_msg = db.exec(
            select(Message)
            .where(
                Message.session_id == handoff.session_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc())
        ).first()
        if user_msg and user_msg.content.strip():
            parts.append(user_msg.content.strip()[:300])
        slots = session.slots_json or {}
        if isinstance(slots, dict) and slots:
            slot_lines = [
                f"  {k}: {v}"
                for k, v in slots.items()
                if v and k not in _INTERNAL_SLOT_KEYS
            ]
            if slot_lines:
                parts.append("已收集信息:\n" + "\n".join(slot_lines))
    if not parts:
        fallback = (handoff.pending_question or "").strip()
        if fallback:
            return fallback[:600]
        return "当前 SOP 需要人工确认后继续执行。"
    # 截断保证整条通知(含上下文摘要)不超过渠道单条消息上限:超限会拆分多条,
    # 处理人引用回复时只有末条消息 id 可关联,拆分会破坏引用回复匹配。
    return "\n".join(parts)[:600]


def notify_handoff_assignee(
    db: Session,
    binding: ChannelBinding,
    handoff: HumanHandoffRequest,
    pending_question: str,
    context_summary: str,
) -> None:
    """转人工时给 assignee 发渠道私聊通知(kind=handoff_notice)。

    通用链路:assignee_user_id → 当前 binding scope 下的非群聊 ChannelIdentity
    → 外部用户 id(open_id/chat_id)。按 binding.channel 构造各渠道投递 target,
    经 outbox worker 用对应渠道 adapter 投递。无可用身份时跳过(网页收件箱兜底)。
    任何异常仅记日志,不影响 handoff 主流程。
    """
    try:
        if binding.channel not in HANDOFF_NOTIFY_CHANNELS:
            logger.info(
                "handoff 通知跳过:渠道暂不支持私聊通知 handoff=%s binding=%s channel=%s",
                handoff.id,
                binding.id,
                binding.channel,
            )
            return
        existing_notice = db.exec(
            select(ChannelDelivery).where(
                ChannelDelivery.tenant_id == binding.tenant_id,
                ChannelDelivery.binding_id == binding.id,
                ChannelDelivery.kind == "handoff_notice",
                ChannelDelivery.session_id == f"handoff:{handoff.id}",
            ).order_by(ChannelDelivery.created_at.desc())
        ).first()
        if existing_notice and (
            existing_notice.status == "pending"
            or (
                existing_notice.status == "delivered"
                and (utc_now() - existing_notice.created_at).total_seconds()
                < HANDOFF_NOTIFY_RETRY_SECONDS
            )
        ):
            return
        identity = resolve_assignee_channel_identity(db, binding, handoff.assignee_user_id)
        external_user_id = identity.external_user_id if identity else None
        if not external_user_id:
            logger.info(
                "handoff 通知跳过:assignee 在当前绑定作用域无可私聊身份 handoff=%s binding=%s assignee=%s",
                handoff.id,
                binding.id,
                handoff.assignee_user_id,
            )
            return
        # assignee 显示名:从 User 表取,无则空
        assignee = db.get(User, handoff.assignee_user_id) if handoff.assignee_user_id else None
        name = ""
        if assignee:
            name = str(assignee.display_name or assignee.username or "").strip()
        problem_description = _build_handoff_problem_description(db, handoff, binding)
        text_parts = [
            f"【人工介入转接】{'已转接给真人员工 ' + name if name else '有一条人工介入待处理'}",
            "",
            "问题:" + problem_description,
        ]
        if context_summary:
            text_parts.append("")
            text_parts.append("上下文:")
            text_parts.append(context_summary[:800])
        text_parts.append("")
        if binding.channel == "feishu":
            text_parts.append(
                "如需答复，请直接回复本条消息（引用后输入答复内容）；"
                "也可发送 /回复反馈 <答复内容>。"
            )
        else:
            text_parts.append(
                f"请发送 /回复反馈 {handoff.id} <答复内容> 精确回复此请求。"
            )
        text = "\n".join(text_parts)
        build_target = _HANDOFF_NOTIFY_TARGET_BUILDERS[binding.channel]
        target = build_target(external_user_id, handoff.id)
        target["handoff_id"] = handoff.id
        db.add(
            ChannelDelivery(
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                session_id=f"handoff:{handoff.id}",
                message_id=None,
                target_json=target,
                kind="handoff_notice",
                text=text,
                status="pending",
                next_attempt_at=utc_now(),
                idempotency_key=new_id("hnotice"),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("handoff 通知登记失败 handoff=%s binding=%s", handoff.id, binding.id)
