from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

from app.db.models import ChatSession, ModelConfig, Skill
from app.llm import LLMClient, LLMError
from app.session.helpers import public_session
from app.session.session_schema import RouterDecision, RouterDecisionValue, TaskScheduleDecision


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "router_prompt.md"
TASK_SCHEDULER_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "task_scheduler_prompt.md"
ALLOWED_DECISIONS = set(get_args(RouterDecisionValue))


class Router:
    def decide(
        self,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> RouterDecision:
        payload = {
            "user_message": message,
            "conversation_context": conversation_context or {},
            "memory_context": memory_context or [],
            "current_session": public_session(session).model_dump(),
            "available_skills": [
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "business_domain": skill.content_json.get("business_domain"),
                    "trigger_intents": skill.content_json.get("trigger_intents", []),
                    "required_info": skill.content_json.get("required_info", []),
                    "steps": [
                        {
                            "step_id": step.get("step_id"),
                            "name": step.get("name"),
                            "instruction": step.get("instruction"),
                            "expected_user_info": step.get("expected_user_info", []),
                            "allowed_actions": step.get("allowed_actions", []),
                        }
                        for step in skill.content_json.get("steps", [])
                        if isinstance(step, dict)
                    ],
                }
                for skill in available_skills
            ],
        }
        try:
            raw = LLMClient(model_config).generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
            raw = self._coerce_raw_decision(raw)
            decision = RouterDecision.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Router returned invalid JSON schema: {exc}") from exc
        return self._normalize_decision(decision, session, available_skills)

    def schedule_tasks_after_completion(
        self,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        completed_reply: str | None = None,
    ) -> TaskScheduleDecision:
        candidate_frames = self._candidate_task_frames(session)
        payload = {
            "user_message": message,
            "completed_reply": completed_reply or "",
            "conversation_context": conversation_context or {},
            "memory_context": memory_context or [],
            "current_session": public_session(session).model_dump(),
            "candidate_task_frames": candidate_frames,
            "available_skills": [
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "business_domain": skill.content_json.get("business_domain"),
                    "trigger_intents": skill.content_json.get("trigger_intents", []),
                    "required_info": skill.content_json.get("required_info", []),
                    "steps": [
                        {
                            "step_id": step.get("step_id"),
                            "name": step.get("name"),
                            "instruction": step.get("instruction"),
                            "expected_user_info": step.get("expected_user_info", []),
                            "allowed_actions": step.get("allowed_actions", []),
                        }
                        for step in skill.content_json.get("steps", [])
                        if isinstance(step, dict)
                    ],
                }
                for skill in available_skills
            ],
        }
        try:
            raw = LLMClient(model_config).generate_json(
                TASK_SCHEDULER_PROMPT_PATH.read_text(encoding="utf-8"),
                payload,
            )
            raw = self._coerce_raw_schedule(raw)
            schedule = TaskScheduleDecision.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Task scheduler returned invalid JSON schema: {exc}") from exc
        return self._normalize_schedule(schedule, candidate_frames)

    def _coerce_raw_decision(self, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        normalized = dict(raw)
        decision = str(normalized.get("decision") or "").strip()
        aliases = {
            "answer": "answer_only",
            "answer_user": "answer_only",
            "chat": "answer_only",
            "chitchat": "answer_only",
            "smalltalk": "answer_only",
            "no_skill": "answer_only",
            "none": "answer_only",
            "continue_active_skill": "continue_active",
            "switch_pending": "switch_to_pending",
            "start_new_skill": "start_new_task",
            "handoff_to_human": "handoff",
            "transfer_to_human": "handoff",
        }
        if decision in aliases:
            normalized["decision"] = aliases[decision]
        elif decision == "":
            normalized["decision"] = "clarify"
        elif decision not in ALLOWED_DECISIONS:
            normalized["decision"] = "clarify"
            normalized.setdefault("reason", f"Router returned unsupported decision: {decision}")
            normalized.setdefault("clarification_question", "请问您想办理哪类业务？")
        return normalized

    def _coerce_raw_schedule(self, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        normalized = dict(raw)
        action = str(normalized.get("action") or "").strip()
        selected_ids: list[str] = []
        raw_selected_ids = normalized.get("selected_task_ids")
        if isinstance(raw_selected_ids, list):
            selected_ids.extend(str(item).strip() for item in raw_selected_ids if str(item).strip())
        raw_selected_tasks = normalized.get("selected_tasks")
        if isinstance(raw_selected_tasks, list):
            for item in raw_selected_tasks:
                if isinstance(item, dict) and item.get("task_id"):
                    selected_ids.append(str(item["task_id"]).strip())
                elif isinstance(item, str) and item.strip():
                    selected_ids.append(item.strip())
        deduped_ids: list[str] = []
        seen: set[str] = set()
        for task_id in selected_ids:
            if task_id and task_id not in seen:
                deduped_ids.append(task_id)
                seen.add(task_id)
        normalized["selected_task_ids"] = deduped_ids
        if action in {"run", "continue", "continue_tasks", "schedule"}:
            normalized["action"] = "run_tasks"
        elif action not in {"run_tasks", "stop"}:
            normalized["action"] = "run_tasks" if deduped_ids else "stop"
        return normalized

    def _normalize_decision(
        self, decision: RouterDecision, session: ChatSession, available_skills: list[Skill]
    ) -> RouterDecision:
        skills = {skill.skill_id: skill for skill in available_skills}
        if decision.target_skill_id and decision.target_skill_id not in skills:
            decision.target_skill_id = None
            decision.target_step_id = None
        if decision.awaiting_input and decision.awaiting_input.skill_id not in {None, *skills.keys()}:
            decision.awaiting_input = None
        if decision.decision in {"start_skill", "start_new_task", "suspend_current_and_start_new_skill"}:
            if not decision.target_skill_id or decision.target_skill_id not in skills:
                decision.decision = "clarify"
                decision.target_skill_id = None
                decision.target_step_id = None
                decision.clarification_question = "请问您想办理哪类业务？"
                return decision
        if decision.decision == "switch_to_pending":
            pending_ids = {
                str(task.get("task_id"))
                for task in [*(session.pending_tasks_json or []), *(session.skill_stack_json or [])]
                if isinstance(task, dict) and task.get("task_id")
            }
            if not decision.selected_task_id or decision.selected_task_id not in pending_ids:
                decision.decision = "clarify"
                decision.clarification_question = "请问您想继续哪一项待处理任务？"
                return decision
        if not decision.target_skill_id and session.active_skill_id:
            decision.target_skill_id = session.active_skill_id
        if decision.target_skill_id and not decision.target_step_id:
            target_skill = skills.get(decision.target_skill_id)
            steps: list[dict[str, Any]] = target_skill.content_json.get("steps", []) if target_skill else []
            if steps:
                decision.target_step_id = steps[0].get("step_id")
        normalized_tasks = self._normalize_tasks(decision.pending_tasks, skills)
        decision.pending_tasks = normalized_tasks
        decision.created_tasks = self._normalize_tasks(decision.created_tasks, skills)
        return decision

    def _normalize_tasks(self, tasks, skills: dict[str, Skill]):
        normalized_tasks = []
        for task in tasks:
            if not task.target_skill_id or task.target_skill_id not in skills:
                continue
            if not task.target_step_id:
                target_skill = skills.get(task.target_skill_id)
                steps = target_skill.content_json.get("steps", []) if target_skill else []
                if steps:
                    task.target_step_id = steps[0].get("step_id")
            normalized_tasks.append(task)
        return normalized_tasks

    def _normalize_schedule(
        self, schedule: TaskScheduleDecision, candidate_frames: list[dict[str, Any]]
    ) -> TaskScheduleDecision:
        valid_ids = {
            str(frame.get("task_id"))
            for frame in candidate_frames
            if isinstance(frame, dict) and frame.get("task_id")
        }
        selected_ids: list[str] = []
        for task_id in schedule.selected_task_ids:
            if task_id in valid_ids and task_id not in selected_ids:
                selected_ids.append(task_id)
        schedule.selected_task_ids = selected_ids
        if not selected_ids:
            schedule.action = "stop"
        return schedule

    def _candidate_task_frames(self, session: ChatSession) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for source, raw_frames in (
            ("pending", session.pending_tasks_json or []),
            ("paused", session.skill_stack_json or []),
        ):
            for frame in raw_frames:
                if not isinstance(frame, dict) or not frame.get("task_id"):
                    continue
                frames.append(
                    {
                        "source": source,
                        "task_id": frame.get("task_id"),
                        "status": frame.get("status"),
                        "skill_id": frame.get("skill_id") or frame.get("target_skill_id"),
                        "step_id": frame.get("step_id") or frame.get("target_step_id"),
                        "slots": frame.get("slots") if isinstance(frame.get("slots"), dict) else {},
                        "intent_summary": frame.get("intent_summary") or frame.get("user_intent"),
                        "source_message": frame.get("source_message"),
                        "parent_task_id": frame.get("parent_task_id"),
                        "resume_policy": frame.get("resume_policy"),
                        "created_at": frame.get("created_at"),
                        "updated_at": frame.get("updated_at"),
                    }
                )
        return frames
