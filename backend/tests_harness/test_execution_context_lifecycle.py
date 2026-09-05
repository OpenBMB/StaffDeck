"""Regression contract: v2 logical loops, not user turns, own execution history."""

from types import SimpleNamespace

from app.core.task_request_compiler import TaskRequirement, TaskExecutionResult
from staffdeck_harness.runtime.execution_context import ExecutionContext


def requirement(loop="loop-sop-1", frame="frame-1", kind="sop", step="collect"):
    return TaskRequirement(
        task_frame_id=frame,
        kind=kind,
        goal="complete workflow",
        execution_loop_id=loop,
        current_user_message="latest answer",
        sop_context={"step": {"node_id": step}} if kind == "sop" else {},
    )


def context(req, checkpoint=None):
    return ExecutionContext.restore(req, checkpoint, tenant_id="t1", agent_id="a1", session_id="s1")


def test_same_sop_resumes_history_in_another_user_turn_and_process():
    first = context(requirement())
    proc1 = SimpleNamespace(context_sessions={})
    sid, recovery = first.prepare(proc1)
    assert not recovery
    result = TaskExecutionResult(
        task_frame_id="frame-1", status="awaiting_user", reply_fragment="Please confirm blue."
    )
    cp = first.complete(
        proc1, sid, result, [{"tool_name": "lookup", "success": True, "data": {"colour": "blue"}}]
    )
    second = context(requirement(), cp)
    assert second.logical_id == first.logical_id
    assert second.prepare(proc1) == (sid, ""), "warm native history must not be duplicated"
    _, recovery = second.prepare(SimpleNamespace(context_sessions={}))
    assert "blue" in recovery and "lookup" in recovery
    assert second.capability_results, "same-node successful receipts survive waiting for the user"


def test_advancing_node_keeps_history_but_not_previous_node_receipts():
    first = context(requirement())
    proc = SimpleNamespace(context_sessions={})
    sid, _ = first.prepare(proc)
    cp = first.complete(
        proc,
        sid,
        TaskExecutionResult(
            task_frame_id="frame-1", status="completed", reply_fragment="address confirmed"
        ),
        [{"tool_name": "lookup", "success": True}],
    )
    next_node = context(requirement(step="ship"), cp)
    assert next_node.history and not next_node.capability_results


def test_general_loop_survives_new_task_frame_but_sop_instances_are_isolated():
    first = context(requirement(loop="general-s1", frame="f1", kind="conversation"))
    proc = SimpleNamespace(context_sessions={})
    sid, _ = first.prepare(proc)
    cp = first.complete(
        proc,
        sid,
        TaskExecutionResult(
            task_frame_id="f1", status="completed", reply_fragment="remember context"
        ),
        [],
    )
    assert context(requirement(loop="general-s1", frame="f2", kind="conversation"), cp).history
    import pytest

    with pytest.raises(ValueError, match="scope"):
        context(requirement(loop="sop-new"), cp)


def test_a_failed_or_uncommitted_native_phase_is_not_reused():
    ctx = context(requirement())
    proc = SimpleNamespace(context_sessions={})
    sid, _ = ctx.prepare(proc)
    checkpoint = ctx.complete(
        proc,
        sid,
        TaskExecutionResult(task_frame_id="frame-1", status="completed", reply_fragment="done"),
        [],
    )
    old_db_state = context(requirement())
    replacement, _ = old_db_state.prepare(proc)
    assert replacement != sid, "native state ahead of committed checkpoint must not leak into retry"
    assert checkpoint["context_scope"]["loop_id"] == "loop-sop-1"


def test_scope_includes_tenant_and_staff():
    import pytest

    first = context(requirement())
    cp = first.complete(
        None, "", TaskExecutionResult(task_frame_id="frame-1", status="awaiting_user"), []
    )
    with pytest.raises(ValueError, match="scope"):
        ExecutionContext.restore(
            requirement(), cp, tenant_id="other", agent_id="a1", session_id="s1"
        )
    with pytest.raises(ValueError, match="scope"):
        ExecutionContext.restore(
            requirement(), cp, tenant_id="t1", agent_id="other", session_id="s1"
        )


def test_checkpoint_receipts_are_json_serializable():
    import json
    from datetime import datetime, timezone

    cp = context(requirement()).complete(
        None,
        "",
        TaskExecutionResult(task_frame_id="frame-1", status="awaiting_user"),
        [
            {
                "tool_name": "test",
                "success": True,
                "receipt": {"finished_at": datetime.now(timezone.utc)},
            }
        ],
    )
    assert json.loads(json.dumps(cp))["capability_results"][0]["receipt"]["finished_at"]
