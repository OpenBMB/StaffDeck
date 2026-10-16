from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from staffdeck_sop_runtime.api import create_app

from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.skills.sop_management import SopManagementService
from staffdeck_harness.contracts.sop import SopDefinition, SopState
from staffdeck_harness.sop.contracts import SopDependencies
from staffdeck_harness.sop.http_runtime import HttpSopRuntime
from staffdeck_harness.sop.runtime_client import RuntimeClient


@pytest.mark.parametrize("remaining_actions", [0, 1])
def test_nonterminal_completion_keeps_task_resumable(remaining_actions):
    client = SimpleNamespace(
        session=lambda _: {"state": {"revision": 0, "active_node_id": "first", "status": "active"},
                           "definition": {"content": {"nodes": [{"node_id": "first"}]}}},
        operation=lambda *a, **k: {"state": {"active_node_id": "last", "status": "active",
                                             "slots": {}, "revision": 1}},
    )
    adapter = HttpSopRuntime(client, SimpleNamespace())
    session = SopState("chat", "tenant", agent_id="agent")
    session.awaiting_input_json = {"runtime_session_id": "runtime", "local_sop_id": "flow"}
    result = TaskExecutionResult(task_frame_id="task", status="completed", reply_fragment="First done")
    requirement = TaskRequirement(task_frame_id="task", execution_loop_id="turn", kind="sop", goal="test")
    advance = adapter.after_execution("tenant", session, None, requirement, result, None,
                                      remaining_actions=remaining_actions)
    assert advance.step_result.is_step_completed
    assert advance.continue_execution == bool(remaining_actions)
    assert result.status == ("action_budget" if remaining_actions == 0 else "completed")
    assert session.active_step_id == "last"


@pytest.mark.parametrize("compact", [False, True])
def test_host_receipt_satisfies_both_resource_and_tool_name_requirements(compact):
    from app.core.task_request_compiler import (
        CapabilityCatalogEntry,
        CapabilityDescriptor,
        CapabilityManifest,
    )

    with TestClient(create_app()) as http:
        def transport(path, payload, headers):
            return (http.get(path) if payload is None else http.post(path, json=payload)).json()

        client = RuntimeClient("http://runtime", transport=transport)
        client.request("/v1/definitions", {
            "sop_id": "flow", "version": "1",
            "content": {"start_node_id": "work", "nodes": [{
                "node_id": "work", "allowed_actions": ["call_tool:lookup"],
                "capability_refs": {"required_tool_ids": ["tool-id"]},
            }]},
        })
        client.operation("runtime", "prepare", request_id="p", turn_id="turn", sopId="flow")
        adapter = HttpSopRuntime(client, SimpleNamespace())
        session = SopState("chat", "tenant", agent_id="agent")
        session.awaiting_input_json = {"runtime_session_id": "runtime", "local_sop_id": "flow"}
        requirement = TaskRequirement(
            task_frame_id="task", execution_loop_id="turn", kind="sop", goal="test",
            capability_manifest=CapabilityManifest(**{("catalog" if compact else "available"): [
                (CapabilityCatalogEntry if compact else CapabilityDescriptor)(
                capability_id="tool-id", name="lookup", kind="tool",
            )]}),
        )
        result = TaskExecutionResult(
            task_frame_id="task", status="completed", reply_fragment="Done",
            capability_results=[{"capability_id": "tool-id", "tool_name": "lookup",
                                 "operation_id": "invoke", "success": True}],
        )
        adapter.after_execution("tenant", session, None, requirement, result, None, remaining_actions=0)
        assert client.session("runtime")["state"]["status"] == "completed"


def test_staffdeck_adapter_shared_host_conformance_cases():
    import json
    from pathlib import Path

    import pytest

    from staffdeck_harness.sop.runtime_client import RuntimeError

    cases = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "portable_sop/tests/fixtures/host_conformance.json"
        ).read_text()
    )
    with TestClient(create_app()) as http:

        def transport(path, payload, headers):
            response = (
                http.get(path, headers=headers)
                if payload is None
                else http.post(path, json=payload, headers=headers)
            )
            return response.json()

        client = RuntimeClient("http://runtime", transport=transport)
        for case in cases:
            node = {"node_id": "work", "type": case.get("node_type", "tool")}
            if case.get("required_field"):
                node["expected_user_info"] = [case["required_field"]]
            content = {"start_node_id": "work", "nodes": [node], "terminal_node_ids": ["work"]}
            if case.get("kind"):
                content["capability_requirements"] = [
                    {
                        "kind": case["kind"],
                        "capability_id": "double",
                        "node_id": "work",
                        "required": True,
                    }
                ]
            view = SimpleNamespace(
                skill_id=case["id"], version="1", name=case["id"], content=content
            )
            client.request(
                "/v1/definitions", SopManagementService.snapshot(view, "tenant", "agent")
            )
            adapter = HttpSopRuntime(
                client, SopDependencies(SimpleNamespace(record=lambda *a: None), lambda *a: None)
            )
            session = SopState(case["id"], "tenant", agent_id="agent")
            skill = SopDefinition("row", "tenant", case["id"], "1", case["id"], deepcopy(content))
            frame = SimpleNamespace(
                tenant_id="tenant",
                session_id=case["id"],
                task_id="task",
                kind="sop",
                skill_id=case["id"],
            )
            adapter.activate_frame(session, frame, [skill])
            requirement = TaskRequirement(
                task_frame_id="task", execution_loop_id="turn", kind="sop", goal="test"
            )
            result = TaskExecutionResult(
                task_frame_id="task",
                status=case["status"],
                reply_fragment="Result",
                next_step_id=case.get("next_node_id"),
                capability_results=[
                    {"capability_id": "double", "operation_id": "call", "success": True}
                ]
                if case.get("receipt")
                else [],
            )
            if case.get("error"):
                with pytest.raises(RuntimeError) as error:
                    adapter.after_execution(
                        "tenant", session, skill, requirement, result, None, remaining_actions=1
                    )
                assert error.value.code == case["error"], case["id"]
            else:
                adapter.after_execution(
                    "tenant", session, skill, requirement, result, None, remaining_actions=1
                )
            runtime_id = adapter._id(session, "task")
            state = client.session(runtime_id)["state"]
            assert state["revision"] == case.get("expected_revision", 0), case["id"]
            assert state["status"] == case.get("expected_status", "active"), case["id"]
            if case.get("resume"):
                kwargs = {
                    "request_id": "resume",
                    "turn_id": "resume",
                    "revision": 1,
                    "waitId": state["pending_wait"]["wait_id"],
                    "slotUpdates": {"name": "Ada"},
                    "response": {"source": "human"},
                }
                assert client.operation(runtime_id, "resume", **kwargs) == client.operation(
                    runtime_id, "resume", **kwargs
                )
                requirement.execution_loop_id = "finish"
                result.status, result.next_step_id = "completed", None
                adapter.after_execution(
                    "tenant", session, skill, requirement, result, None, remaining_actions=1
                )
                assert client.session(runtime_id)["state"]["status"] == "completed", case["id"]


def test_staffdeck_adapter_wait_resume_complete_and_task_isolation():
    content = {
        "start_node_id": "collect",
        "nodes": [
            {
                "node_id": "collect",
                "expected_user_info": ["name"],
                "allowed_actions": ["call_tool:lookup"],
            },
            {"node_id": "done"},
        ],
        "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
        "terminal_node_ids": ["done"],
    }
    view = SimpleNamespace(skill_id="flow", version="1", name="Flow", content=content)
    with TestClient(create_app()) as http:

        def transport(path, payload, headers):
            response = (
                http.get(path, headers=headers)
                if payload is None
                else http.post(path, json=payload, headers=headers)
            )
            return response.json()

        client = RuntimeClient("http://runtime", transport=transport)
        client.request("/v1/definitions", SopManagementService.snapshot(view, "tenant", "agent"))
        adapter = HttpSopRuntime(
            client, SopDependencies(SimpleNamespace(record=lambda *a: None), lambda *a: None)
        )
        session = SopState("chat", "tenant", agent_id="agent")
        skill = SopDefinition("row", "tenant", "flow", "1", "Flow", deepcopy(content))
        frame = SimpleNamespace(
            tenant_id="tenant", session_id="chat", task_id="task1", kind="sop", skill_id="flow"
        )
        adapter.activate_frame(session, frame, [skill])
        assert session.active_step_id == "collect"
        requirement = TaskRequirement(
            task_frame_id="task1", execution_loop_id="execution-1", kind="sop", goal="test"
        )
        waiting = TaskExecutionResult(
            task_frame_id="task1", status="awaiting_user", reply_fragment="Name?"
        )
        adapter.after_execution(
            "tenant", session, skill, requirement, waiting, None, remaining_actions=2
        )
        assert session.status == "awaiting_user"
        requirement.execution_loop_id = "execution-2"
        requirement.current_user_message = "Ada"
        result = TaskExecutionResult(
            task_frame_id="task1",
            status="completed",
            reply_fragment="Collected",
            slot_updates={"name": "Ada"},
            capability_results=[
                {"tool_name": "lookup", "success": True, "operation_id": "lookup-call"}
            ],
        )
        advance = adapter.after_execution(
            "tenant", session, skill, requirement, result, None, remaining_actions=1
        )
        assert advance.continue_execution and session.active_step_id == "done"
        result = TaskExecutionResult(
            task_frame_id="task1", status="completed", reply_fragment="Done"
        )
        adapter.after_execution(
            "tenant", session, skill, requirement, result, None, remaining_actions=1
        )
        assert session.active_skill_id is None
        assert client.session(adapter._id(session, "task1"))["state"]["status"] == "completed"
        frame.task_id = "task2"
        adapter.activate_frame(session, frame, [skill])
        assert session.active_step_id == "collect" and session.slots_json == {}
        assert client.session(adapter._id(session, "task2"))["state"]["revision"] == 0


def test_staffdeck_same_execution_loop_can_wait_and_resume_twice():
    content = {
        "start_node_id": "collect",
        "nodes": [{"node_id": "collect", "expected_user_info": ["name"]}],
        "terminal_node_ids": ["collect"],
    }
    view = SimpleNamespace(skill_id="repeat", version="1", name="Repeat", content=content)
    with TestClient(create_app()) as http:
        requests = []

        def transport(path, payload, headers):
            if payload is not None:
                requests.append(deepcopy(payload))
            response = (
                http.get(path, headers=headers)
                if payload is None
                else http.post(path, json=payload, headers=headers)
            )
            return response.json()

        client = RuntimeClient("http://runtime", transport=transport)
        client.request("/v1/definitions", SopManagementService.snapshot(view, "tenant", "agent"))
        adapter = HttpSopRuntime(
            client, SopDependencies(SimpleNamespace(record=lambda *a: None), lambda *a: None)
        )
        session = SopState("repeat-chat", "tenant", agent_id="agent")
        skill = SopDefinition("row", "tenant", "repeat", "1", "Repeat", deepcopy(content))
        frame = SimpleNamespace(
            tenant_id="tenant",
            session_id="repeat-chat",
            task_id="task",
            kind="sop",
            skill_id="repeat",
        )
        adapter.activate_frame(session, frame, [skill])
        requirement = TaskRequirement(
            task_frame_id="task", execution_loop_id="same-loop", kind="sop", goal="test"
        )
        result = TaskExecutionResult(
            task_frame_id="task",
            status="awaiting_user",
            reply_fragment="First question",
            action_count=1,
        )
        adapter.after_execution(
            "tenant", session, skill, requirement, result, None, remaining_actions=1
        )
        requirement.current_user_message = "First answer"
        result.reply_fragment = "Second question"
        adapter.after_execution(
            "tenant", session, skill, requirement, result, None, remaining_actions=1
        )
        requirement.current_user_message = "Second answer"
        result.status, result.reply_fragment = "completed", "Done"
        result.slot_updates = {"name": "Ada"}
        adapter.after_execution(
            "tenant", session, skill, requirement, result, None, remaining_actions=1
        )
        runtime_id = adapter._id(session, "task")
        assert client.session(runtime_id)["state"]["status"] == "completed"
        assert client.session(runtime_id)["state"]["revision"] == 5
        mutations = [p for p in requests if "expectedRevision" in p]
        assert len({p["requestId"] for p in mutations}) == 5
        # Retrying the exact final HTTP request must still replay its response.
        replay = client.request(
            f"/v1/sessions/{__import__('urllib.parse', fromlist=['quote']).quote(runtime_id, safe='')}/submit",
            mutations[-1],
        )
        assert replay["state"]["revision"] == 5


def test_management_snapshot_is_scoped_and_does_not_retain_host_objects():
    view = SimpleNamespace(
        skill_id="parent",
        version="1",
        name="Parent",
        content={
            "start_node_id": "sub",
            "nodes": [{"node_id": "sub", "type": "subflow", "sub_sop_id": "child"}],
        },
    )
    one = SopManagementService.snapshot(view, "tenant1", "agent")
    two = SopManagementService.snapshot(view, "tenant2", "agent")
    assert one["sop_id"] != two["sop_id"]
    assert one["content"]["nodes"][0]["sub_sop_id"] != "child"
    assert view.content["nodes"][0]["sub_sop_id"] == "child"


def test_runtime_nested_nodes_match_staffdeck_compiler_node_identifiers():
    from app.skills.nesting import _expand_content

    parent = {
        "skill_id": "parent",
        "start_node_id": "sub",
        "terminal_node_ids": ["sub"],
        "nodes": [{"node_id": "sub", "type": "subflow", "sub_sop_id": "child"}],
    }
    child = {
        "skill_id": "child",
        "start_node_id": "done",
        "terminal_node_ids": ["done"],
        "nodes": [{"node_id": "done"}],
    }
    expanded = _expand_content(
        "parent", deepcopy(parent), {"child": SimpleNamespace(content_json=child)}, path=["parent"]
    )
    with TestClient(create_app()) as http:
        for identifier, content in [("child", child), ("parent", parent)]:
            view = SimpleNamespace(
                skill_id=identifier, version="1", name=identifier, content=content
            )
            assert (
                http.post(
                    "/v1/definitions", json=SopManagementService.snapshot(view, "tenant", "agent")
                ).status_code
                == 200
            )
        response = http.post(
            "/v1/sessions/nested/prepare",
            json={
                "protocolVersion": "3.0",
                "requestId": "p",
                "operationId": "p",
                "turnId": "t",
                "sopId": '["staffdeck","tenant","agent","parent"]',
            },
        )
        assert response.status_code == 200, response.text
        pinned = http.get("/v1/sessions/nested").json()["definition"]["content"]
        assert pinned.pop("capability_requirements") == []
        assert pinned == expanded
