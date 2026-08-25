from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app import paths
from app.core.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_pending_tasks,
)
from app.core.graph_rules import GraphRules
from app.core.task_frame_store import MAX_TASK_FRAMES_PER_TURN
from app.db.models import ChatSession, ModelConfig, Skill, new_id
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    TURN_PLANNER_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation, llm_span_attributes
from app.session.session_schema import (
    PendingTask,
    PlannedTaskFrame,
    RouterDecision,
    TaskUpdate,
    TeamPlannerContext,
    TurnPlan,
)
from app.session.slot_policy import strip_router_generated_message_slots


PROMPT_PATH = (
    paths.resource_dir() / "app" / "llm" / "prompts" / "turn_planner_prompt.md"
)
SCHEMA_REPAIR_ATTEMPTS = 1


class TurnPlanner:
    """Single scene/SOP intent planner for the Harness v2 execution path."""

    def plan(
        self,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        task_frame_state: list[dict[str, Any]] | None = None,
        interaction_mode: str = "normal",
        team_context: TeamPlannerContext | None = None,
    ) -> TurnPlan:
        payload = stage_payload(
            phase="TurnPlanner",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data={
                "current_session": _session_payload(
                    session,
                    task_frame_state=task_frame_state,
                ),
                # `Skill` is the historical database model name for an SOP.
                # Keep GeneralSkill selection out of TurnPlanner entirely and
                # make the prompt boundary explicit so the planner cannot
                # confuse runtime capabilities with SOP routing candidates.
                "available_sops": [_sop_payload(skill) for skill in available_skills],
                "interaction_mode": interaction_mode,
                "team_context": (
                    team_context.model_dump(mode="json") if team_context is not None else None
                ),
            },
            output_contract=TURN_PLANNER_OUTPUT_SCHEMA,
        )
        try:
            client = LLMClient(model_config)
            with llm_operation("turn_planner.plan"):
                plan = self._generate_validated_plan(
                    client,
                    unified_system_prompt(),
                    payload,
                )
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Turn Planner returned invalid JSON schema: {exc}") from exc
        return self._normalize(
            plan,
            message,
            session,
            available_skills,
            task_frame_state,
            interaction_mode,
            team_context,
        )

    def _generate_validated_plan(
        self,
        client: LLMClient,
        system_prompt: str,
        payload: dict[str, Any],
    ) -> TurnPlan:
        base_payload = deepcopy(payload)
        next_payload = payload
        max_attempts = SCHEMA_REPAIR_ATTEMPTS + 1
        for attempt in range(max_attempts):
            with llm_span_attributes(
                schema_attempt=attempt + 1,
                schema_retry_count=attempt,
                schema_max_attempts=max_attempts,
            ):
                raw = client.generate_json(system_prompt, next_payload)
            try:
                return TurnPlan.model_validate(raw)
            except ValidationError as exc:
                if attempt >= SCHEMA_REPAIR_ATTEMPTS:
                    raise
                next_payload = deepcopy(base_payload)
                next_payload["_schema_repair"] = {
                    "attempt": attempt + 1,
                    "max_attempts": SCHEMA_REPAIR_ATTEMPTS,
                    "previous_output": raw,
                    "validation_errors": _compact_validation_errors(exc),
                    "instruction": (
                        "上一轮输出是合法 JSON，但不符合输出字段类型。"
                        "请保留原任务语义，修正列出的字段后重新输出完整 JSON object。"
                        "空 object 使用 {}，空 array 使用 []，不要为容器字段输出 null。"
                    ),
                }
        raise AssertionError("unreachable")

    def _normalize(
        self,
        plan: TurnPlan,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        task_frame_state: list[dict[str, Any]] | None = None,
        interaction_mode: str = "normal",
        team_context: TeamPlannerContext | None = None,
    ) -> TurnPlan:
        skills = {skill.skill_id: skill for skill in available_skills}
        known_frames = _known_task_frames(session, task_frame_state)
        frames: list[PlannedTaskFrame] = []
        seen_ids: set[str] = set()
        task_id_map: dict[str, str] = {}
        for raw_frame in plan.task_frames[:MAX_TASK_FRAMES_PER_TURN]:
            frame = raw_frame.model_copy(deep=True)
            requested_task_id = str(frame.task_id or "").strip()
            known = known_frames.get(requested_task_id)
            if known and str(known.get("status") or "") not in {
                "completed",
                "cancelled",
                "failed",
            }:
                frame.task_id = requested_task_id
                frame.kind = (
                    "sop" if known.get("kind") == "sop" else "conversation"
                )
            else:
                frame.task_id = _unique_task_id(None, seen_ids)
            if requested_task_id and requested_task_id not in task_id_map:
                task_id_map[requested_task_id] = frame.task_id
            if frame.kind == "sop":
                if known:
                    frame.target_skill_id = str(
                        known.get("skill_id") or ""
                    ).strip() or None
                    frame.target_step_id = str(
                        known.get("step_id") or ""
                    ).strip() or None
                if not frame.target_skill_id or frame.target_skill_id not in skills:
                    continue
                if known:
                    frame.decision = (
                        "switch_to_pending"
                        if frame.decision == "switch_to_pending"
                        else "continue_active"
                    )
                elif (
                    frame.decision == "continue_active"
                    and frame.target_skill_id == session.active_skill_id
                ):
                    frame.target_step_id = (
                        session.active_step_id
                        or _first_node_id(skills[frame.target_skill_id])
                    )
                else:
                    frame.decision = "start_new_task"
                    frame.target_step_id = _first_node_id(skills[frame.target_skill_id])
            else:
                frame.kind = "conversation"
                frame.target_skill_id = None
                frame.target_step_id = None
                if plan.decision == "handoff_human":
                    frame.decision = "handoff_human"
                elif frame.decision not in {"answer_only", "clarify"}:
                    frame.decision = "answer_only"
            frame.task_id = _unique_task_id(frame.task_id, seen_ids)
            seen_ids.add(frame.task_id)
            frame.slot_hints = strip_router_generated_message_slots(frame.slot_hints)
            frame.requirements = _requirements(
                [
                    *(
                        known.get("requirements") or []
                        if isinstance(known, dict)
                        else []
                    ),
                    *frame.requirements,
                ],
                frame.user_intent or plan.user_intent or message,
            )
            frame.user_intent = _one_line(
                frame.user_intent or frame.requirements[0]
            )
            frame.source_message = message
            frames.append(frame)

        if not frames:
            active_skill = skills.get(session.active_skill_id or "")
            if plan.decision == "continue_active" and active_skill is not None:
                active_known = next(
                    (
                        item
                        for item in known_frames.values()
                        if item.get("active")
                        and item.get("kind") == "sop"
                        and item.get("skill_id") == active_skill.skill_id
                    ),
                    None,
                )
                frames.append(
                    PlannedTaskFrame(
                        task_id=(
                            str(active_known.get("task_id"))
                            if active_known
                            else _unique_task_id(None, seen_ids)
                        ),
                        kind="sop",
                        decision="continue_active",
                        target_skill_id=active_skill.skill_id,
                        target_step_id=session.active_step_id
                        or _first_node_id(active_skill),
                        user_intent=_one_line(plan.user_intent or message),
                        requirements=_requirements([], plan.user_intent or message),
                        source_message=message,
                    )
                )
            elif plan.decision == "handoff_human" and active_skill is not None:
                handoff_step_id = _find_handoff_node_id(
                    active_skill, session.active_step_id
                )
                if handoff_step_id:
                    frames.append(
                        PlannedTaskFrame(
                            task_id=_unique_task_id(None, seen_ids),
                            kind="sop",
                            decision="handoff_human",
                            target_skill_id=active_skill.skill_id,
                            target_step_id=handoff_step_id,
                            user_intent=_one_line(plan.user_intent or message),
                            requirements=_requirements(
                                [], plan.user_intent or message
                            ),
                            source_message=message,
                        )
                    )
                else:
                    active_conversation = next(
                        (
                            item
                            for item in known_frames.values()
                            if item.get("active")
                            and item.get("kind") == "conversation"
                        ),
                        None,
                    )
                    frames.append(
                        PlannedTaskFrame(
                            task_id=(
                                str(active_conversation.get("task_id"))
                                if active_conversation
                                else _unique_task_id(None, seen_ids)
                            ),
                            kind="conversation",
                            decision="handoff_human",
                            user_intent=_one_line(plan.user_intent or message),
                            requirements=_requirements(
                                (
                                    list(
                                        active_conversation.get("requirements") or []
                                    )
                                    if active_conversation
                                    else []
                                ),
                                plan.user_intent or message,
                            ),
                            source_message=message,
                        )
                    )
            else:
                active_conversation = next(
                    (
                        item
                        for item in known_frames.values()
                        if item.get("active")
                        and item.get("kind") == "conversation"
                    ),
                    None,
                )
                frames.append(
                    PlannedTaskFrame(
                        task_id=(
                            str(active_conversation.get("task_id"))
                            if active_conversation
                            else _unique_task_id(None, seen_ids)
                        ),
                        kind="conversation",
                        decision=(
                            "clarify" if plan.decision == "clarify" else "answer_only"
                        ),
                        user_intent=_one_line(plan.user_intent or message),
                        requirements=_requirements(
                            (
                                list(active_conversation.get("requirements") or [])
                                if active_conversation
                                else []
                            ),
                            plan.user_intent or message,
                        ),
                        source_message=message,
                    )
                )

        valid_ids = {frame.task_id for frame in frames if frame.task_id}
        for frame in frames:
            frame.depends_on_task_ids = [
                task_id_map.get(task_id, task_id)
                for task_id in _unique_text(frame.depends_on_task_ids)
                if task_id_map.get(task_id, task_id) in valid_ids
                and task_id_map.get(task_id, task_id) != frame.task_id
            ]
        frames = _normalize_execution_targets(
            frames,
            interaction_mode=interaction_mode,
            team_context=team_context,
        )

        first = frames[0]
        if plan.decision == "complete_task":
            selected_task_id = str(plan.selected_task_id or "").strip()
            plan.selected_task_id = (
                selected_task_id
                if selected_task_id in known_frames
                else None
            )
        elif first.kind == "sop":
            plan.decision = first.decision
            plan.selected_task_id = (
                first.task_id if first.decision == "switch_to_pending" else None
            )
        elif plan.decision not in {"clarify", "handoff_human", "complete_task"}:
            plan.decision = "answer_only"
            plan.selected_task_id = None
        plan.task_frames = frames
        plan.task_updates = _sanitize_task_updates(plan, known_frames)
        plan.user_intent = _one_line(plan.user_intent or first.user_intent or message)
        return plan


def _compact_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = error.get("loc") or ()
        compact.append(
            {
                "path": ".".join(str(part) for part in location),
                "type": str(error.get("type") or "validation_error"),
                "message": str(error.get("msg") or "Invalid value"),
            }
        )
    return compact


def _normalize_execution_targets(
    frames: list[PlannedTaskFrame],
    *,
    interaction_mode: str,
    team_context: TeamPlannerContext | None,
) -> list[PlannedTaskFrame]:
    """Validate planner-selected assignees without guessing team membership."""
    leader_id = team_context.leader_agent_id if team_context is not None else ""
    member_ids = {
        member.agent_id
        for member in (team_context.members if team_context is not None else [])
        if member.agent_id and member.agent_id != leader_id
    }
    for frame in frames:
        assignee_id = str(frame.assignee_agent_id or "").strip()
        is_valid_remote = (
            interaction_mode == "team_tl"
            and team_context is not None
            and frame.kind == "conversation"
            and frame.execution_target == "team_member"
            and assignee_id in member_ids
        )
        if is_valid_remote:
            frame.execution_target = "team_member"
            frame.assignee_agent_id = assignee_id
        else:
            frame.execution_target = "self"
            frame.assignee_agent_id = None
            frame.activation_condition = {}

    # A TeamTask can depend only on other TeamTasks published in this batch.
    # If a model points a remote frame at a leader-local frame, keep it local
    # rather than creating a permanently blocked task with an unresolvable ID.
    while True:
        remote_ids = {
            frame.task_id
            for frame in frames
            if frame.execution_target == "team_member" and frame.task_id
        }
        changed = False
        for frame in frames:
            if frame.execution_target != "team_member":
                continue
            if any(task_id not in remote_ids for task_id in frame.depends_on_task_ids):
                frame.execution_target = "self"
                frame.assignee_agent_id = None
                frame.activation_condition = {}
                changed = True
        if not changed:
            break

    for frame in frames:
        if frame.execution_target != "team_member" or not frame.depends_on_task_ids:
            frame.activation_condition = {}
        elif not frame.activation_condition:
            frame.activation_condition = {"type": "all_succeeded"}
    return frames


def turn_plan_router_decision(plan: TurnPlan) -> RouterDecision:
    first = plan.task_frames[0] if plan.task_frames else None
    first_sop = first if first and first.kind == "sop" else None
    sop_frames = [
        PendingTask(
            task_id=frame.task_id,
            decision=frame.decision,
            target_skill_id=frame.target_skill_id,
            target_step_id=frame.target_step_id,
            confidence=plan.confidence,
            user_intent=frame.user_intent,
            reason=plan.reason,
            source_message=frame.source_message,
            slot_hints=dict(frame.slot_hints),
        )
        for frame in plan.task_frames
        if frame.kind == "sop"
    ]
    return RouterDecision(
        decision=(
            first_sop.decision
            if first_sop is not None
            else (
                plan.decision
                if plan.decision
                in {"answer_only", "clarify", "handoff_human", "complete_task"}
                else "answer_only"
            )
        ),
        selected_task_id=plan.selected_task_id,
        target_skill_id=first_sop.target_skill_id if first_sop else None,
        target_step_id=first_sop.target_step_id if first_sop else None,
        confidence=plan.confidence,
        user_intent=plan.user_intent,
        reason=plan.reason,
        clarification_question=plan.clarification_question,
        slot_hints=dict(first_sop.slot_hints) if first_sop else {},
        task_frames=sop_frames,
        task_updates=plan.task_updates,
    )


def _first_node_id(skill: Skill) -> str | None:
    content = skill.content_json or {}
    start = str(content.get("start_node_id") or "").strip()
    if start:
        return start
    for node in content.get("nodes") or []:
        if isinstance(node, dict):
            node_id = str(node.get("node_id") or "").strip()
            if node_id:
                return node_id
    return None


def _find_handoff_node_id(
    skill: Skill, active_step_id: str | None = None
) -> str | None:
    """查找 SOP 中从当前节点可达的 handoff 节点。

    使用 GraphRules.find_handoff_node_id 做基于 edges 的 BFS,
    优先返回从 active_step_id 可达的 handoff 节点,而非数组顺序的第一个。
    """
    content = skill.content_json or {}
    return GraphRules.find_handoff_node_id(content, active_step_id)


def _known_task_frames(
    session: ChatSession,
    task_frame_state: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    known: dict[str, dict[str, Any]] = {}
    for raw in [
        *(task_frame_state or []),
        *(session.pending_tasks_json or []),
    ]:
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id:
            continue
        item = dict(raw)
        item["task_id"] = task_id
        item.setdefault(
            "kind",
            "sop"
            if item.get("skill_id") or item.get("target_skill_id")
            else "conversation",
        )
        item.setdefault(
            "skill_id",
            item.get("target_skill_id"),
        )
        item.setdefault(
            "step_id",
            item.get("target_step_id"),
        )
        item.setdefault(
            "requirements",
            [item.get("user_intent") or item.get("intent_summary")]
            if item.get("user_intent") or item.get("intent_summary")
            else [],
        )
        known[task_id] = item
    return known


def _sanitize_task_updates(
    plan: TurnPlan,
    known_frames: dict[str, dict[str, Any]],
) -> list[TaskUpdate]:
    updates: list[TaskUpdate] = []
    seen: set[str] = set()
    for raw in plan.task_updates:
        task_id = str(raw.task_id or "").strip()
        if not task_id or task_id in seen or task_id not in known_frames:
            continue
        seen.add(task_id)
        updates.append(
            TaskUpdate(
                task_id=task_id,
                status="queued" if raw.status == "queued" else None,
                user_intent=_one_line(raw.user_intent) or None,
                reason=_one_line(raw.reason) or None,
                slot_hints=strip_router_generated_message_slots(
                    dict(raw.slot_hints or {})
                ),
                remove=bool(raw.remove),
            )
        )
    return updates


def _session_payload(
    session: ChatSession,
    *,
    task_frame_state: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _without_empty(
        {
            "active_skill_id": session.active_skill_id,
            "active_step_id": session.active_step_id,
            "slots": session.slots_json or {},
            "pending_tasks": compact_pending_tasks(session.pending_tasks_json),
            "awaiting_input": compact_awaiting_input(session.awaiting_input_json),
            "task_frames": list(task_frame_state or []),
            "status": session.status,
        }
    )


def _sop_payload(skill: Skill) -> dict[str, Any]:
    content = skill.content_json or {}
    return _without_empty(
        {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "trigger_intents": content.get("trigger_intents", []),
        }
    )


def _unique_task_id(value: str | None, seen: set[str]) -> str:
    candidate = str(value or "").strip()
    if candidate and candidate not in seen:
        return candidate
    candidate = new_id("task")
    while candidate in seen:
        candidate = new_id("task")
    return candidate


def _requirements(values: list[str], fallback: str) -> list[str]:
    requirements = _unique_text(values)
    fallback_text = _one_line(fallback)
    if not requirements and fallback_text:
        requirements.append(fallback_text)
    return requirements or ["完成用户本轮请求。"]


def _unique_text(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _one_line(value)
        if text and text not in result:
            result.append(text)
    return result


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
