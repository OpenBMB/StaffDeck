"""Routing policy only: candidate contract, confidence and stay/switch decisions."""
import math
from dataclasses import dataclass
from typing import Any
from sqlmodel import Session
from staffdeck_harness.runtime.inference import RuntimeInference

AUTO_ROUTE_CONFIDENCE_THRESHOLD = 0.75
SOP_ACTIVE_CONFIDENCE_THRESHOLD = 0.9

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
    payload = {"current_agent_id": current_agent_id, "message": message,
        "recent_messages": recent_messages or [], "candidates": candidates}
    def validate(data):
        target, confidence, reason = data.get("agent_id"), data.get("confidence"), data.get("reason", "")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("agent_id 必须是非空字符串")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence 必须是 0 到 1 之间的有限数值")
        if not isinstance(reason, str):
            raise ValueError("reason 必须是字符串")
        return target.strip(), float(confidence), reason[:200]
    try:
        target, confidence, reason = RuntimeInference(db).structured(tenant_id=tenant_id,
            staff_id=current_agent_id, phase="routing", system=SYSTEM_PROMPT,
            payload=payload, validate=validate)
    except Exception as exc:
        # Business fallback belongs to routing, not the model service.
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
