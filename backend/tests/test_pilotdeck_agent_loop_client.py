from __future__ import annotations

import json
import sys

import pytest

from app.core.pilotdeck_agent_loop_client import (
    PilotDeckAgentLoopClient,
    PilotDeckAgentLoopError,
    PilotDeckExecutionContext,
    PilotDeckExecutionIdentity,
)
from app.core.harness_agent import HarnessExecutionFenced
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    TaskExecutionResult,
    TaskRequirement,
)

SIDECAR = r'''
import json, sys
CURRENT_RUN_ID = ""
CURRENT_OPERATION_ID = ""
CURRENT_REQUEST_ID = ""
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        CURRENT_RUN_ID = msg["runId"]
        CURRENT_OPERATION_ID = msg["operationId"]
        CURRENT_REQUEST_ID = msg["requestId"]
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        print(json.dumps({"kind":"request","messageId":"module-call","method":"module_call","runId":msg["runId"],"operationId":msg["operationId"],"requestId":"module-request","module":"capability","payload":{"name":"lookup","arguments":{}}}), flush=True)
    elif msg.get("kind") == "response" and msg.get("inReplyTo") == "module-call":
        print(json.dumps({"kind":"event","messageId":"event-1","eventType":"agent.tool_result","streamId":"stream-1","sequence":0,"runId":CURRENT_RUN_ID,"operationId":CURRENT_OPERATION_ID,"requestId":CURRENT_REQUEST_ID,"final":False,"payload":{"tool":"lookup"}}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":1,"runId":CURRENT_RUN_ID,"operationId":CURRENT_OPERATION_ID,"requestId":CURRENT_REQUEST_ID,"final":True,"outcome":"completed","payload":{"result":{"task_frame_id":"frame-1","status":"completed","reply_fragment":"done","action_count":1}}}), flush=True)
'''


def _requirement() -> TaskRequirement:
    return TaskRequirement(task_frame_id="frame-1", kind="conversation", goal="lookup")


def test_sidecar_client_dispatches_host_module_call_and_decodes_result() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", SIDECAR])
    traces: list[str] = []
    result = client.execute(
        _requirement(),
        identity=PilotDeckExecutionIdentity(
            tenant_id="tenant-1",
            session_id="session-1",
            turn_id="turn-1",
            run_id="run-1",
            operation_id="op-1",
            idempotency_key="frame-1",
        ),
        bridge=lambda module, payload: {"success": True, "module": module, "payload": payload},
        trace_sink=lambda event_type, payload: traces.append(event_type),
    )
    client.close()
    assert result.status == "completed"
    assert result.reply_fragment == "done"
    assert traces == ["agent.tool_result"]


def test_sidecar_client_serializes_fenced_host_capability_as_result_unknown() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", SIDECAR])

    def bridge(_module: str, _payload: dict[str, object]) -> dict[str, object]:
        raise HarnessExecutionFenced("capability acknowledgement was lost")

    response = client._dispatch_module_call(
        {
            "messageId": "module-call",
            "requestId": "module-request",
            "module": "capability",
            "payload": {"name": "write_file", "arguments": {}},
        },
        bridge,
    )
    client.close()

    assert response == {
        "kind": "response",
        "messageId": "module-error-1",
        "inReplyTo": "module-call",
        "requestId": "module-request",
        "ok": False,
        "final": True,
        "outcome": "result_unknown",
        "code": "RESULT_UNKNOWN",
        "error": {
            "code": "RESULT_UNKNOWN",
            "message": "capability acknowledgement was lost",
        },
    }


def _identity() -> PilotDeckExecutionIdentity:
    return PilotDeckExecutionIdentity(
        tenant_id="tenant-1",
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
        operation_id="op-1",
        idempotency_key="frame-1",
    )


def _client_script(final_payload: str, *, outcome: str = "completed", request_id_expr: str = 'msg["requestId"]', code: str | None = None, error: dict[str, str] | None = None) -> str:
    code_field = f",\"code\":{code!r}" if code is not None else ""
    error_field = f",\"error\":{json.dumps(error)}" if error is not None else ""
    return f'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({{"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}}), flush=True)
    elif msg.get("method") == "execute":
        print(json.dumps({{"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}}), flush=True)
        print(json.dumps({{"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":{request_id_expr},"final":True,"outcome":{outcome!r}{code_field}{error_field},"payload":{final_payload}}}), flush=True)
'''


def test_client_rejects_failed_outcome_even_when_payload_looks_successful() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(
        '{"result":{"task_frame_id":"frame-1","status":"completed"}}',
        outcome="failed",
    )])
    with pytest.raises(PilotDeckAgentLoopError) as exc_info:
        client.execute(_requirement(), identity=_identity())
    client.close()
    assert getattr(exc_info.value, "code", "") == "EXECUTE_FAILED"


def test_client_preserves_result_unknown_terminal_error() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.result_unknown","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"result_unknown","code":"RESULT_UNKNOWN","error":{"code":"RESULT_UNKNOWN","message":"capability acknowledgement was lost"},"payload":{}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    with pytest.raises(PilotDeckAgentLoopError) as exc_info:
        client.execute(_requirement(), identity=_identity())
    client.close()

    assert exc_info.value.code == "RESULT_UNKNOWN"
    assert str(exc_info.value) == "capability acknowledgement was lost"
    assert exc_info.value.result_unknown is True


def test_sidecar_client_projects_knowledge_evidence_into_host_checkpoint() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", SIDECAR])
    result = client._finalize_result(
        TaskExecutionResult(task_frame_id="frame-1", status="completed"),
        {},
        {"successful_knowledge_searches": 1},
        [{
            "tool_name": "knowledge_search",
            "success": True,
            "data": {
                "evidence_pack": [{"knowledge_base_id": "handbook", "content": "evidence"}],
            },
            "error": None,
        }],
        [],
    )
    client.close()

    assert result.loop_checkpoint["successful_knowledge_searches"] == 2
    assert result.evidence_results == [{
        "evidence_pack": [{"knowledge_base_id": "handbook", "content": "evidence"}],
    }]


def test_sidecar_client_preserves_a_terminal_knowledge_budget_failure() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", SIDECAR])
    result = client._finalize_result(
        TaskExecutionResult(task_frame_id="frame-1", status="completed"),
        {},
        {},
        [{
            "tool_name": "knowledge_search",
            "success": False,
            "data": None,
            "error": {
                "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
                "message": "knowledge budget exhausted",
            },
        }],
        [],
    )
    client.close()

    assert result.status == "failed"
    assert result.error == {
        "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
        "message": "knowledge budget exhausted",
    }


def test_client_projects_terminal_knowledge_budget_failure_without_generic_wrapping() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(
        "{}",
        outcome="failed",
        code="KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
        error={
            "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
            "message": "third knowledge search was rejected",
        },
    )])
    result = client.execute(_requirement(), identity=_identity())
    client.close()

    assert result.status == "failed"
    assert result.error == {
        "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
        "message": "third knowledge search was rejected",
    }


def test_client_rejects_malformed_completed_payload() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(
        '{"result":{"task_frame_id":"frame-1","status":"not-a-status"}}',
    )])
    with pytest.raises(PilotDeckAgentLoopError) as exc_info:
        client.execute(_requirement(), identity=_identity())
    client.close()
    assert getattr(exc_info.value, "code", "") == "INVALID_RESULT"


def test_client_maps_generic_agent_turn_success_to_staffdeck_result() -> None:
    payload = {
        "result": {
            "type": "success",
            "sessionId": "session-1",
            "turnId": "turn-1",
            "stopReason": "completed",
            "usage": {"totalTokens": 4},
            "permissionDenials": [],
            "turns": 1,
            "startedAt": "2026-01-01T00:00:00Z",
            "completedAt": "2026-01-01T00:00:01Z",
            "finalMessage": {"role": "assistant", "content": [{"type": "text", "text": "普通成功回复"}]},
        },
        "messages": [],
    }
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(json.dumps(payload))])
    result = client.execute(_requirement(), identity=_identity())
    client.close()
    assert result.status == "completed"
    assert result.reply_fragment == "普通成功回复"
    assert result.action_count == 1


def test_client_maps_action_budget_module_failure_to_action_budget_result() -> None:
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(
        '{"moduleFailure":{"code":"ACTION_BUDGET_EXHAUSTED","message":"StaffDeck action budget exhausted before the next model action."}}',
        outcome="failed",
        code="agent_model_error",
    )])
    result = client.execute(_requirement(), identity=_identity())
    client.close()
    assert result.status == "action_budget"
    assert result.error == {"code": "ACTION_BUDGET_EXHAUSTED", "message": "StaffDeck action budget exhausted."}


def test_client_ignores_event_from_old_request_id() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        for request_id, sequence in [("old-request", 0), (msg["requestId"], 0)]:
            print(json.dumps({"kind":"event","messageId":"event-"+request_id,"eventType":"agent.event","streamId":"stream-1","sequence":sequence,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":request_id,"final":False,"payload":{"requestId":request_id}}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":1,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"completed","payload":{"result":{"task_frame_id":"frame-1","status":"completed","reply_fragment":"current","action_count":1}}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    traces: list[tuple[str, dict[str, object]]] = []
    result = client.execute(_requirement(), identity=_identity(), trace_sink=lambda event, payload: traces.append((event, payload)))
    client.close()
    assert result.reply_fragment == "current"
    assert traces == [("agent.event", {"requestId": "execute-2"})]


def test_client_does_not_accept_completed_after_cancellation() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
    elif msg.get("method") == "cancel":
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"completed","payload":{"result":{"task_frame_id":"frame-1","status":"completed","reply_fragment":"late","action_count":1}}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    with pytest.raises(PilotDeckAgentLoopError) as exc_info:
        client.execute(_requirement(), identity=_identity(), is_cancelled=lambda: True)
    client.close()
    assert getattr(exc_info.value, "code", "") == "CANCELLED_AFTER_REQUEST"


def test_client_sends_ephemeral_execution_context() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        payload = msg["payload"]
        ok = payload.get("executionContext", {}).get("remainingActions") == 3 and payload.get("messages", [{}])[0].get("images")
        result = {"task_frame_id":"frame-1","status":"completed","reply_fragment":"context-ok" if ok else "context-missing","action_count":1}
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"completed","payload":{"result":result}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    result = client.execute(
        _requirement(),
        identity=_identity(),
        execution_context=PilotDeckExecutionContext(
            remaining_actions=3,
            conversation_context={"messages": [{"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]},
        ),
    )
    client.close()
    assert result.reply_fragment == "context-ok"


def test_client_projects_generic_payload_and_only_supported_seed_state() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        payload = msg["payload"]
        ok = (
            set(payload) == {"task", "tools", "messages", "permissionContext", "executionContext", "seedState"}
            and payload["task"]["prompt"].find('"goal": "lookup"') >= 0
            and payload["tools"][0]["name"] == "lookup"
            and payload["seedState"] == {"allowedReadFiles": ["/workspace/input.txt"]}
        )
        result = {"task_frame_id":"frame-1","status":"completed","reply_fragment":"generic-ok" if ok else "generic-missing","action_count":1}
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"completed","payload":{"result":result}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    requirement = TaskRequirement(
        task_frame_id="frame-1",
        kind="conversation",
        goal="lookup",
        capability_manifest=CapabilityManifest(available=[CapabilityDescriptor(
            capability_id="lookup",
            name="lookup",
            kind="tool",
            input_schema={"type": "object"},
        )]),
    )
    result = client.execute(
        requirement,
        identity=_identity(),
        checkpoint={"agentLoopSeedState": {"allowedReadFiles": ["/workspace/input.txt"]}, "hostOnly": "opaque"},
        execution_context=PilotDeckExecutionContext(
            remaining_actions=3,
            conversation_context={"messages": [{"images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]},
            permission_context={"mode": "default", "canPrompt": False},
        ),
    )
    client.close()
    assert result.reply_fragment == "generic-ok"


def test_client_projects_host_context_override_without_persisting_image_data() -> None:
    script = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}), flush=True)
    elif msg.get("method") == "execute":
        payload = msg["payload"]
        override = payload.get("contextOverride", {})
        messages = override.get("messages", [])
        ok = (
            override.get("systemPrompt") == "host-system"
            and len(messages) == 3
            and messages[1]["content"][0]["type"] == "tool_call"
            and messages[2]["content"][0]["type"] == "tool_result"
            and messages[2]["content"][0]["isError"] is True
            and override["metadata"]["iteration"] == 2
            and ";base64," not in json.dumps(payload.get("seedState", {}))
        )
        result = {"task_frame_id":"frame-1","status":"completed","reply_fragment":"override-ok" if ok else "override-missing","action_count":1}
        print(json.dumps({"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}), flush=True)
        print(json.dumps({"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":msg["runId"],"operationId":msg["operationId"],"requestId":msg["requestId"],"final":True,"outcome":"completed","payload":{"result":result}}), flush=True)
'''
    checkpoint = {
        "agentLoopSeedState": {"allowedReadFiles": ["/workspace/input.txt"]},
    }
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    result = client.execute(
        _requirement(),
        identity=_identity(),
        checkpoint=checkpoint,
        execution_context=PilotDeckExecutionContext(
            context_override={
                "systemPrompt": "host-system",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "task"}]},
                    {"role": "assistant", "content": [{"type": "tool_call", "id": "call-1", "name": "lookup", "input": {}}]},
                    {"role": "assistant", "content": [{"type": "tool_result", "toolCallId": "call-1", "content": [{"type": "text", "text": "denied"}], "isError": True}]},
                ],
                "metadata": {"iteration": 2},
            },
        ),
    )
    client.close()
    assert result.reply_fragment == "override-ok"


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("handoff", {"handoff": True}),
        ("awaiting_user", {"slot_updates": {"date": ""}}),
        ("blocked", {"next_step_id": None}),
    ],
)
def test_client_decodes_staffdeck_structured_terminal_carrier(status: str, extra: dict[str, object]) -> None:
    carrier = {
        "action": "finish",
        "status": status,
        "reply_fragment": "请继续",
        "task_summary": "structured terminal",
        **extra,
    }
    carrier_text = (
        "请继续"
        "__STAFFDECK_TASK_RESULT__="
        + json.dumps(carrier, ensure_ascii=False, separators=(",", ":"))
        + "__END__"
    )
    payload = {
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": carrier_text}]}],
    }
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(json.dumps(payload))])
    result = client.execute(_requirement(), identity=_identity(), checkpoint={})
    client.close()
    assert result.status == status
    assert result.reply_fragment == "请继续"
    assert "__STAFFDECK_TASK_RESULT__=" not in result.reply_fragment
    checkpoint_messages = result.loop_checkpoint["agentLoopMessages"]
    assert "__STAFFDECK_TASK_RESULT__=" not in checkpoint_messages[0]["content"][0]["text"]


def test_client_projects_sidecar_tool_history_into_staffdeck_result() -> None:
    carrier = {
        "action": "finish",
        "status": "completed",
        "reply_fragment": "done",
    }
    payload = {
        "finalMessage": {"role": "assistant", "content": [{
            "type": "text",
            "text": (
                "done"
                "__STAFFDECK_TASK_RESULT__="
                + json.dumps(carrier, separators=(",", ":"))
                + "__END__"
            ),
        }]},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "task"}]},
            {"role": "assistant", "content": [{
                "type": "tool_call",
                "id": "call-lookup",
                "name": "lookup",
                "input": {"q": "collect"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "toolCallId": "call-lookup",
                "raw": {
                    "type": "success",
                    "toolCallId": "call-lookup",
                    "toolName": "lookup",
                    "data": {"value": "found"},
                },
            }]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ],
    }

    script = f'''
import json, sys
execute = None
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "hello":
        print(json.dumps({{"kind":"response","messageId":"hello-res","inReplyTo":msg["messageId"],"ok":True,"connectionGeneration":"test-connection"}}), flush=True)
    elif msg.get("method") == "execute":
        execute = msg
        print(json.dumps({{"kind":"response","messageId":"accepted","inReplyTo":msg["messageId"],"requestId":msg["requestId"],"ok":True,"streamId":"stream-1","cursor":0}}), flush=True)
        print(json.dumps({{"kind":"request","messageId":"module-call","method":"module_call","runId":msg["runId"],"operationId":msg["operationId"],"requestId":"module-request","module":"capability","payload":{{"name":"lookup","arguments":{{"q":"collect"}}}}}}), flush=True)
    elif msg.get("kind") == "response" and msg.get("inReplyTo") == "module-call":
        print(json.dumps({{"kind":"event","messageId":"final-1","eventType":"agent.execute.completed","streamId":"stream-1","sequence":0,"runId":execute["runId"],"operationId":execute["operationId"],"requestId":execute["requestId"],"final":True,"outcome":"completed","payload":{json.dumps(payload)}}}), flush=True)
'''
    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", script])
    result = client.execute(
        _requirement(),
        identity=_identity(),
        bridge=lambda module, event: {
            "type": "success",
            "toolCallId": "call-lookup",
            "toolName": event["name"],
            "data": {"value": "found"},
        },
    )
    client.close()

    assert result.action_count == 2
    assert result.capability_results == [{
        "tool_name": "lookup",
        "success": True,
        "data": {"value": "found"},
        "error": None,
    }]


def test_client_rejects_completed_carrier_without_required_capability() -> None:
    carrier = {
        "action": "finish",
        "status": "completed",
        "reply_fragment": "done",
    }
    payload = {
        "messages": [{"role": "assistant", "content": [{
            "type": "text",
            "text": (
                "__STAFFDECK_TASK_RESULT__="
                + json.dumps(carrier, separators=(",", ":"))
                + "__END__"
            ),
        }]}],
    }
    requirement = TaskRequirement(
        task_frame_id="frame-1",
        kind="sop",
        goal="must call lookup",
        required_capability_names=["lookup"],
    )

    client = PilotDeckAgentLoopClient([sys.executable, "-u", "-c", _client_script(json.dumps(payload))])
    result = client.execute(requirement, identity=_identity())
    client.close()

    assert result.status == "action_budget"
    assert result.reply_fragment == "当前任务已达到本轮自动执行上限，需要下一轮继续。"
    assert result.error == {
        "code": "ACTION_BUDGET_EXHAUSTED",
        "message": "StaffDeck action budget exhausted.",
    }
