from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import model_for_agent
from app.channels.service_routing import (
    manual_pin_active,
    mounted_agents,
    set_current_agent,
)
from app.channels.service_session import find_channel_session
from app.db.models import AgentProfile, ChannelBinding, ChatSession, Message
from app.llm import LLMClient
from app.observability import EventLog

logger = logging.getLogger(__name__)

AUTO_ROUTE_CONFIDENCE_THRESHOLD = 0.75
# SOP 进行中(active_skill_id 非空)放宽为可切但要求更高置信度;
# 会话按员工各自独立,SOP 上下文在原员工会话里冻结可续,不会因切走丢失
SOP_ACTIVE_CONFIDENCE_THRESHOLD = 0.9
RECENT_MESSAGE_LIMIT = 2
RECENT_MESSAGE_CHAR_LIMIT = 200

SYSTEM_PROMPT = (
    "你是企业数字员工调度员。根据用户消息与候选员工名单，选择最合适的员工应答。\n"
    '只输出 JSON：{"agent_id": "<候选 agent_id 或 stay>", "confidence": 0到1之间的小数, "reason": "一句话原因"}。\n'
    "规则：消息意图不明确、或属于当前员工职责范围时选 stay；只有明确属于其他候选员工领域时才选该员工的 agent_id。"
)


@dataclass
class RouteDecision:
    """意图分类结果;任何失败都回退为保持当前员工。"""

    agent_id: str
    switched: bool
    confidence: float
    reason: str
    target_agent_id: str | None = None  # 分类器原始命中(未命中/被阈值拦下为 None)
    threshold: float = AUTO_ROUTE_CONFIDENCE_THRESHOLD  # 本次生效阈值(随决策落事件,便于复盘)
    # 回退失败原因(异常摘要/解析失败类型,截断 200 字);正常决策为空
    error: str = ""


def auto_route_enabled(binding: ChannelBinding) -> bool:
    """智能分发开关:config_json.auto_route 非 False 即开(默认开)。"""
    return (binding.config_json or {}).get("auto_route") is not False


def classify_intent(
    db: Session,
    tenant_id: str,
    candidates: list[dict[str, Any]],
    current_agent_id: str,
    message: str,
    recent_messages: list[dict[str, str]] | None = None,
    *,
    threshold: float = AUTO_ROUTE_CONFIDENCE_THRESHOLD,
) -> RouteDecision:
    """LLM 意图分类;超时/异常/坏 JSON/低于生效阈值一律回退"保持当前"(绝不抛错)。"""
    stay = RouteDecision(
        agent_id=current_agent_id, switched=False, confidence=0.0, reason="", threshold=threshold
    )
    if not candidates:
        return stay
    model_config = model_for_agent(db, tenant_id, None, "default")
    if not model_config:
        logger.warning("智能分发缺少默认模型配置，保持当前员工")
        stay.error = "缺少默认模型配置"
        return stay
    payload = {
        "current_agent_id": current_agent_id,
        "message": message,
        "recent_messages": recent_messages or [],
        "candidates": candidates,
    }
    try:
        text = LLMClient(model_config).generate_text(
            SYSTEM_PROMPT, payload, response_format={"type": "json_object"}
        )
        data = json.loads(text)
        if not isinstance(data, dict):
            stay.error = "分类响应不是 JSON 对象"
            return stay
        target = str(data.get("agent_id") or "").strip()
        confidence = float(data.get("confidence") or 0.0)
        reason = str(data.get("reason") or "")[:200]
    except Exception as exc:
        logger.warning("智能分发分类失败，保持当前员工: %s", exc)
        stay.error = str(exc)[:200]
        return stay
    candidate_ids = {str(item.get("agent_id") or "") for item in candidates}
    if not target or target == "stay" or target == current_agent_id:
        return RouteDecision(
            agent_id=current_agent_id,
            switched=False,
            confidence=confidence,
            reason=reason,
            threshold=threshold,
        )
    if target not in candidate_ids or confidence < threshold:
        return RouteDecision(
            agent_id=current_agent_id,
            switched=False,
            confidence=confidence,
            reason=reason,
            threshold=threshold,
        )
    return RouteDecision(
        agent_id=target,
        switched=True,
        confidence=confidence,
        reason=reason,
        target_agent_id=target,
        threshold=threshold,
    )


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
    rows = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id.in_(agent_ids),
        )
    ).all()
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
        set_current_agent(db, binding, external_conv_id, decision.agent_id)
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
