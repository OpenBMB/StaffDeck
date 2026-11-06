import re

import pytest

from app.core.task_request_compiler import TaskRequirement
from staffdeck_harness.bridge.task_agent import _step_prompt
from staffdeck_harness.bridge.tool_guide import system_tool_guide
from staffdeck_harness.capabilities.host import PROXY_TOOLS
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.interactions.pipeline_host import PipelineState
from tests_harness.test_capability_host_hardening import db as db, _ctx, _host, _staff


def test_initial_prompt_teaches_actual_system_tools_before_user_input():
    names = set(PROXY_TOOLS)
    guide = system_tool_guide("conversation", names)
    for name in names:
        assert f"mcp__staffdeck__{name}" in guide
    assert "mcp__staffdeck__capability_search" not in guide
    assert "capabilities" not in guide.split("## 可调用接口")[1].split("## 报错后的处理")[0]
    assert '"tool_id"' in guide and '"resource_id"' in guide
    assert "不会扩大授权" in guide and "不重新提交" in guide
    prompt = _step_prompt(TaskRequirement(task_frame_id="t", kind="conversation", goal="test",
        current_user_message="USER-MARKER"), PipelineState(CompositionCompiler(hooks=()).compile(_staff())), [], "", system_tools=names)
    assert prompt.index("# 平台系统工具") < prompt.index("USER-MARKER")
    assert "mcp__staffdeck__submit_step_result" not in guide


def test_guide_does_not_advertise_unavailable_tool_proxies_and_sop_control_is_explicit():
    guide = system_tool_guide("sop", {"capability_describe", "capability_invoke", "submit_step_result"})
    advertised = set(re.findall(r"^- mcp__staffdeck__(\w+)：", guide, re.M))
    assert advertised == {"capability_describe", "capability_invoke"}
    assert "mcp__staffdeck__submit_step_result" in guide


@pytest.mark.parametrize("name,args,code,action", [
    ("not_a_proxy", {}, "TOOL_NOT_AVAILABLE", "check_system_tool_guide"),
    ("capability_invoke", {"operation": "not.registered/v1", "resource_id": "x", "arguments": {}}, "UNSUPPORTED_CAPABILITY", "capability_describe"),
    ("tool_invoke", {"tool_id": "x", "arguments": "PRIVATE-MARKER"}, "INVALID_ARGUMENTS", "correct_arguments"),
    ("capability_describe", {"kind": "PRIVATE-MARKER"}, "INVALID_ARGUMENTS", "correct_arguments"),
])
def test_proxy_errors_are_actionable_and_do_not_echo_invalid_values(db, name, args, code, action):
    host = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    result, receipt = host.invoke_proxy(name, args, _ctx())
    assert result.error["code"] == code
    assert result.error["next_action"] == action
    assert result.error["executed"] is False and receipt is None
    assert "PRIVATE-MARKER" not in str(result.error)


def test_unbound_resource_is_different_from_invalid_proxy_arguments(db):
    host = _host(db, CompositionCompiler(hooks=()).compile(_staff()))
    result, receipt = host.invoke_proxy("tool_invoke", {"tool_id": "not-bound", "arguments": {}}, _ctx())
    assert result.error["code"] == "ACTIVATION_FENCED"
    assert result.error["details"]["reason"] == "RESOURCE_NOT_BOUND"
    assert result.error["next_action"] == "check_binding" and receipt is None
