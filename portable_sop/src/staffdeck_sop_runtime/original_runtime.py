"""HTTP-facing adapter for StaffDeck's existing SOP lifecycle module.

The sidecar deliberately owns no graph, slot, or completion rules.  It converts
the narrow PilotDeck wire format into the existing StaffDeck data contracts and
delegates every transition through ``staffdeck_harness.sop.module.SopRuntimeModule``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.db.models import ChatSession, Skill
from app.session.session_schema import RouterDecision
from staffdeck_harness.sop.graph import GraphRules
from staffdeck_harness.sop.contracts import SopDependencies
from staffdeck_harness.sop.module import SopRuntimeModule


SOP_MODULE = SopRuntimeModule()


class SopRuntimeError(ValueError):
    """A StaffDeck contract error rendered through the sidecar protocol."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


def prepare(bundle: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    skill = _active_skill(bundle, state)
    session = _session(state, skill)
    step = _step(skill, session.active_step_id)
    return {"state": _wire_state(session, state), "step": _wire_step(skill, session, step)}


def submit(
    bundle: dict[str, Any],
    state: dict[str, Any],
    proposal: dict[str, Any],
    successful_tool_names: Iterable[str] = (),
) -> dict[str, Any]:
    skill = _active_skill(bundle, state)
    session = _session(state, skill)
    step = _step(skill, session.active_step_id)
    runtime = SOP_MODULE.build(SopDependencies(events=_Events(), create_handoff=_handoff))
    terminal_snapshot = {
        "skill_id": session.active_skill_id,
        "step_id": session.active_step_id,
        "slots": dict(session.slots_json or {}),
    }
    status = _text(proposal.get("status"))
    if status not in {"completed", "awaiting_user", "handoff", "failed", "blocked", "waiting_external_task"}:
        raise SopRuntimeError("INVALID_STATUS", "SOP result status is invalid.", {"status": status})
    reply = _text(proposal.get("replyFragment"))
    if not reply:
        raise SopRuntimeError("REPLY_REQUIRED", "SOP results require a replyFragment.")

    # The native coordinator reactivates a session when a user answer resumes
    # an awaiting SOP. PilotDeck submits that answer through this boundary, so
    # project the existing host transition before delegating lifecycle work.
    if session.status == "awaiting_user" and status == "completed":
        session.status = "active"

    result = TaskExecutionResult(
        task_frame_id="pilotdeck-sidecar",
        status=status,
        reply_fragment=reply,
        slot_updates=_mapping(proposal.get("slotUpdates")),
        next_step_id=_text(proposal.get("nextStepId")) or _text(proposal.get("next_step_id")) or None,
        task_summary=_text(proposal.get("taskSummary")),
        structured_result=deepcopy(proposal.get("structuredResult")),
        capability_results=[{"tool_name": name, "success": True} for name in successful_tool_names if _text(name)],
    )
    requirement = _requirement(step, session)
    _validate_submission(requirement, skill, session, result)
    decision = RouterDecision(decision="handoff_human" if status == "handoff" else "continue_active")
    try:
        # PilotDeck owns the next model invocation.  Tell StaffDeck that one
        # action remains so its existing lifecycle preserves a successful
        # graph advance instead of recategorizing it as action-budget exhaustion.
        runtime.after_execution("pilotdeck", session, skill, requirement, result, decision, remaining_actions=1)
    except ValueError as error:
        raise SopRuntimeError("SOP_RUNTIME_REJECTED", str(error)) from error

    # These result statuses are owned by the existing task result contract. The
    # host needs them in its persisted wire state to decide whether to accept a
    # later external/human continuation.
    if result.status in {"awaiting_user", "blocked", "waiting_external_task", "failed"}:
        session.status = result.status
    elif result.status == "completed" and session.active_skill_id is None:
        # StaffDeck represents a completed skill by clearing its active frame.
        # PilotDeck's persisted protocol additionally needs a terminal marker
        # so it will not prepare another model turn for that frame.
        session.status = "completed"
    next_state = _wire_state(session, state)
    if result.status == "completed":
        # Successful PilotDeck business-tool receipts belong to this submitted
        # StaffDeck step only. The next step receives a fresh receipt set.
        next_state["successful_tool_names"] = []
        if session.active_skill_id is None:
            # Keep the final StaffDeck position and slots as a host-facing
            # terminal snapshot. The lifecycle has already completed its own
            # state and intentionally cleared these execution fields.
            next_state["active_skill_id"] = terminal_snapshot["skill_id"]
            next_state["active_step_id"] = terminal_snapshot["step_id"]
            next_state["slots_json"] = {
                **terminal_snapshot["slots"],
                **dict(result.slot_updates),
            }
    return {
        "state": next_state,
        "result": {
            "status": result.status,
            "replyFragment": result.reply_fragment,
            "slotUpdates": dict(result.slot_updates),
            "taskSummary": result.task_summary or None,
            "structuredResult": deepcopy(result.structured_result),
            "nextStepId": result.next_step_id,
            "events": [],
        },
    }


def _validate_submission(
    requirement: TaskRequirement,
    skill: Skill,
    session: ChatSession,
    result: TaskExecutionResult,
) -> None:
    """Run the existing StaffDeck control validator before lifecycle mutation."""
    allowed = GraphRules.outgoing_edges(skill.content_json).get(session.active_step_id or "", [])
    checked = SOP_MODULE.submission_validator(requirement).submit(
        {
            "status": result.status,
            "reply_fragment": result.reply_fragment,
            "slot_updates": result.slot_updates,
            "next_step_id": result.next_step_id,
            "task_summary": result.task_summary,
            "structured_result": result.structured_result,
        },
        allowed_next_steps=[_text(edge.get("next_node_id")) for edge in allowed if _text(edge.get("next_node_id"))],
        capability_results=result.capability_results,
        citations=result.citations,
        evidence=result.evidence_results,
    )
    if not checked.success:
        error = checked.error or {}
        raise SopRuntimeError(
            _text(error.get("code")) or "SOP_RUNTIME_REJECTED",
            _text(error.get("message")) or "StaffDeck rejected the SOP step result.",
            dict(checked.extensions.get("details") or {}) if isinstance(checked.extensions, dict) else {},
        )
    accepted = checked.data if isinstance(checked.data, dict) else {}
    result.next_step_id = _text(accepted.get("next_step_id")) or None


class _Events:
    def record(self, *_args: Any, **_kwargs: Any) -> None:
        # Side effects are persisted and projected by the PilotDeck host.
        return None


def _handoff(_tenant: str, session: ChatSession, skill: Skill | None, _result: Any) -> None:
    session.status = "handoff"
    session.awaiting_input_json = {
        "kind": "handoff",
        "skill_id": skill.skill_id if skill else session.active_skill_id,
        "step_id": session.active_step_id,
    }


def _active_skill(bundle: dict[str, Any], state: dict[str, Any]) -> Skill:
    definitions = _definitions(bundle)
    skill_id = _text(state.get("active_skill_id")) or _text(state.get("selected_skill_id"))
    if not skill_id:
        raise SopRuntimeError("SOP_NOT_SELECTED", "No SOP is selected for this session.")
    definition = definitions.get(skill_id)
    if definition is None:
        raise SopRuntimeError("SOP_NOT_FOUND", f"SOP '{skill_id}' is not enabled by this deployment.")
    return definition


def _definitions(bundle: dict[str, Any]) -> dict[str, Skill]:
    raw = bundle.get("sops") if isinstance(bundle, dict) else None
    if not isinstance(raw, list) or not raw:
        raise SopRuntimeError("SOP_DEFINITIONS_EMPTY", "The SOP definition bundle has no valid definitions.")
    definitions: dict[str, Skill] = {}
    for value in raw:
        if not isinstance(value, dict):
            continue
        skill_id = _text(value.get("id")) or _text(value.get("skill_id"))
        content = _mapping(value.get("content")) or _mapping(value.get("content_json"))
        if not skill_id or not content:
            continue
        if skill_id in definitions:
            raise SopRuntimeError("SOP_DUPLICATE_ID", f"Duplicate SOP id '{skill_id}'.")
        definitions[skill_id] = Skill(
            id=f"portable-{skill_id}", tenant_id="pilotdeck", skill_id=skill_id,
            version=_text(value.get("version")) or "1", name=_text(value.get("name")) or skill_id,
            content_json=deepcopy(content), status="published",
        )
    if not definitions:
        raise SopRuntimeError("SOP_DEFINITIONS_EMPTY", "The SOP definition bundle has no valid definitions.")
    return definitions


def _session(state: dict[str, Any], skill: Skill) -> ChatSession:
    active_step = _text(state.get("active_step_id")) or _first_step(skill)
    if not active_step or not GraphRules.has_step(skill.content_json, active_step):
        raise SopRuntimeError("SOP_STEP_NOT_FOUND", f"SOP '{skill.skill_id}' does not contain step '{active_step}'.")
    return ChatSession(
        id="pilotdeck-sidecar", tenant_id="pilotdeck", active_skill_id=skill.skill_id,
        active_step_id=active_step, slots_json=deepcopy(_mapping(state.get("slots_json"))),
        skill_stack_json=deepcopy(_list_of_mappings(state.get("skill_stack_json"))),
        pending_tasks_json=deepcopy(_list_of_mappings(state.get("pending_tasks_json"))),
        awaiting_input_json=deepcopy(state.get("awaiting_input_json")) if isinstance(state.get("awaiting_input_json"), dict) else None,
        summary=_text(state.get("summary")) or None,
        status=_text(state.get("status")) or "active",
    )


def _first_step(skill: Skill) -> str:
    content = skill.content_json
    start = _text(content.get("start_node_id"))
    if start and GraphRules.has_step(content, start):
        return start
    nodes = GraphRules.nodes(content)
    return _text(nodes[0].get("node_id")) if nodes else ""


def _step(skill: Skill, step_id: str | None) -> dict[str, Any]:
    step = GraphRules.current_step(skill.content_json, step_id)
    if step is None:
        raise SopRuntimeError("SOP_STEP_NOT_FOUND", f"SOP '{skill.skill_id}' does not contain step '{step_id}'.")
    return step


def _requirement(step: dict[str, Any], session: ChatSession) -> TaskRequirement:
    expected = [_text(value) for value in step.get("expected_user_info", []) if _text(value)]
    required_tools = [action.partition(":")[2].strip() for action in GraphRules.step_actions(step) if action.startswith("call_tool:")]
    return TaskRequirement(
        task_frame_id="pilotdeck-sidecar", kind="sop", goal=_text(step.get("instruction")) or "SOP step",
        expected_slots=expected, required_slots=expected, known_slots=dict(session.slots_json or {}),
        required_capability_names=required_tools,
    )


def _wire_state(session: ChatSession, prior: dict[str, Any]) -> dict[str, Any]:
    return {
        **deepcopy(prior),
        "version": int(prior.get("version") or 1),
        "selected_skill_id": _text(prior.get("selected_skill_id")) or session.active_skill_id,
        "active_skill_id": session.active_skill_id,
        "active_step_id": session.active_step_id,
        "slots_json": deepcopy(session.slots_json or {}),
        "skill_stack_json": deepcopy(session.skill_stack_json or []),
        "awaiting_input_json": deepcopy(session.awaiting_input_json),
        "summary": session.summary,
        "status": session.status,
    }


def _wire_step(skill: Skill, session: ChatSession, step: dict[str, Any]) -> dict[str, Any]:
    outgoing = GraphRules.outgoing_edges(skill.content_json).get(session.active_step_id or "", [])
    actions = GraphRules.step_actions(step)
    return {
        "skillId": skill.skill_id, "skillName": skill.name, "version": skill.version,
        "nodeId": session.active_step_id, "node": deepcopy(step),
        "instruction": _text(step.get("instruction")),
        "expectedUserInfo": [_text(value) for value in step.get("expected_user_info", []) if _text(value)],
        "knownSlots": deepcopy(session.slots_json or {}),
        "allowedNextStepIds": [_text(edge.get("next_node_id")) for edge in outgoing if _text(edge.get("next_node_id"))],
        "requiredToolNames": [action.partition(":")[2].strip() for action in actions if action.startswith("call_tool:")],
        "allowedActions": actions,
        "isTerminal": GraphRules.terminal_position(skill.content_json, session.active_step_id, session.slots_json or {}),
        "declaresHandoff": GraphRules.is_handoff_node(step),
        "subSopId": _text(step.get("sub_sop_id")) or None,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
