from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session, select

from app.channels.service_routing import (
    compare_and_set_current_agent,
    manual_pin_active,
    mounted_agents,
    route_revision,
    set_current_agent,
)
from app.channels.service_session import find_channel_session
from app.db.models import ChannelBinding, ChatSession, Message
from app.observability import EventLog

logger = logging.getLogger(__name__)

RECENT_MESSAGE_LIMIT = 2
RECENT_MESSAGE_CHAR_LIMIT = 200

from staffdeck_harness.routing.intent import (
    RouteDecision, classify_intent, AUTO_ROUTE_CONFIDENCE_THRESHOLD, SOP_ACTIVE_CONFIDENCE_THRESHOLD,
)


def auto_route_enabled(binding: ChannelBinding) -> bool:
    return (binding.config_json or {}).get("auto_route") is not False

def recent_channel_messages(db: Session, chat_session: ChatSession | None) -> list[dict[str, str]]:
    """当前会话最近 2 条消息(role + content 截断 200 字),作分类上下文。"""
    if not chat_session:
        return []
    rows = db.exec(
        select(Message)
        .where(Message.session_id == chat_session.id)
        .order_by(Message.created_at.desc())
        .limit(RECENT_MESSAGE_LIMIT)
    ).all()
    rows.reverse()
    return [{"role": row.role, "content": (row.content or "")[:RECENT_MESSAGE_CHAR_LIMIT]} for row in rows]


def _route_candidates(db: Session, tenant_id: str, agent_ids: list[str]) -> list[dict[str, Any]]:
    if not agent_ids:
        return []
    from staffdeck_harness.runtime.staff_directory import optional_staff_profile
    rows = [row for agent_id in dict.fromkeys(agent_ids)
            if (row := optional_staff_profile(db, tenant_id, agent_id)) is not None
            and row.status == "active"]
    return [
        {"agent_id": row.id, "name": row.name, "description": row.description or ""}
        for row in rows
    ]


def maybe_auto_route(
    db: Session,
    binding: ChannelBinding,
    current_agent_id: str,
    external_conv_id: str,
    message: str,
) -> RouteDecision | None:
    """智能分发入口：前置条件/粘性保护不满足返回 None（维持当前指针，不分类）。

    命中且非当前员工时更新路由指针；返回决策供调用方发 notice 与落事件。
    """
    if not auto_route_enabled(binding):
        return None
    mounts = mounted_agents(db, binding)
    if len(mounts) < 2:
        return None
    # 粘性保护:handoff 进行中、手动切换保护窗内,硬跳过;
    # SOP 进行中不跳过但提高切换阈值(会话按员工独立,SOP 上下文冻结可续)
    current_session = find_channel_session(db, binding, current_agent_id, external_conv_id)
    if current_session and current_session.status == "handoff":
        return None
    if manual_pin_active(db, binding, external_conv_id):
        return None
    inspected_route = route_revision(db, binding, external_conv_id)
    if not inspected_route:
        set_current_agent(db, binding, external_conv_id, current_agent_id)
        inspected_route = route_revision(db, binding, external_conv_id)
    if not inspected_route or inspected_route[0] != current_agent_id:
        return None
    threshold = (
        SOP_ACTIVE_CONFIDENCE_THRESHOLD
        if current_session and current_session.active_skill_id
        else AUTO_ROUTE_CONFIDENCE_THRESHOLD
    )
    candidates = _route_candidates(db, binding.tenant_id, [mount.agent_id for mount in mounts])
    decision = classify_intent(
        db,
        binding.tenant_id,
        candidates,
        current_agent_id,
        message,
        recent_channel_messages(db, current_session),
        threshold=threshold,
    )
    if decision.switched:
        switched = compare_and_set_current_agent(
            db,
            binding,
            external_conv_id,
            expected_agent_id=current_agent_id,
            expected_revision=inspected_route[1],
            agent_id=decision.agent_id,
        )
        if not switched:
            decision.agent_id = current_agent_id
            decision.switched = False
            decision.error = "route_changed_during_classification"
    return decision


def record_auto_route_event(
    db: Session,
    binding: ChannelBinding,
    session_id: str,
    decision: RouteDecision,
    current_agent_id: str,
) -> None:
    """决策落会话事件流,便于观测与复盘。"""
    EventLog(db).record(
        binding.tenant_id,
        session_id,
        "auto_route_decision",
        {
            "current_agent_id": current_agent_id,
            "agent_id": decision.agent_id,
            "target_agent_id": decision.target_agent_id,
            "switched": decision.switched,
            "confidence": decision.confidence,
            "threshold": decision.threshold,
            "reason": decision.reason,
            "error": decision.error,
        },
    )
