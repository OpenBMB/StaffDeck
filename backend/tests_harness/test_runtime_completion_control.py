from app.core.task_request_compiler import TaskRequirement
from staffdeck_harness.bridge.control import ExecutionHost
from staffdeck_harness.composition.compiler import CompositionCompiler
from tests_harness.test_capability_host_hardening import db as db, _host, _staff, _ctx
import pytest


def test_conversation_cannot_submit_a_sop_result(db):
    cap = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    execution = ExecutionHost(
        cap, TaskRequirement(task_frame_id="tf1", kind="conversation", goal="chat")
    )
    assert "submit_step_result" not in execution.model_tool_names()
    result, receipt = execution.invoke_proxy(
        "submit_step_result", {"status": "completed", "reply_fragment": "hi"}, _ctx()
    )
    assert result.error["code"] == "CONTROL_UNAVAILABLE" and receipt is None
    assert cap.slot.finish is None and not cap.slot.closed


def test_completion_requires_successful_node_capabilities_before_closing(db):
    cap = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    req = TaskRequirement(
        task_frame_id="tf1", kind="sop", goal="flow", required_capability_names=["required-tool"]
    )
    execution = ExecutionHost(cap, req)
    result, _ = execution.invoke_proxy(
        "submit_step_result", {"status": "completed", "reply_fragment": "done"}, _ctx()
    )
    assert result.error["code"] == "REQUIRED_CAPABILITY_NOT_INVOKED" and not cap.slot.closed
    cap.results.append({"tool_name": "required-tool", "success": True})
    result, _ = execution.invoke_proxy(
        "submit_step_result",
        {"status": "completed", "reply_fragment": "done", "next_step_id": None},
        _ctx(),
    )
    assert result.success and cap.slot.closed


def test_control_cannot_select_an_unlisted_transition(db):
    cap = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    execution = ExecutionHost(cap, TaskRequirement(task_frame_id="tf1", kind="sop", goal="flow"))
    result, _ = execution.invoke_proxy(
        "submit_step_result",
        {"status": "completed", "reply_fragment": "done", "next_step_id": "injected-node"},
        _ctx(),
    )
    assert result.error["code"] == "INVALID_TRANSITION" and not cap.slot.closed


@pytest.mark.parametrize("status", ["completed", "awaiting_user", "handoff", "failed"])
def test_native_conversation_result_uses_v2_status_and_slot_normalization(db, status):
    from app.core.harness_agent import HarnessAction, finish_execution_result
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent
    from staffdeck_harness.interactions.pipeline_host import PipelineState

    cap = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    req = TaskRequirement(task_frame_id="tf1", kind="conversation", goal="chat")
    action = HarnessAction(
        action="finish",
        status=status,
        reply_fragment="reply",
        slot_updates={"confirmed": True},
        next_step_id="not-a-sop-step",
    )
    expected = finish_execution_result(req, action, [], [], [], [], action_count=1)
    runner = HarnessV3TaskAgent.__new__(HarnessV3TaskAgent)
    runner._host = cap
    actual = runner._result(
        req, cap.slot, PipelineState(cap.slot.snapshot), action.model_dump_json(), "stop", 1, [], []
    )
    assert actual.model_dump() == expected.model_dump()
    assert not cap.slot.closed, "a native reply is not a control-tool submission"


def test_marker_only_checkpoint_recovers_only_its_own_sop_public_history(db):
    from app.core.task_frame_store import TaskFrameStore
    from app.db.models import ChatSession, HarnessTaskFrameRecord, HarnessRunRecord, Message
    import json

    session = ChatSession(id="s1", tenant_id="t1", user_id="u1", agent_id="a1")
    row = HarnessTaskFrameRecord(
        tenant_id="t1", session_id="s1", task_id="f1", source_turn_id="m1", kind="sop"
    )
    other = HarnessTaskFrameRecord(
        tenant_id="t1", session_id="s1", task_id="f2", source_turn_id="m2", kind="sop"
    )
    db.add_all([session, row, other])
    db.commit()
    store = TaskFrameStore(db)
    logical, foreign = store.ensure_agent_loop(row), store.ensure_agent_loop(other)
    for frame, loop, text in ((row, logical, "OWN-NONCE"), (other, foreign, "OTHER-SECRET")):
        db.add(
            Message(
                id=frame.source_turn_id, tenant_id="t1", session_id="s1", role="user", content=text
            )
        )
        db.add(
            HarnessRunRecord(
                tenant_id="t1",
                session_id="s1",
                task_frame_record_id=frame.id,
                agent_loop_id=loop.id,
                task_id=frame.task_id,
                source_turn_id=frame.source_turn_id,
                status="awaiting_user",
                task_requirement_json={"known_slots": {"token": text}},
                result_json={"reply_fragment": text, "slot_updates": {"token": text}},
            )
        )
    logical.checkpoint_json = {"engine": "harness_v3", "version": 1, "task_frame_id": row.task_id}
    db.add(logical)
    db.commit()
    recovered = store.execution_checkpoint(row, logical)
    assert "OWN-NONCE" in json.dumps(recovered)
    assert "OTHER-SECRET" not in json.dumps(recovered)
    assert recovered["history_recovery_source"] == "legacy_public_runs"
