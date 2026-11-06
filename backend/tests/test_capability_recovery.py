from types import SimpleNamespace as NS
import threading

import pytest

from app.core import harness_agent as harness
from app.core.capability_recovery import CapabilityRecovery
from app.core.task_request_compiler import (
    CapabilityCatalogEntry, CapabilityDescriptor, CapabilityManifest, TaskExecutionResult,
    TaskRequirement, _current_time_text,
)
from app.db.models import ModelConfig
from staffdeck_harness.bridge.control import ExecutionHost
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.sop.results import enforce_required_slots


def run(monkeypatch, actions, invoke, *, catalog=(), checkpoint=None):
    iterator = iter(actions)
    payloads, events = [], []

    class Model:
        def __init__(self, *a, **k):
            pass

        def generate_json(self, system, payload, **kwargs):
            payloads.append(payload)
            return next(iterator)

    monkeypatch.setattr(harness, "LLMClient", Model)
    req = TaskRequirement(task_frame_id="frame", kind="sop", goal="申请算力",
        source_user_message="申请4张卡", current_user_message="确认提交",
        known_slots={"gpu_count": 4, "confirmed": True},
        capability_manifest=CapabilityManifest(snapshot_revision="rev", catalog=list(catalog), available=[
            CapabilityDescriptor(capability_id="describe", name="capability_describe", kind="internal"),
            CapabilityDescriptor(capability_id="current", name="session.current", kind="internal"),
        ]))
    result = harness.HarnessTaskAgent().run(req,
        ModelConfig(id="fake", tenant_id="t", name="fake", model="fake", provider="openai_compatible"),
        invoke, max_actions=32, checkpoint=checkpoint, trace_sink=lambda e, p: events.append((e, p)))
    return result, payloads, events


def tool(name, **args):
    return {"action": "tool", "tool_name": name, "arguments": args}


def test_reported_confirm_submit_loop_expands_authorized_schema_before_one_write(monkeypatch):
    calls = []

    def invoke(name, args):
        calls.append((name, args))
        if name == "capability_describe":
            return {"success": True, "data": {"snapshot_revision": "rev", "activated_capabilities": [
                CapabilityDescriptor(capability_id="compute", name="compute_quota", kind="tool",
                    input_schema={"type": "object", "required": ["gpu_count", "confirmed"]}).model_dump()
            ]}}
        assert args == {"gpu_count": 4, "confirmed": True}
        return {"success": True, "tool_name": name, "data": {"id": "SIMULATED"}}

    actions = [tool("compute_quota", guessed=True), tool("compute_quota", gpu_count=4, confirmed=True),
               {"action": "finish", "status": "completed", "reply_fragment": "模拟完成"}]
    result, payloads, events = run(monkeypatch, actions, invoke, catalog=[
        CapabilityCatalogEntry(capability_id="compute", name="compute_quota", kind="tool")])
    assert result.status == "completed" and result.action_count == 3
    assert calls == [("capability_describe", {"capabilities": ["compute_quota"]}),
                     ("compute_quota", {"gpu_count": 4, "confirmed": True})]
    assert any(e == "harness_action_failed" and p["error"]["code"] == "CAPABILITY_SCHEMA_LOADED" for e, p in events)
    assert payloads[-1]["task_requirement"]["known_slots"] == {"gpu_count": 4, "confirmed": True}


def test_unknown_capability_stops_after_three_rejections_even_with_intervening_reads(monkeypatch):
    calls = []
    actions = [tool("missing", variant=1), tool("session.current"), tool("missing", variant=2),
               tool("session.current"), tool("missing", variant=3)]
    result, _, events = run(monkeypatch, actions, lambda n, a: calls.append(n) or {"success": True})
    assert calls == ["session.current", "session.current"]
    assert result.status == "awaiting_user" and result.next_step_id is None
    assert result.error["code"] == "CAPABILITY_RECOVERY_EXHAUSTED"
    assert result.action_count == 5
    assert len([e for e, _ in events if e == "harness_action_failed"]) == 3


@pytest.mark.parametrize("describe_result", [
    {"success": False, "error": {"code": "PERMISSION_DENIED", "message": "denied"}},
    {"success": True, "data": {"snapshot_revision": "stale", "activated_capabilities": [
        {"capability_id": "compute", "name": "compute_quota", "kind": "tool"}]}}
])
def test_automatic_description_never_bypasses_permission_or_snapshot(monkeypatch, describe_result):
    calls = []
    result, _, _ = run(monkeypatch, [tool("compute_quota")] * 3,
        lambda n, a: calls.append(n) or describe_result,
        catalog=[CapabilityCatalogEntry(capability_id="compute", name="compute_quota", kind="tool")])
    assert calls == ["capability_describe"] and result.status == "awaiting_user"
    expected_code = "PERMISSION_DENIED" if not describe_result["success"] else "CAPABILITY_SNAPSHOT_CHANGED"
    assert result.error["details"]["cause_code"] == expected_code


def test_resume_preserves_original_intent_but_injects_latest_confirmation(monkeypatch):
    _, payloads, _ = run(monkeypatch, [{"action": "finish", "status": "awaiting_user"}], lambda *a: {},
        checkpoint={"task_frame_id": "frame", "transcript": [{"role": "assistant", "content": "请确认"}]})
    assert {"role": "user", "content": "确认提交"} in payloads[0]["harness_transcript"]
    assert payloads[0]["task_requirement"]["source_user_message"] == "申请4张卡"


@pytest.mark.parametrize("value", [None, "", "   ", [], {}])
def test_completion_rechecks_slots_that_were_filled_before_the_step(value):
    req = TaskRequirement(task_frame_id="f", kind="sop", goal="collect", required_slots=[],
                          expected_slots=["gpu_count"], known_slots={"gpu_count": 4})
    result = TaskExecutionResult(task_frame_id="f", status="completed", reply_fragment="已提交", next_step_id="submit",
                                 slot_updates={"gpu_count": value})
    enforce_required_slots(result, req, NS(slots_json={"gpu_count": 4}))
    assert result.status == "awaiting_user" and result.next_step_id is None
    assert result.error["code"] == "REQUIRED_SLOT_MISSING" and "已提交" not in result.reply_fragment


@pytest.mark.parametrize("value", [0, False])
def test_false_and_zero_are_valid_slot_values(value):
    req = TaskRequirement(task_frame_id="f", kind="sop", goal="collect", expected_slots=["value"])
    result = TaskExecutionResult(task_frame_id="f", status="completed", slot_updates={"value": value})
    assert enforce_required_slots(result, req, NS(slots_json={})).status == "completed"


def test_clock_uses_client_timezone_and_invalid_zone_falls_back():
    assert "+08:00" in _current_time_text("Asia/Shanghai")
    assert "-10:00" in _current_time_text("Pacific/Honolulu")
    assert _current_time_text("Invalid/Zone")


def test_v3_stops_the_same_resource_failure_without_resetting_on_session_reads():
    events = []
    cap = NS(_invoke_lock=threading.RLock(), fence=NS(check=lambda slot: None), slot=NS(finish=None),
             _emit=lambda e, p: events.append(e), registry=None,
             invoke_proxy=lambda n, a, c: (ModuleResult.fail("INVALID_ARGUMENTS", "missing field") if n == "tool_invoke" else ModuleResult.ok({}), None))
    host = ExecutionHost(cap, NS(kind="sop"))
    for _ in range(2):
        host.invoke_proxy("tool_invoke", {"tool_id": "compute", "arguments": {}}, None)
        host.invoke_proxy("capability_describe", {}, None)
        assert not host.recovery_blocked
    host.invoke_proxy("tool_invoke", {"tool_id": "compute", "arguments": {}}, None)
    assert host.recovery_blocked["code"] == "CAPABILITY_RECOVERY_EXHAUSTED"
    assert len(events) == 3


def test_changed_parameters_can_recover_but_transient_errors_are_not_classified_as_stuck():
    guard = CapabilityRecovery()
    failure = {"success": False, "error": {"code": "INVALID_ARGUMENTS"}}
    assert guard.observe("quota", {"count": "four"}, failure) is None
    assert guard.observe("quota", {"count": 4}, {"success": True}) is None
    for _ in range(5):
        assert guard.observe("quota", {}, {"success": False, "error": {"code": "TIMEOUT"}}) is None
