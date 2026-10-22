from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import ChatSession, Skill
from app.session.session_schema import PendingTask, RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


@dataclass
class QueuedTaskContinuation:
    reply: str
    task_results: list[dict[str, object]]
    active_skill: Skill | None
    router_decision: RouterDecision
    step_result: StepAgentResult
    tool_result: ToolResult | None


class TaskFramePolicy:
    @staticmethod
    def queued_continuation(
        replies: list[str],
        task_results: list[dict[str, object]],
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> QueuedTaskContinuation | None:
        if not replies and not task_results:
            return None
        return QueuedTaskContinuation(
            reply="\n\n".join(replies).strip(),
            task_results=task_results,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
        )

    @staticmethod
    def merge_reply_segment(
        replies: list[str], segment: str
    ) -> tuple[list[str], bool]:
        clean_segment = str(segment or "").strip()
        if not clean_segment:
            return replies, False
        return [*replies, clean_segment], False

    @staticmethod
    def find(chat_session: ChatSession, task_id: str) -> dict[str, Any] | None:
        for frame in chat_session.pending_tasks_json or []:
            if isinstance(frame, dict) and str(frame.get("task_id") or "") == str(task_id):
                return frame
        return None

    @classmethod
    def decision_from_pending(
        cls,
        chat_session: ChatSession,
        task_id: str,
        order_reason: str | None = None,
    ) -> RouterDecision | None:
        frame = cls.find(chat_session, task_id)
        if not frame:
            return None
        return cls.decision_from_frame(frame, task_id, order_reason)

    @staticmethod
    def decision_from_frame(
        frame: dict[str, Any],
        task_id: str,
        order_reason: str | None = None,
    ) -> RouterDecision | None:
        skill_id = frame.get("skill_id") or frame.get("target_skill_id")
        if not skill_id:
            return None
        slot_hints = {}
        if isinstance(frame.get("slots"), dict):
            slot_hints = dict(frame["slots"])
        elif isinstance(frame.get("slot_hints"), dict):
            slot_hints = dict(frame["slot_hints"])
        return RouterDecision(
            decision="switch_to_pending",
            selected_task_id=str(task_id),
            target_skill_id=str(skill_id),
            target_step_id=frame.get("step_id") or frame.get("target_step_id"),
            confidence=float(frame.get("confidence") or 0.0),
            user_intent=frame.get("intent_summary") or frame.get("user_intent"),
            reason=order_reason or frame.get("reason"),
            source_message=frame.get("source_message"),
            slot_hints=slot_hints,
        )

    @staticmethod
    def turn_followup_frames(router_decision: RouterDecision) -> list[PendingTask]:
        frames = list(router_decision.task_frames or [])
        if not frames:
            return []
        first = frames[0]
        return frames[1:] if first.target_skill_id == router_decision.target_skill_id else frames

    @staticmethod
    def decision_from_turn_frame(frame: PendingTask) -> RouterDecision:
        return RouterDecision(
            decision="start_new_task",
            target_skill_id=frame.target_skill_id,
            target_step_id=frame.target_step_id,
            confidence=frame.confidence,
            user_intent=frame.user_intent,
            reason=frame.reason or "按 Router 本轮 task_frames 顺序继续执行。",
            source_message=frame.source_message,
            slot_hints=dict(frame.slot_hints or {}),
            task_frames=[frame],
        )

    @staticmethod
    def next_pending_task_id(chat_session: ChatSession) -> str | None:
        for frame in chat_session.pending_tasks_json or []:
            if not isinstance(frame, dict):
                continue
            if str(frame.get("status") or "pending") != "pending":
                continue
            if not (frame.get("skill_id") or frame.get("target_skill_id")):
                continue
            task_id = str(frame.get("task_id") or "").strip()
            if task_id:
                return task_id
        return None
