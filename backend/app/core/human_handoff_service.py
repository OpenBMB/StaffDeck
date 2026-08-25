from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    ChatSession,
    HumanHandoffRequest,
    Message,
    User,
    utc_now,
)
from app.session.session_schema import StepAgentResult


class HumanHandoffService:
    def __init__(self, db: Session, events: Any) -> None:
        self.db = db
        self.events = events

    def create(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        *,
        current_step_resolver: Callable[[], dict[str, Any] | None],
        assignee_resolver: Callable[[str, str | None, str | None], str | None],
        context_summary: Callable[[ChatSession], str],
        pending_question: Callable[[dict[str, Any] | None, StepAgentResult], str],
        step_assignee_user_id: str | None = None,
        binding_default_assignee_user_id: str | None = None,
        step_notify_channel: str | None = None,
        binding_default_notify_channel: str | None = None,
    ) -> HumanHandoffRequest:
        existing = self.db.exec(
            select(HumanHandoffRequest)
            .where(HumanHandoffRequest.tenant_id == tenant_id)
            .where(HumanHandoffRequest.session_id == chat_session.id)
            .where(HumanHandoffRequest.status == "pending")
        ).first()
        if existing:
            chat_session.status = "handoff"
            chat_session.awaiting_input_json = {
                "type": "human_handoff",
                "handoff_id": existing.id,
                "pending_question": existing.pending_question,
            }
            chat_session.updated_at = utc_now()
            return existing

        current_step = current_step_resolver()
        pending_question_text = pending_question(current_step, step_result)
        # Assignee 优先级:SOP 节点指定 → 当前渠道默认处理人 → 数字员工负责人 → 租户管理员。
        # 不再从知识库 Contact 概念推断 assignee(知识内容变化会导致处理人不稳定,
        # 且缺少权限/审计入口)。
        # 通知渠道随命中的配置走:None=默认投递;"web"=仅网页端;绑定渠道=按该渠道转接。
        configured = next(
            (
                (user_id, notify_channel)
                for user_id, notify_channel in (
                    (step_assignee_user_id, step_notify_channel),
                    (binding_default_assignee_user_id, binding_default_notify_channel),
                )
                if self._is_internal_assignee(tenant_id, user_id)
            ),
            None,
        )
        if configured:
            configured_assignee = configured[0]
            assignee_notify_channel = str(configured[1] or "").strip() or None
        else:
            configured_assignee = None
            assignee_notify_channel = None
        assignee_user_id = configured_assignee or assignee_resolver(
            tenant_id, chat_session.agent_id, chat_session.user_id
        )
        handoff = HumanHandoffRequest(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            agent_id=chat_session.agent_id,
            requester_user_id=chat_session.user_id,
            assignee_user_id=assignee_user_id,
            trigger_skill_id=chat_session.active_skill_id,
            trigger_step_id=chat_session.active_step_id,
            context_summary=context_summary(chat_session),
            pending_question=pending_question_text,
            resume_payload_json={
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "slots": chat_session.slots_json or {},
                "pending_tasks": chat_session.pending_tasks_json or [],
            },
            metadata_json={
                "step": current_step or {},
                "step_reply": step_result.reply,
                "step_handoff": step_result.handoff,
                "assignee_notify_channel": assignee_notify_channel,
            },
        )
        self.db.add(handoff)
        chat_session.status = "handoff"
        chat_session.awaiting_input_json = {
            "type": "human_handoff",
            "handoff_id": handoff.id,
            "pending_question": handoff.pending_question,
        }
        chat_session.updated_at = utc_now()
        self.events.record(
            tenant_id,
            chat_session.id,
            "human_handoff_requested",
            {
                "handoff_id": handoff.id,
                "agent_id": handoff.agent_id,
                "assignee_user_id": handoff.assignee_user_id,
                "trigger_skill_id": handoff.trigger_skill_id,
                "trigger_step_id": handoff.trigger_step_id,
                "pending_question": handoff.pending_question,
            },
        )
        return handoff

    def _is_internal_assignee(self, tenant_id: str, user_id: str | None) -> bool:
        if not user_id:
            return False
        user = self.db.get(User, user_id)
        return bool(user and user.tenant_id == tenant_id and user.source == "web")

    def assignee_user_id(
        self,
        tenant_id: str,
        agent_id: str | None,
        fallback_user_id: str | None,
        *,
        tenant_admin_resolver: Callable[[str], str | None],
    ) -> str | None:
        if agent_id:
            agent = self.db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id, AgentProfile.id == agent_id
                )
            ).first()
            metadata = agent.metadata_json if agent else {}
            if isinstance(metadata, dict):
                for key in (
                    "owner_user_id",
                    "created_by_user_id",
                    "creator_user_id",
                    "created_by",
                    "owner_id",
                ):
                    value = metadata.get(key)
                    candidate = str(value or "").strip() or None
                    if self._is_internal_assignee(tenant_id, candidate):
                        return candidate
        admin_user_id = tenant_admin_resolver(tenant_id)
        if self._is_internal_assignee(tenant_id, admin_user_id):
            return admin_user_id
        if self._is_internal_assignee(tenant_id, fallback_user_id):
            return fallback_user_id
        return None

    def tenant_admin_user_id(self, tenant_id: str) -> str | None:
        row = self.db.exec(
            select(User)
            .where(User.tenant_id == tenant_id, User.role == "admin")
            .order_by(User.created_at)
        ).first()
        return row.id if row else None

    def context_summary(self, chat_session: ChatSession) -> str:
        rows = self.db.exec(
            select(Message)
            .where(Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
            .limit(2)
        ).all()
        lines: list[str] = []
        for message in reversed(rows):
            content = re.sub(r"\s+", " ", message.content or "").strip()
            if content:
                lines.append(f"{message.role}: {content[:240]}")
        return "\n".join(lines)

    @staticmethod
    def pending_question(
        current_step: dict[str, Any] | None, step_result: StepAgentResult
    ) -> str:
        candidates: list[Any] = [
            step_result.reply,
            current_step.get("handoff_question") if current_step else None,
            current_step.get("question") if current_step else None,
            current_step.get("name") if current_step else None,
        ]
        for candidate in candidates:
            text = re.sub(r"\s+", " ", str(candidate or "")).strip()
            if text:
                return text[:600]
        return "当前 SOP 需要人工确认后继续执行。"
