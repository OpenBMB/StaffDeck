from __future__ import annotations

import pytest

from app.core.pilotdeck_agent_loop_client import StaffDeckModuleError
from app.core.pilotdeck_module_bridge import StaffDeckPilotDeckModuleBridge
from app.core.turn_coordinator import (
    _HarnessV3SidecarCapabilityInvoker,
    _save_sidecar_checkpoint,
)
from staffdeck_harness.contracts.invocation import ModuleResult


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.requests: list[tuple[str, object]] = []

    def generate_json(self, system_prompt, user_payload):
        self.requests.append((system_prompt, user_payload))
        return self.response

    def generate_text_stream(self, *_args, **_kwargs):
        raise AssertionError("text fallback must not be called")


class SequenceModel(FakeModel):
    def generate_json_sequence(self, system_prompt, user_payload):
        self.requests.append((system_prompt, user_payload))
        return self.response


class FakeInvoker:
    def __init__(self, result):
        self.result = result

    def invoke(self, _name, _arguments):
        return self.result


class FakeExecutionHost:
    def __init__(self, result, receipt=None):
        self.result = result
        self.receipt = receipt
        self.calls = []
        self.capabilities = type(
            "Capabilities",
            (),
            {"discover_artifacts": lambda _self, _ctx: []},
        )()

    def invoke_proxy(self, name, arguments, context):
        self.calls.append((name, arguments, context))
        return self.result, self.receipt


def _sidecar_manifest(*, kind="tool", operation="tool.invoke/v1"):
    descriptor = type(
        "Descriptor",
        (),
        {
            "name": "lookup",
            "kind": kind,
            "capability_id": "tool-1",
            "available": True,
            "metadata": {
                "operation": operation,
                "resource_id": "tool-1",
            },
        },
    )()
    return type("Manifest", (), {"available": [descriptor]})()


def test_sidecar_capability_uses_harness_v3_execution_host() -> None:
    execution_host = FakeExecutionHost(ModuleResult.ok({"answer": 42}))
    invoker = _HarnessV3SidecarCapabilityInvoker(
        execution_host,
        lambda trace_id: {"trace_id": trace_id},
        _sidecar_manifest(),
    )

    result = invoker.invoke("lookup", {"q": "meaning"})

    assert result["success"] is True
    assert result["data"] == {"answer": 42}
    assert execution_host.calls[0][0:2] == (
        "tool_invoke",
        {"tool_id": "tool-1", "arguments": {"q": "meaning"}},
    )


def test_sidecar_capability_fails_closed_for_unknown_outcome() -> None:
    receipt = type(
        "Receipt",
        (),
        {"status": "outcome_unknown", "to_json": lambda _self: {}},
    )()
    execution_host = FakeExecutionHost(
        ModuleResult.fail("HARNESS_TOOL_ERROR", "provider disconnected"),
        receipt,
    )
    invoker = _HarnessV3SidecarCapabilityInvoker(
        execution_host,
        lambda trace_id: {"trace_id": trace_id},
        _sidecar_manifest(),
    )

    result = invoker.invoke("lookup", {})

    assert result["success"] is False
    assert result["outcome"] == "result_unknown"
    assert result["error"]["code"] == "RESULT_UNKNOWN"


def test_sidecar_checkpoint_preserves_host_owned_state() -> None:
    saved = []
    store = type(
        "Store",
        (),
        {
            "db": type("DB", (), {"commit": lambda _self: None})(),
            "save_agent_loop_checkpoint": lambda _self, *args, **kwargs: saved.append(
                (args, kwargs)
            ),
        },
    )()
    current = {"host": "preserved", "iteration": 1}

    result = _save_sidecar_checkpoint(
        store,
        object(),
        {"checkpoint": {"iteration": 2, "sidecar": "added"}},
        "run-1",
        current,
    )

    assert result == {"accepted": True}
    assert current == {
        "host": "preserved",
        "iteration": 2,
        "sidecar": "added",
    }
    assert saved[0][0][1] == current


def test_model_preserves_multimodal_messages_without_text_fallback() -> None:
    model = FakeModel({"reply_fragment": "看到了图片。"})
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    result = bridge.model(
        {
            "request": {
                "systemPrompt": "system",
                "messages": [
                    {
                        "role": "user",
                        "content": "请描述图片",
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AAAA"},
                            }
                        ],
                    }
                ],
                "tools": [],
                "toolChoice": "auto",
                "metadata": {"source": "test"},
            }
        }
    )

    assert {"type": "text_delta", "text": "看到了图片。"} in result["events"]
    assert model.requests[0][1]["conversation_context"]["messages"][0]["images"][0]["image_url"]["url"].startswith("data:image/png")
    assert model.requests[0][1]["canonical_messages"][0]["role"] == "user"


def test_model_merges_execution_metadata_with_explicit_request_metadata() -> None:
    model = FakeModel({"reply_fragment": "ok"})
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    bridge.model({
        "context": {"metadata": {"iteration": 3, "shared": "execution"}},
        "request": {
            "systemPrompt": "system",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "q"}]}],
            "metadata": {"shared": "request"},
        },
    })

    assert model.requests[0][1]["metadata"] == {
        "iteration": 3,
        "shared": "request",
    }


def test_model_error_is_not_downgraded_to_text_generation() -> None:
    model = FakeModel(None)
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    with pytest.raises(TypeError, match="empty or invalid JSON action"):
        bridge.model({"systemPrompt": "system", "userPayload": "payload"})


def test_permission_fails_closed_without_checker() -> None:
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=FakeModel({"reply_fragment": "ok"}),
        capability_invoker=FakeInvoker({"success": True}),
    )

    result = bridge.permission({"toolName": "exec_command"})

    assert result["allowed"] is False
    assert result["error"]["code"] == "PERMISSION_UNAVAILABLE"


def test_capability_error_result_keeps_host_error_shape() -> None:
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=FakeModel({"reply_fragment": "ok"}),
        capability_invoker=FakeInvoker(
            {
                "type": "error",
                "toolCallId": "call-1",
                "toolName": "lookup",
                "error": {"code": "CAPABILITY_AUTHORIZATION_REVOKED", "message": "revoked"},
                "content": [{"type": "text", "text": "revoked"}],
            }
        ),
    )

    result = bridge.capability({"name": "lookup", "toolCallId": "call-1", "arguments": {}})

    assert result["type"] == "error"
    assert result["error"]["code"] == "CAPABILITY_AUTHORIZATION_REVOKED"
    assert result["toolCallId"] == "call-1"


def test_action_budget_is_consumed_by_capability_actions() -> None:
    model = FakeModel({"reply_fragment": "ok"})
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
        remaining_actions=1,
    )
    bridge.model({"systemPrompt": "system", "userPayload": "first"})
    bridge.capability({"name": "lookup", "toolCallId": "call-1", "arguments": {}})
    with pytest.raises(StaffDeckModuleError, match="action budget exhausted"):
        bridge.model({"systemPrompt": "system", "userPayload": "second"})
    assert len(model.requests) == 1


def test_knowledge_search_budget_is_enforced_by_the_host_bridge() -> None:
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=FakeModel({"reply_fragment": "ok"}),
        capability_invoker=FakeInvoker({
            "success": True,
            "data": {"evidence_pack": [{"content": "evidence"}]},
        }),
        successful_knowledge_searches=1,
    )

    bridge.capability({"name": "knowledge_search", "arguments": {}})
    with pytest.raises(StaffDeckModuleError, match="两次有效知识检索") as exc_info:
        bridge.capability({"name": "knowledge_search", "arguments": {}})

    assert exc_info.value.code == "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED"


def test_model_projects_tool_history_with_name_and_json_result() -> None:
    model = FakeModel({"reply_fragment": "继续处理"})
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    bridge.model(
        {
            "request": {
                "systemPrompt": "system",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_call",
                                "id": "call-1",
                                "name": "lookup",
                                "input": {"q": "status"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "toolCallId": "call-1",
                                "content": [{"type": "text", "text": '{"ok":true}'}],
                            }
                        ],
                    },
                ],
            }
        }
    )

    transcript = model.requests[0][1]["harness_transcript"]
    assert transcript[0]["tool_name"] == "lookup"
    assert transcript[1]["tool_name"] == "lookup"
    assert transcript[1]["result"]["data"] == {"ok": True}


def test_model_preserves_raw_tool_error_code_and_empty_data() -> None:
    model = FakeModel({"reply_fragment": "继续处理"})
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    bridge.model({
        "request": {
            "systemPrompt": "system",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "toolCallId": "call-1",
                    "isError": True,
                    "content": [{"type": "text", "text": "failed"}],
                    "raw": {
                        "type": "error",
                        "toolName": "lookup",
                        "error": {
                            "code": "MOCK_TOOL_ERROR",
                            "message": "failed",
                            "retryable": False,
                        },
                    },
                }],
            }],
        },
    })

    result = model.requests[0][1]["harness_transcript"][0]["result"]
    assert result["data"] is None
    assert result["error"]["code"] == "MOCK_TOOL_ERROR"


def test_model_emits_all_sequence_tool_calls_in_order() -> None:
    model = SequenceModel(
        [
            {"action": "tool", "tool_name": "lookup", "arguments": {"q": "a"}},
            {"action": "tool", "tool_name": "summarize", "arguments": {"q": "b"}},
        ]
    )
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    events = bridge.model({"systemPrompt": "system", "userPayload": "payload"})["events"]
    calls = [event["toolCall"]["name"] for event in events if event["type"] == "tool_call_end"]
    assert calls == ["lookup", "summarize"]


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("handoff", {"handoff": True}),
        ("awaiting_user", {"slot_updates": {"date": ""}}),
        ("blocked", {"next_step_id": None}),
    ],
)
def test_model_preserves_structured_terminal_action(status: str, extra: dict[str, object]) -> None:
    model = FakeModel({
        "action": "finish",
        "status": status,
        "reply_fragment": "请继续",
        "task_summary": "structured terminal",
        **extra,
    })
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    events = bridge.model({"systemPrompt": "system", "userPayload": "payload"})["events"]
    text = "".join(event["text"] for event in events if event["type"] == "text_delta")
    assert "__STAFFDECK_TASK_RESULT__=" in text
    assert f'"status":"{status}"' in text
    assert "请继续" in text


def test_model_unwraps_json_action_returned_as_provider_message_text() -> None:
    model = FakeModel({
        "action": "finish",
        "status": "completed",
        "reply_fragment": '{"action":"finish","status":"handoff","reply_fragment":"转人工","handoff":true}',
    })
    bridge = StaffDeckPilotDeckModuleBridge(
        model_client=model,
        capability_invoker=FakeInvoker({"success": True}),
    )

    events = bridge.model({"systemPrompt": "system", "userPayload": "payload"})["events"]
    text = "".join(event["text"] for event in events if event["type"] == "text_delta")
    assert '"status":"handoff"' in text
    assert '"handoff":true' in text
    assert '"status":"completed"' not in text
