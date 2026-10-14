from staffdeck_sop_core import SopCoreError, SopState, prepare, resume, submit
import pytest


DEFINITION = {
    "sop_id": "approval",
    "version": "1.0.0",
    "name": "Approval",
    "content": {
        "start_node_id": "collect",
        "nodes": [
            {"node_id": "collect", "expected_user_info": ["name"], "allowed_actions": ["call_tool:lookup"]},
            {"node_id": "done", "type": "response", "allowed_actions": ["answer_user"]},
        ],
        "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
        "terminal_node_ids": ["done"],
    },
}


def test_core_wait_resume_and_complete_without_staffdeck_imports():
    state = SopState("session-1", "approval", "1.0.0")
    prepared = prepare(DEFINITION, state)
    assert prepared["step"]["nodeId"] == "collect"
    state = SopState.from_dict(prepared["state"])

    waiting = submit(DEFINITION, state, {"status": "awaiting_user", "reply": "Name?"})
    assert waiting.status == "awaiting_user"
    assert waiting.wait and waiting.wait.kind == "user_input"

    resumed = resume(waiting.state, {"wait_id": waiting.wait.wait_id, "slot_updates": {"name": "Ada"}})
    completed = submit(DEFINITION, resumed, {"status": "completed", "reply": "Done"}, [
        {"capability_id": "lookup", "operation_id": "op-1", "node_id": "collect", "status": "succeeded", "session_id": "session-1", "turn_id": "turn-1"},
    ])
    assert completed.status == "active"
    assert completed.state.active_node_id == "done"
    final = submit(DEFINITION, completed.state, {"status": "completed", "reply": "Finished"})
    assert final.status == "completed"


def test_core_rejects_cross_node_receipts_and_invalid_transitions():
    state = SopState("session-1", "approval", "1.0.0", active_node_id="collect")
    try:
        submit(DEFINITION, state, {"status": "completed", "reply": "Done", "slot_updates": {"name": "Ada"}}, [
            {"capability_id": "lookup", "operation_id": "op-1", "node_id": "done", "status": "succeeded"},
        ])
    except SopCoreError as error:
        assert error.code == "RECEIPT_SCOPE_MISMATCH"
    else:
        raise AssertionError("cross-node receipts must be rejected")

    try:
        submit(DEFINITION, state, {"status": "completed", "reply": "Done", "slot_updates": {"name": "Ada"}, "next_node_id": "missing"}, [
            {"capability_id": "lookup", "operation_id": "op-1", "node_id": "collect", "status": "succeeded", "session_id": "session-1", "turn_id": "turn-1"},
        ])
    except SopCoreError as error:
        assert error.code == "INVALID_TRANSITION"
    else:
        raise AssertionError("invalid transitions must be rejected")


def test_global_required_slots_block_terminal_completion():
    definition = {"sop_id": "flow", "version": "1", "content": {
        "start_node_id": "done", "nodes": [{"node_id": "done"}],
        "terminal_node_ids": ["done"], "required_info": ["account"]}}
    state = SopState("s", "flow", "1", active_node_id="done")
    with pytest.raises(SopCoreError) as failure:
        submit(definition, state, {"status": "completed", "reply": "Done"})
    assert failure.value.code == "REQUIRED_SLOT_MISSING"
    assert submit(definition, state, {"status": "completed", "reply": "Done",
        "slot_updates": {"account": "A"}}).status == "completed"


def test_core_rejects_host_objects_in_state():
    state = SopState("session-1", "approval", "1.0.0", metadata={"host": object()})
    with pytest.raises(SopCoreError) as failure:
        prepare(DEFINITION, state)
    assert failure.value.code == "SOP_VALUE_INVALID"


@pytest.mark.parametrize("kind", ["tool", "skill", "knowledge"])
def test_capability_ports_accept_host_doubles_for_all_resource_kinds(kind):
    definition = {"sop_id": "flow", "version": "1", "content": {
        "start_node_id": "done", "nodes": [{"node_id": "done"}], "terminal_node_ids": ["done"]},
        "capability_requirements": [{"kind": kind, "capability_id": "double", "node_id": "done", "required": True}]}
    receipt = {"session_id": "s", "turn_id": "t", "node_id": "done", "operation_id": "o",
        "capability_id": "double", "status": "succeeded", "result": {"value": "test"}}
    result = submit(definition, SopState("s", "flow", "1", active_node_id="done"),
        {"status": "completed", "reply": "Done"}, [receipt], turn_id="t")
    assert result.status == "completed"
