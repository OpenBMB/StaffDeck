"""SOP lifecycle implementation owned by the modular runtime, independent of AgentLoop.

No model or Harness v3 engine dependencies. Session/Skill rows are existing persistence contracts;
handoff is an injected collaboration port. The coordinator owns the transaction/CAS.
"""

from __future__ import annotations

from typing import Any
from app.agents.branching import visible_published_skills, visible_skill
from app.db.models import ChatSession, Skill, utc_now
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult
from staffdeck_harness.sop.graph import GraphRules
from staffdeck_harness.sop.finalizer import TurnFinalizer, ExecutionFinalizeState
from staffdeck_harness.sop.state import SopSessionState

GRAPH_PENDING_STEPS_SLOT = "_graph_pending_steps"


def _find_handoff_node_id_in_skill(skill, active_step_id=None):
    return GraphRules.find_handoff_node_id(skill.content_json or {}, active_step_id)


class SopRuntime:
    def __init__(self, db, events, *, create_handoff):
        self.db = db
        self.events = events
        self.create_handoff = create_handoff
        self.runtime = SopSessionState()

    def restore_task_frame(self, session, frame):
        return self.runtime.restore_task_frame(session, frame)

    def complete_current_skill(self, session):
        return self.runtime.complete_current_skill(session)

    def activate_frame(self, session, row, skills):
        if row.tenant_id != session.tenant_id or row.session_id != session.id:
            raise ValueError("SOP execution instance scope mismatch")
        if row.kind != "sop":
            return None
        skill = next((s for s in skills if s.skill_id == row.skill_id), None)
        if skill is None:
            return None
        event = {"start_new_task": "skill_started", "switch_to_pending": "skill_resumed"}.get(
            row.decision
        )
        if event:
            self.events.record(
                session.tenant_id,
                session.id,
                event,
                {
                    "decision": row.decision,
                    "from_skill_id": session.active_skill_id,
                    "to_skill_id": skill.skill_id,
                    "from_skill_version": None,
                    "to_skill_version": skill.version,
                    "from_step_id": session.active_step_id,
                    "to_step_id": row.step_id,
                    "task_frame_id": row.task_id,
                    "execution_engine": getattr(self.events, "execution_engine", None)
                    or "harness_v2",
                },
            )
        self.restore_task_frame(
            session,
            {
                "task_id": row.task_id,
                "skill_id": row.skill_id,
                "step_id": row.step_id,
                "slots": dict(row.slots_json or {}),
                "awaiting_input": {},
            },
        )
        return skill

    def after_execution(
        self, tenant_id, session, skill, requirement, result, router_decision, *, remaining_actions
    ):
        from staffdeck_harness.sop.contracts import SopAdvance
        from staffdeck_harness.sop.results import (
            enforce_required_slots,
            step_result,
            append_session_handoff_artifact,
        )

        if tenant_id != session.tenant_id:
            raise ValueError("SOP result scope mismatch")
        enforce_required_slots(result, requirement, session)
        if result.status == "completed" and not result.next_step_id and skill is not None:
            next_node = self.default_next_step(skill, session.active_step_id)
            if next_node:
                result.next_step_id = (
                    str(next_node.get("step_id") or next_node.get("node_id") or "").strip() or None
                )
        projected = step_result(result)
        previous_node = session.active_step_id
        self.apply_step_result(tenant_id, session, projected, skill)
        state = self.finalize_execution_after_reply(
            tenant_id,
            session,
            skill,
            router_decision,
            projected,
            None,
        )
        if state == "handoff":
            result.status = "handoff"
            append_session_handoff_artifact(result, session)
        elif result.status == "handoff":
            result.status = "failed"
            result.error = {
                "code": "HANDOFF_NOT_ALLOWED",
                "message": "当前 SOP 步骤未声明转人工能力。",
            }
        elif state == "completed":
            result.status = "completed"
        elif result.status == "completed":
            if (
                remaining_actions > 0
                and session.active_skill_id
                and session.active_step_id != previous_node
            ):
                return SopAdvance(projected, True)
            result.status = "action_budget"
        return SopAdvance(projected, False)

    def current_step_allows_human_handoff(
        self, skill: Skill | None, active_step_id: str | None
    ) -> bool:
        if not skill:
            return False
        current_step = self.current_skill_step(skill, active_step_id)
        return bool(current_step and self.step_declares_human_handoff(current_step))

    def maybe_route_to_handoff_node(
        self, chat_session: ChatSession, active_skill: Skill | None
    ) -> bool:
        """当 step_result.handoff=True 但当前 step 不声明 handoff 时,
        查找 SOP 中的 handoff 节点并路由到它。这使得后续的
        _create_human_handoff_request 能从 handoff 节点读取 assignee_user_id。

        返回 True 表示已路由到 handoff 节点。
        """
        if not active_skill or not chat_session.active_skill_id:
            return False
        current_step = self.current_skill_step(active_skill, chat_session.active_step_id)
        if current_step and self.step_declares_human_handoff(current_step):
            return False
        handoff_step_id = _find_handoff_node_id_in_skill(active_skill, chat_session.active_step_id)
        if not handoff_step_id:
            return False
        self.change_active_step(
            chat_session.tenant_id,
            chat_session,
            handoff_step_id,
            reason="handoff_node_routed_by_step_result",
        )
        return True

    def step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        node_type = str(step.get("type") or "").strip()
        return node_type == "handoff" or "handoff_human" in self.step_actions(step)

    def step_actions(self, step: dict[str, Any]) -> list[str]:
        return GraphRules.step_actions(step)

    def finish_stale_completed_skill(
        self, tenant_id: str, chat_session: ChatSession, skills: list[Skill]
    ) -> None:
        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            chat_session.updated_at = utc_now()
        active_skill = next(
            (skill for skill in skills if skill.skill_id == chat_session.active_skill_id), None
        )
        if active_skill and self.is_terminal_skill_state(active_skill, chat_session):
            self.complete_active_skill(
                tenant_id, chat_session, active_skill, "stale_terminal_state"
            )

    def should_complete_skill(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        # Graph topology is authoritative for SOP completion. A non-terminal
        # node may allow an interim reply and all global slots may already be
        # filled, but an outgoing edge still means the workflow has work left.
        # Check this before the reply/tool completion shortcuts so transitioning
        # into an intermediate node cannot finish the entire SOP.
        if self.graph_flow_has_unfinished_work(skill, chat_session, step_result):
            return False
        if (
            tool_result
            and tool_result.success
            and self.current_step_can_finish_after_tool(skill, chat_session)
        ):
            return True
        if self.graph_pending_steps(chat_session):
            return False
        if self.is_answer_ready_skill_state(skill, chat_session):
            return True
        if self.is_terminal_skill_state(skill, chat_session):
            return True
        if not step_result.next_step_id and not step_result.tool_call:
            return True
        return self.is_terminal_skill_state(skill, chat_session)

    def is_terminal_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self.is_terminal_skill_position(
            skill, chat_session.active_step_id, chat_session.slots_json or {}
        )

    def is_answer_ready_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self.current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self.step_actions(step)
        if not self.actions_allow_final_reply(actions):
            return False
        required = [str(field) for field in (skill.content_json or {}).get("required_info", [])]
        return all(
            self.skill_slot_satisfied(chat_session.slots_json or {}, field) for field in required
        )

    def graph_flow_has_unfinished_work(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult | None = None,
    ) -> bool:
        if not skill or chat_session.active_skill_id != skill.skill_id:
            return False
        if self.graph_pending_steps(chat_session):
            return True
        if (
            step_result
            and step_result.next_step_id
            and str(step_result.next_step_id) == str(chat_session.active_step_id)
        ):
            return True
        if not chat_session.active_step_id:
            return False
        return bool(self.graph_outgoing_edges(skill).get(chat_session.active_step_id))

    def is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        if not active_step_id:
            return False
        content = skill.content_json or {}
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        return GraphRules.terminal_position_from_step(
            content,
            active_step_id,
            slots,
            self.current_skill_step(skill, active_step_id),
            self.skill_slot_satisfied,
            self.step_actions,
        )

    def current_step_can_finish_after_tool(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self.current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self.step_actions(step)
        if not self.actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self.skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def actions_allow_final_reply(self, actions: list[str]) -> bool:
        return GraphRules.actions_allow_final_reply(actions)

    def complete_active_skill(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill, reason: str
    ) -> None:
        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.complete_current_skill(chat_session)
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_completed",
            {
                "skill_id": before_skill or skill.skill_id,
                "step_id": before_step,
                "reason": reason,
                "resumed_skill_id": chat_session.active_skill_id,
                "resumed_step_id": chat_session.active_step_id,
            },
        )

    def finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        return TurnFinalizer.finalize(
            tenant_id,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            current_step_allows_handoff=self.current_step_allows_human_handoff,
            route_to_handoff_node=self.maybe_route_to_handoff_node,
            create_handoff=self.create_handoff,
            record_event=self.events.record,
            should_complete=self.should_complete_skill,
            complete_skill=self.complete_active_skill,
        )

    def apply_step_result(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None = None,
    ) -> None:
        source_skill_id = chat_session.active_skill_id
        source_step_id = chat_session.active_step_id
        if step_result.slot_updates:
            chat_session.slots_json = {
                **(chat_session.slots_json or {}),
                **step_result.slot_updates,
            }
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        active_skill_matches = bool(
            active_skill and active_skill.skill_id == chat_session.active_skill_id
        )
        invalid_next_step = False
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            if not self.skill_has_step(active_skill, next_step_id):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "step_agent_result_repaired",
                    {
                        "mode": "invalid_next_step_ignored",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "invalid_next_step_id": step_result.next_step_id,
                    },
                )
                step_result.next_step_id = None
                step_result.is_step_completed = False
                invalid_next_step = True

        self.sync_awaiting_input_from_step_result(
            chat_session,
            step_result,
            active_skill,
            source_skill_id=source_skill_id,
            source_step_id=source_step_id,
        )

        if not chat_session.active_skill_id:
            return
        if invalid_next_step:
            return
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            source_step_id = chat_session.active_step_id
            pending_steps = self.graph_pending_steps(chat_session)
            if pending_steps:
                if next_step_id in pending_steps:
                    pending_steps = [item for item in pending_steps if item != next_step_id]
                    self.store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                    self.change_active_step(
                        tenant_id,
                        chat_session,
                        next_step_id,
                        reason="graph_merge_step",
                    )
                    return

                if next_step_id not in pending_steps:
                    pending_steps.append(next_step_id)
                    self.store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                if self.activate_next_pending_graph_step(
                    tenant_id,
                    chat_session,
                    active_skill,
                    reason="graph_sibling_step",
                ):
                    step_result.next_step_id = chat_session.active_step_id
                return

            self.queue_graph_sibling_steps(
                tenant_id,
                chat_session,
                active_skill,
                source_step_id,
                next_step_id,
            )

        if step_result.next_step_id:
            self.change_active_step(tenant_id, chat_session, str(step_result.next_step_id).strip())
            return

        if active_skill_matches and step_result.is_step_completed:
            if self.activate_next_pending_graph_step(
                tenant_id,
                chat_session,
                active_skill,
                reason="graph_pending_step",
            ):
                step_result.next_step_id = chat_session.active_step_id

    def sync_awaiting_input_from_step_result(
        self,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None,
        *,
        source_skill_id: str | None,
        source_step_id: str | None,
    ) -> None:
        if not active_skill or active_skill.skill_id != source_skill_id or not source_step_id:
            return

        step = self.current_skill_step(active_skill, source_step_id)
        if not step:
            return
        missing_fields = [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self.skill_slot_satisfied(chat_session.slots_json or {}, str(field))
        ]
        is_waiting_reply = step_result.action in {"ask_user", "clarify"}
        if is_waiting_reply and missing_fields:
            previous = (
                chat_session.awaiting_input_json
                if isinstance(chat_session.awaiting_input_json, dict)
                else {}
            )
            awaiting_input = {
                "skill_id": source_skill_id,
                "step_id": source_step_id,
                "expected_fields": missing_fields,
                "question_summary": str(step_result.reply or "").strip() or None,
            }
            if previous.get("task_id"):
                awaiting_input["task_id"] = previous["task_id"]
            chat_session.awaiting_input_json = awaiting_input
            chat_session.last_agent_question = awaiting_input["question_summary"]
            return

        should_clear = bool(
            step_result.next_step_id
            or step_result.tool_call
            or step_result.is_step_completed
            or not missing_fields
        )
        awaiting = chat_session.awaiting_input_json
        if not should_clear or not isinstance(awaiting, dict):
            return
        if awaiting.get("skill_id") not in {None, source_skill_id}:
            return
        if awaiting.get("step_id") not in {None, source_step_id}:
            return
        task_id = awaiting.get("task_id")
        chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
        chat_session.last_agent_question = None

    def change_active_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        next_step_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        if previous_step == next_step_id:
            return
        payload: dict[str, Any] = {
            "from_skill_id": chat_session.active_skill_id,
            "to_skill_id": chat_session.active_skill_id,
            "from_step_id": previous_step,
            "to_step_id": next_step_id,
        }
        if reason:
            payload["reason"] = reason
        self.events.record(tenant_id, chat_session.id, "skill_step_changed", payload)

    def graph_pending_steps(self, chat_session: ChatSession) -> list[str]:
        value = (chat_session.slots_json or {}).get(GRAPH_PENDING_STEPS_SLOT)
        return GraphRules.normalize_pending_steps(value)

    def store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        slots = dict(chat_session.slots_json or {})
        normalized = GraphRules.normalize_pending_steps(pending_steps)
        if normalized:
            slots[GRAPH_PENDING_STEPS_SLOT] = normalized
        else:
            slots.pop(GRAPH_PENDING_STEPS_SLOT, None)
        chat_session.slots_json = slots
        self.events.record(
            tenant_id,
            chat_session.id,
            "graph_pending_steps_updated",
            {"pending_step_ids": normalized},
        )

    def queue_graph_sibling_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        source_step_id: str | None,
        selected_step_id: str,
    ) -> None:
        if not source_step_id:
            return
        outgoing = self.graph_outgoing_edges(active_skill).get(source_step_id) or []
        sibling_steps = GraphRules.sibling_steps_from_edges(
            outgoing,
            selected_step_id,
            self.edge_condition,
        )
        if not sibling_steps:
            return
        pending_steps = self.graph_pending_steps(chat_session)
        for step_id in sibling_steps:
            if step_id not in pending_steps:
                pending_steps.append(step_id)
        self.store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def edge_condition(self, edge: dict[str, Any]) -> str:
        return GraphRules.edge_condition(edge)

    def activate_next_pending_graph_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        *,
        reason: str,
    ) -> bool:
        pending_steps = self.graph_pending_steps(chat_session)
        while pending_steps:
            next_step_id = pending_steps.pop(0)
            if not self.skill_has_step(active_skill, next_step_id):
                continue
            self.store_graph_pending_steps(tenant_id, chat_session, pending_steps)
            self.change_active_step(tenant_id, chat_session, next_step_id, reason=reason)
            return True
        self.store_graph_pending_steps(tenant_id, chat_session, [])
        return False

    def skill_has_step(self, skill: Skill, step_id: str | None) -> bool:
        return GraphRules.has_step(skill.content_json or {}, step_id)

    def first_step_id(self, skill: Skill) -> str | None:
        content = skill.content_json or {}
        start_node_id = str(content.get("start_node_id") or "").strip()
        if start_node_id and self.skill_has_step(skill, start_node_id):
            return start_node_id
        steps = self.skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.steps_from_nodes(self.ordered_skill_nodes(skill))

    def skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.nodes(skill.content_json or {})

    def ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        content = skill.content_json or {}
        return GraphRules.ordered_nodes(
            content,
            nodes=self.skill_nodes(skill),
            outgoing=self.graph_outgoing_edges(skill),
        )

    def graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        return GraphRules.outgoing_edges(skill.content_json or {})

    def default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.default_next_step_from_parts(
            self.skill_nodes(skill),
            self.graph_outgoing_edges(skill).get(active_step_id, []),
        )

    def current_skill_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.current_step_from_steps(self.skill_steps(skill), active_step_id)

    def skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        return GraphRules.slot_satisfied(slots, field)

    def list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
        return visible_published_skills(self.db, tenant_id, agent_id)

    def get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None:
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        skills_by_id = {skill.skill_id: skill for skill in skills}
        available_skill_ids = set(skills_by_id)
        changed = False
        removed_skill_ids: set[str] = set()
        repaired_steps: list[dict[str, str | None]] = []

        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            changed = True

        def frame_skill_id(frame: object) -> str:
            if not isinstance(frame, dict):
                return ""
            return str(frame.get("target_skill_id") or frame.get("skill_id") or "").strip()

        def keep_frame(frame: object) -> bool:
            skill_id = frame_skill_id(frame)
            if not skill_id:
                return True
            if skill_id in available_skill_ids:
                return True
            removed_skill_ids.add(skill_id)
            return False

        active_skill_id = str(chat_session.active_skill_id or "").strip()
        if active_skill_id and active_skill_id not in available_skill_ids:
            removed_skill_ids.add(active_skill_id)
            chat_session.active_skill_id = None
            chat_session.active_step_id = None
            chat_session.slots_json = {}
            chat_session.awaiting_input_json = None
            chat_session.resume_after_answer_json = None
            changed = True
        elif active_skill_id:
            active_skill = skills_by_id[active_skill_id]
            active_step_id = str(chat_session.active_step_id or "").strip()
            restored_step_id = self.first_step_id(active_skill)
            if (
                restored_step_id
                and active_step_id != restored_step_id
                and not self.skill_has_step(active_skill, active_step_id)
            ):
                chat_session.active_step_id = restored_step_id
                awaiting = (
                    chat_session.awaiting_input_json
                    if isinstance(chat_session.awaiting_input_json, dict)
                    else {}
                )
                task_id = awaiting.get("task_id")
                chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
                chat_session.last_agent_question = None
                repaired_steps.append(
                    {
                        "skill_id": active_skill_id,
                        "from_step_id": active_step_id,
                        "to_step_id": restored_step_id,
                    }
                )
                changed = True

        for attr in ("pending_tasks_json",):
            value = getattr(chat_session, attr) or []
            if not isinstance(value, list):
                continue
            kept = [frame for frame in value if keep_frame(frame)]
            if len(kept) != len(value):
                setattr(chat_session, attr, kept)
                changed = True

        awaiting = chat_session.awaiting_input_json
        if isinstance(awaiting, dict):
            awaiting_skill_id = str(awaiting.get("skill_id") or "").strip()
            if awaiting_skill_id and awaiting_skill_id not in available_skill_ids:
                removed_skill_ids.add(awaiting_skill_id)
                chat_session.awaiting_input_json = None
                changed = True

        if changed:
            chat_session.updated_at = utc_now()
            if hasattr(self, "events"):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_state_pruned",
                    {
                        "removed_skill_ids": sorted(removed_skill_ids),
                        "repaired_steps": repaired_steps,
                    },
                )
        return changed
