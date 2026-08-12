from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.db.models import ChatSession, Message, Skill
from app.knowledge.citations import knowledge_citations_from_results
from app.session.attachments import (
    message_content_with_attachment_context,
    message_images_from_metadata,
)
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult


class ConversationProjection:
    @staticmethod
    def message_context_entry(row: Message) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": row.id,
            "role": row.role,
            "content": message_content_with_attachment_context(row.content, row.metadata_json),
            "created_at": row.created_at,
        }
        images = message_images_from_metadata(row.metadata_json)
        if images and row.role == "user":
            entry["images"] = images
        return entry

    @staticmethod
    def context_compacted_now(context: dict[str, object] | None) -> bool:
        if not isinstance(context, dict):
            return False
        metadata = context.get("metadata")
        return isinstance(metadata, dict) and metadata.get("compacted_now") is True

    @classmethod
    def assistant_message_metadata(
        cls,
        step_result: StepAgentResult | None,
        *,
        citation_deduper: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        knowledge_results = list(step_result.knowledge_results or []) if step_result else []
        dedupe = citation_deduper or cls.dedupe_knowledge_citations
        citations = dedupe(knowledge_citations_from_results(knowledge_results))
        if not citations:
            return {}
        latest_query = next(
            (
                item.get("query")
                for item in reversed(knowledge_results)
                if isinstance(item.get("query"), dict)
            ),
            None,
        )
        return {
            "knowledge_citations": citations,
            "knowledge_query": latest_query or {},
        }

    @staticmethod
    def dedupe_knowledge_citations(
        citations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            identity = str(
                citation.get("title")
                or citation.get("section_path")
                or citation.get("summary")
                or citation.get("excerpt")
                or citation.get("source_path")
                or citation.get("concept_id")
                or citation.get("id")
                or ""
            )
            key = re.sub(r"\s+", " ", identity).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append({**citation, "label": f"[{len(result) + 1}]"})
            if len(result) >= 4:
                break
        return result

    @staticmethod
    def user_message_metadata(request: ChatTurnRequest) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if request.client_turn_id:
            metadata["client_turn_id"] = request.client_turn_id
        if request.interaction_mode != "normal":
            metadata["interaction_mode"] = request.interaction_mode
        if request.model_config_id:
            metadata["model_config_id"] = request.model_config_id
        if request.attachments:
            metadata["attachments"] = [item.model_dump(mode="json") for item in request.attachments]
        return metadata

    @staticmethod
    def runtime_stream_context(
        decision: RouterDecision,
        before_skill: str | None,
        before_step: str | None,
        chat_session: ChatSession,
    ) -> dict[str, object]:
        return {
            "runtimeDecision": decision.decision,
            "fromSkillId": before_skill,
            "fromStepId": before_step,
            "toSkillId": chat_session.active_skill_id,
            "toStepId": chat_session.active_step_id,
        }

    @staticmethod
    def skill_state_payload(
        chat_session: ChatSession,
        skills: list[Skill],
        runtime_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        skill_names = {skill.skill_id: skill.name for skill in skills}
        visible_skill_ids = set(skill_names)
        current_skills: list[dict[str, object]] = []
        active_skill_id = (
            chat_session.active_skill_id
            if chat_session.active_skill_id in visible_skill_ids
            else None
        )
        if active_skill_id:
            current_skills.append(
                {
                    "skillId": active_skill_id,
                    "name": skill_names.get(active_skill_id, active_skill_id),
                    "stepId": chat_session.active_step_id,
                    "state": "active",
                }
            )
        for task in chat_session.pending_tasks_json or []:
            if not isinstance(task, dict):
                continue
            skill_id = str(task.get("target_skill_id") or task.get("skill_id") or "").strip()
            if not skill_id or skill_id not in visible_skill_ids:
                continue
            current_skills.append(
                {
                    "skillId": skill_id,
                    "name": skill_names.get(skill_id, skill_id),
                    "stepId": task.get("target_step_id") or task.get("step_id"),
                    "state": task.get("status") or "pending",
                }
            )
        return {
            "activeSkillId": active_skill_id,
            "activeStepId": chat_session.active_step_id if active_skill_id else None,
            "currentSkills": current_skills,
            **(runtime_context or {}),
        }

    @staticmethod
    def fallback_session_title(message: str) -> str:
        title = re.sub(r"\s+", " ", message).strip().strip("。！？!?")
        return title[:28] if title else ""

    @staticmethod
    def normalize_reply_citation_labels(reply: str, citations: object) -> str:
        if not isinstance(citations, list) or not citations:
            return reply
        max_label = len(citations)

        def replace(match: re.Match[str]) -> str:
            try:
                value = int(match.group(1))
            except ValueError:
                return match.group(0)
            if 1 <= value <= max_label:
                return match.group(0)
            return f"[{max(1, max_label)}]"

        return re.sub(r"\[(\d+)\]", replace, reply)

    @staticmethod
    def strip_trailing_citation_summary(reply: str) -> str:
        return re.sub(
            r"(?:\n|\s){0,3}(?:参考资料|引用来源|资料来源)\s*[:：]\s*(?:\[\d+\]\s*)+$",
            "",
            reply.rstrip(),
        ).rstrip()
