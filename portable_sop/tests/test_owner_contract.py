"""Independent v3 SOP owner oracle for the portable HTTP boundary.

Expected lifecycle results come from the native StaffDeck module contracts.  In
particular this file deliberately does not import the portable adapter's
``prepare``/``submit`` functions or any of its conversion helpers.
"""

from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.db.models import ChatSession, Skill
from app.session.session_schema import RouterDecision
from staffdeck_harness.sop.contracts import SopDependencies
from staffdeck_harness.sop.graph import GraphRules
from staffdeck_harness.sop.module import SopRuntimeModule
from staffdeck_sop_runtime.api import create_app


CLIENT = TestClient(create_app())
OWNER = SopRuntimeModule()

FLOW = {
    "sops": [{
        "id": "collect",
        "version": "1",
        "name": "Collect",
        "content": {
            "start_node_id": "collect",
            "nodes": [
                {
                    "node_id": "collect",
                    "instruction": "Collect a name.",
                    "expected_user_info": ["name"],
                    "allowed_actions": ["call_tool:read_file"],
                },
                {"node_id": "done", "instruction": "Done."},
            ],
            "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
            "terminal_node_ids": ["done"],
        },
    }],
}

HANDOFF = {
    "sops": [{
        "id": "approval",
        "content": {
            "start_node_id": "approval",
            "nodes": [{"node_id": "approval", "type": "handoff"}],
            "terminal_node_ids": ["approval"],
        },
    }],
}


def envelope(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "protocolVersion": "2.0",
        "runId": "owner-oracle-run",
        "operationId": f"owner-oracle:{request_id}",
        "requestId": request_id,
        "sessionId": "owner-oracle-session",
        "turnId": "owner-oracle-turn",
        "expectedRevision": 1,
        "payload": payload,
    }


def owner_skill(bundle: dict[str, Any], skill_id: str) -> Skill:
    definition = next(item for item in bundle["sops"] if item["id"] == skill_id)
    return Skill(
        id=f"owner-{skill_id}", tenant_id="pilotdeck", skill_id=skill_id,
        version=str(definition.get("version") or "1"), name=str(definition.get("name") or skill_id),
        content_json=deepcopy(definition["content"]), status="published",
    )


def owner_session(state: dict[str, Any], skill: Skill) -> ChatSession:
    content = skill.content_json
    active_step = state.get("active_step_id") or content.get("start_node_id") or GraphRules.nodes(content)[0]["node_id"]
    return ChatSession(
        id="owner-session", tenant_id="pilotdeck", active_skill_id=skill.skill_id,
        active_step_id=str(active_step), slots_json=deepcopy(state.get("slots_json") or {}),
        skill_stack_json=deepcopy(state.get("skill_stack_json") or []),
        pending_tasks_json=deepcopy(state.get("pending_tasks_json") or []),
        awaiting_input_json=deepcopy(state.get("awaiting_input_json")) if isinstance(state.get("awaiting_input_json"), dict) else None,
        summary=state.get("summary"), status=str(state.get("status") or "active"),
    )


def owner_requirement(skill: Skill, session: ChatSession) -> TaskRequirement:
    step = GraphRules.current_step(skill.content_json, session.active_step_id)
    assert step is not None
    expected = [str(value) for value in step.get("expected_user_info", []) if str(value).strip()]
    required_tools = [
        action.split(":", 1)[1]
        for action in GraphRules.step_actions(step)
        if action.startswith("call_tool:")
    ]
    return TaskRequirement(
        task_frame_id="owner-oracle", kind="sop", goal=str(step.get("instruction") or "SOP step"),
        expected_slots=expected, required_slots=expected, known_slots=dict(session.slots_json or {}),
        required_capability_names=required_tools,
    )


def owner_submit(
    bundle: dict[str, Any], state: dict[str, Any], proposal: dict[str, Any], successful_tools: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Execute the original validator and lifecycle, returning owner facts or its raw rejection."""
    skill_id = str(state.get("active_skill_id") or state.get("selected_skill_id"))
    skill = owner_skill(bundle, skill_id)
    session = owner_session(state, skill)
    requirement = owner_requirement(skill, session)
    next_step = proposal.get("nextStepId") or proposal.get("next_step_id")
    outgoing = GraphRules.outgoing_edges(skill.content_json).get(session.active_step_id or "", [])
    checked = OWNER.submission_validator(requirement).submit(
        {
            "status": proposal["status"],
            "reply_fragment": proposal["replyFragment"],
            "slot_updates": deepcopy(proposal.get("slotUpdates") or {}),
            "next_step_id": next_step,
            "task_summary": proposal.get("taskSummary") or "",
            "structured_result": deepcopy(proposal.get("structuredResult")),
        },
        allowed_next_steps=[str(edge["next_node_id"]) for edge in outgoing],
        capability_results=[{"tool_name": name, "success": True} for name in successful_tools],
        citations=[], evidence=[],
    )
    if not checked.success:
        return None, dict(checked.error or {})
    accepted = checked.data or {}
    result = TaskExecutionResult(
        task_frame_id="owner-oracle", status=proposal["status"], reply_fragment=proposal["replyFragment"],
        slot_updates=deepcopy(proposal.get("slotUpdates") or {}),
        next_step_id=accepted.get("next_step_id"), task_summary=str(proposal.get("taskSummary") or ""),
        structured_result=deepcopy(proposal.get("structuredResult")),
        capability_results=[{"tool_name": name, "success": True} for name in successful_tools],
    )
    if session.status == "awaiting_user" and result.status == "completed":
        session.status = "active"

    def create_handoff(_tenant: str, current: ChatSession, active: Skill | None, _result: Any) -> None:
        current.status = "handoff"
        current.awaiting_input_json = {
            "kind": "handoff",
            "skill_id": active.skill_id if active else current.active_skill_id,
            "step_id": current.active_step_id,
        }

    OWNER.build(SopDependencies(events=_NoopEvents(), create_handoff=create_handoff)).after_execution(
        "pilotdeck", session, skill, requirement, result,
        RouterDecision(decision="handoff_human" if result.status == "handoff" else "continue_active"),
        remaining_actions=1,
    )
    return {
        "result": {
            "status": result.status,
            "replyFragment": result.reply_fragment,
            "slotUpdates": dict(result.slot_updates),
            "nextStepId": result.next_step_id,
        },
        "session": {
            "active_skill_id": session.active_skill_id,
            "active_step_id": session.active_step_id,
            "slots_json": dict(session.slots_json or {}),
            "skill_stack_json": list(session.skill_stack_json or []),
            "awaiting_input_json": session.awaiting_input_json,
            "status": session.status,
        },
    }, None


class _NoopEvents:
    def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def http_prepare(bundle: dict[str, Any], state: dict[str, Any], request_id: str) -> dict[str, Any]:
    response = CLIENT.post("/v1/sop/prepare", json=envelope({"bundle": bundle, "state": state}, request_id))
    assert response.status_code == 200
    return response.json()["payload"]


def http_submit(bundle: dict[str, Any], state: dict[str, Any], proposal: dict[str, Any], successful_tools: list[str], request_id: str):
    return CLIENT.post("/v1/sop/submit", json=envelope({
        "bundle": bundle, "state": state, "proposal": proposal, "successfulToolNames": successful_tools,
    }, request_id))


def test_http_prepare_projects_native_graph_and_sub_sop_metadata_without_owner_reimplementation():
    bundle = {
        "sops": [{
            "id": "parent",
            "name": "Parent",
            "content": {"start_node_id": "delegate", "nodes": [{"node_id": "delegate", "sub_sop_id": "child"}]},
        }, {"id": "child", "content": {"start_node_id": "start", "nodes": [{"node_id": "start"}]}}],
    }
    state = {"selected_skill_id": "parent", "slots_json": {"nested": {"ok": True}}}
    skill = owner_skill(bundle, "parent")
    session = owner_session(state, skill)
    step = GraphRules.current_step(skill.content_json, session.active_step_id)
    assert step is not None
    prepared = http_prepare(bundle, state, "native-prepare")

    assert prepared["state"]["active_skill_id"] == session.active_skill_id
    assert prepared["state"]["active_step_id"] == session.active_step_id
    assert prepared["state"]["slots_json"] == session.slots_json
    assert prepared["step"]["nodeId"] == session.active_step_id
    assert prepared["step"]["allowedActions"] == GraphRules.step_actions(step)
    assert prepared["step"]["subSopId"] == step["sub_sop_id"]


@pytest.mark.parametrize("value", [None, "", "   ", 0, False, [], {}, ["Ada"], {"name": "Ada"}, "Ada"])
def test_http_submit_matches_native_owner_validator_for_raw_slot_values(value: Any):
    prepared = http_prepare(FLOW, {"selected_skill_id": "collect"}, f"slot-prepare-{repr(value)}")
    proposal = {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": value}}
    expected, rejection = owner_submit(FLOW, prepared["state"], proposal, ["read_file"])
    response = http_submit(FLOW, prepared["state"], proposal, ["read_file"], f"slot-submit-{repr(value)}")
    if rejection:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == rejection["code"]
        return
    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["result"]["status"] == expected["result"]["status"]
    assert payload["result"]["nextStepId"] == expected["result"]["nextStepId"]
    assert payload["state"]["active_step_id"] == expected["session"]["active_step_id"]
    assert payload["state"]["slots_json"] == expected["session"]["slots_json"]


@pytest.mark.parametrize(
    ("proposal", "tools", "expected_code"),
    [
        ({"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}}, [], "REQUIRED_CAPABILITY_NOT_INVOKED"),
        ({"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}, "nextStepId": "missing"}, ["read_file"], "INVALID_TRANSITION"),
    ],
)
def test_http_submit_preserves_native_owner_rejection_codes(proposal, tools, expected_code):
    prepared = http_prepare(FLOW, {"selected_skill_id": "collect", "successful_tool_names": ["read_file"]}, f"rejection-{expected_code}-prepare")
    expected, rejection = owner_submit(FLOW, prepared["state"], proposal, tools)
    assert expected is None
    assert rejection and rejection["code"] == expected_code
    response = http_submit(FLOW, prepared["state"], proposal, tools, f"rejection-{expected_code}-submit")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize(
    ("bundle", "initial", "proposal", "tools", "expected_host_status"),
    [
        (FLOW, {"selected_skill_id": "collect"}, {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}}, ["read_file"], "active"),
        (FLOW, {"selected_skill_id": "collect"}, {"status": "completed", "replyFragment": "choose", "slotUpdates": {"name": "Ada"}, "nextStepId": "done"}, ["read_file"], "active"),
        (FLOW, {"selected_skill_id": "collect"}, {"status": "awaiting_user", "replyFragment": "What is your name?"}, [], "awaiting_user"),
        (FLOW, {"selected_skill_id": "collect"}, {"status": "failed", "replyFragment": "failed"}, [], "failed"),
        (FLOW, {"selected_skill_id": "collect"}, {"status": "blocked", "replyFragment": "blocked"}, [], "blocked"),
        (FLOW, {"selected_skill_id": "collect"}, {"status": "waiting_external_task", "replyFragment": "wait"}, [], "waiting_external_task"),
        (HANDOFF, {"selected_skill_id": "approval"}, {"status": "handoff", "replyFragment": "approval required"}, [], "handoff"),
    ],
)
def test_http_submit_matches_native_lifecycle_facts_and_host_wait_projection(bundle, initial, proposal, tools, expected_host_status):
    prepared = http_prepare(bundle, initial, f"lifecycle-{proposal['status']}-prepare")
    expected, rejection = owner_submit(bundle, prepared["state"], proposal, tools)
    assert rejection is None
    response = http_submit(bundle, prepared["state"], proposal, tools, f"lifecycle-{proposal['status']}-submit")
    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["result"]["status"] == expected["result"]["status"]
    assert payload["result"]["nextStepId"] == expected["result"]["nextStepId"]
    assert payload["state"]["slots_json"] == expected["session"]["slots_json"]
    assert payload["state"]["status"] == expected_host_status
    if proposal["status"] in {"awaiting_user", "handoff"}:
        assert payload["state"]["awaiting_input_json"] == expected["session"]["awaiting_input_json"]
    if expected["session"]["active_skill_id"] is not None:
        assert payload["state"]["active_step_id"] == expected["session"]["active_step_id"]


def test_terminal_host_projection_keeps_snapshot_after_the_native_owner_clears_execution_fields():
    terminal = {
        "sops": [{
            "id": "terminal",
            "content": {"start_node_id": "finish", "nodes": [{"node_id": "finish"}], "terminal_node_ids": ["finish"]},
        }],
    }
    prepared = http_prepare(terminal, {"selected_skill_id": "terminal", "slots_json": {"before": 1}}, "terminal-prepare")
    proposal = {"status": "completed", "replyFragment": "done", "slotUpdates": {"after": 2}}
    expected, rejection = owner_submit(terminal, prepared["state"], proposal, [])
    assert rejection is None
    assert expected["session"]["active_skill_id"] is None
    response = http_submit(terminal, prepared["state"], proposal, [], "terminal-submit")
    payload = response.json()["payload"]
    assert payload["state"]["status"] == "completed"
    assert payload["state"]["active_skill_id"] == "terminal"
    assert payload["state"]["active_step_id"] == "finish"
    assert payload["state"]["slots_json"] == {"before": 1, "after": 2}
