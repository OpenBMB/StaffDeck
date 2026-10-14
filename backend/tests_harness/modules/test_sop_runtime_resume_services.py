from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from staffdeck_sop_runtime.api import create_app

from app.core.handoff_reply_service import apply_handoff_reply
from app.db.models import (
    ChatSession,
    ExternalBusinessTask,
    HarnessTaskFrameRecord,
    HumanHandoffRequest,
)
from app.tools.external_tasks import _prepare_sop_resume
from staffdeck_harness.sop.http_runtime import HttpSopRuntime
from staffdeck_harness.sop.runtime_client import RuntimeClient


@pytest.fixture
def runtime(monkeypatch):
    with TestClient(create_app()) as http:

        def transport(path, payload, headers):
            response = (
                http.get(path, headers=headers)
                if payload is None
                else http.post(path, json=payload, headers=headers)
            )
            return response.json()

        client = RuntimeClient("http://runtime", transport=transport)
        monkeypatch.setenv("SOP_RUNTIME_ENDPOINT", "http://runtime")
        monkeypatch.setattr(
            "staffdeck_harness.sop.runtime_client.RuntimeClient", lambda *a, **k: client
        )
        yield client


def _wait(client, session_id, status):
    node = {"node_id": "wait", "type": "handoff" if status == "handoff" else "tool_call"}
    client.request(
        "/v1/definitions",
        {
            "sop_id": "flow",
            "version": "1",
            "content": {
                "start_node_id": "wait",
                "nodes": [node, {"node_id": "done"}],
                "edges": [{"source_node_id": "wait", "next_node_id": "done"}],
                "terminal_node_ids": ["done"],
            },
        },
    )
    client.operation(session_id, "prepare", request_id="prepare", turn_id="turn", sopId="flow")
    return client.operation(
        session_id,
        "submit",
        request_id="wait",
        turn_id="turn",
        revision=0,
        proposal={"status": status, "reply": "Waiting"},
    )["state"]


@pytest.mark.parametrize("source", ["web", "feishu"])
def test_actual_handoff_reply_service_resumes_runtime_and_preserves_mapping(db, runtime, source):
    from staffdeck_harness.modules.registry import reset_registry

    reset_registry()
    session = ChatSession(
        id="s1",
        tenant_id="t1",
        user_id="u1",
        agent_id="a1",
        active_skill_id="flow",
        active_step_id="wait",
        status="handoff",
    )
    runtime_id = HttpSopRuntime._id(session, "task1")
    wait = _wait(runtime, runtime_id, "handoff")
    session.awaiting_input_json = {
        "runtime_session_id": runtime_id,
        "task_id": "task1",
        "local_sop_id": "flow",
        "runtime_revision": wait["revision"],
        **wait["pending_wait"],
    }
    row = HumanHandoffRequest(
        id="handoff1",
        tenant_id="t1",
        session_id="s1",
        agent_id="a1",
        requester_user_id="u1",
        assignee_user_id="admin",
        status="assigned",
    )
    frame = HarnessTaskFrameRecord(tenant_id="t1", session_id="s1", source_turn_id="turn",
        task_id="task1", kind="sop", skill_id="flow", step_id="wait", status="handoff")
    db.add_all([session, row, frame])
    db.commit()
    resumed = []
    apply_handoff_reply(
        db, row, "Approved", answered_by_user_id="admin", source=source, resume=resumed.append
    )
    state = runtime.session(runtime_id)["state"]
    assert state["status"] == "active" and state["revision"] == 2
    assert state["slots"]["handoff_completed"] is True
    assert session.awaiting_input_json["runtime_session_id"] == runtime_id
    assert session.awaiting_input_json["runtime_revision"] == 2
    assert resumed == ["handoff1"] and row.status == "answered"
    assert frame.status == "ready_to_resume" and frame.step_id == "wait"
    runtime.operation(
        runtime_id,
        "submit",
        request_id="advance",
        turn_id="after",
        revision=2,
        proposal={"status": "completed", "reply": "Approved"},
    )
    result = runtime.operation(
        runtime_id,
        "submit",
        request_id="done",
        turn_id="after",
        revision=3,
        proposal={"status": "completed", "reply": "Done"},
    )
    assert result["state"]["status"] == "completed"


def test_actual_external_completion_service_resumes_once(db, runtime):
    runtime_id = HttpSopRuntime._id(SimpleNamespace(tenant_id="t1", id="s1"), "task1")
    _wait(runtime, runtime_id, "waiting_external_task")
    frame = HarnessTaskFrameRecord(
        tenant_id="t1",
        session_id="s1",
        source_turn_id="turn",
        task_id="task1",
        kind="sop",
        skill_id="flow",
        step_id="wait",
        status="waiting_external_task",
        result_json={"structured_result": {"task_id": "external1"}},
    )
    task = ExternalBusinessTask(
        id="external1",
        tenant_id="t1",
        user_id="u1",
        tool_id="tool1",
        session_id="s1",
        task_frame_id="task1",
        callback_token_hash="test",
        status="completed",
        result_json={"answer": "done"},
        resume_step_id="unauthorized-node",
    )
    db.add_all([frame, task])
    db.commit()
    _prepare_sop_resume(db, task)
    _prepare_sop_resume(db, task)
    state = runtime.session(runtime_id)["state"]
    assert state["revision"] == 2 and state["status"] == "active"
    assert state["slots"] == {"answer": "done"}
    assert frame.status == "ready_to_resume" and frame.step_id == "wait"
