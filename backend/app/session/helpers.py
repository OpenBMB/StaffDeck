from __future__ import annotations

from app.db.models import ChatSession
from app.session.session_schema import SessionPublic


def public_session(session: ChatSession) -> SessionPublic:
    return SessionPublic(
        session_id=session.id,
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        title=session.title,
        active_skill_id=session.active_skill_id,
        active_step_id=session.active_step_id,
        slots=session.slots_json or {},
        skill_stack=session.skill_stack_json or [],
        pending_tasks=session.pending_tasks_json or [],
        resume_after_answer=session.resume_after_answer_json,
        awaiting_input=session.awaiting_input_json,
        knowledge_context=session.knowledge_context_json or [],
        summary=session.summary,
        last_agent_question=session.last_agent_question,
        status=session.status,
    )
