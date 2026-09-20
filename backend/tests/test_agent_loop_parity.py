from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACE_PATH = ROOT / "tools/agent-loop-parity/trace.py"
_SPEC = importlib.util.spec_from_file_location("agent_loop_parity_trace", TRACE_PATH)
assert _SPEC and _SPEC.loader
_TRACE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _TRACE
_SPEC.loader.exec_module(_TRACE)
canonicalize = _TRACE.canonicalize
compare_traces = _TRACE.compare_traces
compare_trace_details = _TRACE.compare_trace_details
load_trace = _TRACE.load_trace
validate_trace_expectations = _TRACE.validate_trace_expectations

MOCK_PATH = ROOT / "tools/agent-loop-parity/mock_backend.py"
RUN_PATH = ROOT / "tools/agent-loop-parity/run.py"
PILOTDECK_SIDECAR_ADAPTER_PATH = (
    ROOT / "tools/agent-loop-parity/adapters/pilotdeck_sidecar_impl.py"
)
STAFFDECK_ENGINE_PATH = (
    ROOT / "tools/agent-loop-parity/adapters/staffdeck_engine_impl.py"
)
PILOTDECK_GATEWAY_ADAPTER_PATH = (
    ROOT / "tools/agent-loop-parity/adapters/pilotdeck_gateway_impl.mjs"
)


def _load_runner():
    tools_root = str(RUN_PATH.parent)
    sys.path.insert(0, tools_root)
    try:
        spec = importlib.util.spec_from_file_location("agent_loop_parity_run", RUN_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(tools_root)


def _load_pilotdeck_sidecar_adapter():
    spec = importlib.util.spec_from_file_location(
        "agent_loop_parity_pilotdeck_sidecar",
        PILOTDECK_SIDECAR_ADAPTER_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scenario_matrix_is_deterministic_and_complete() -> None:
    document = json.loads((ROOT / "tools/agent-loop-parity/scenarios.json").read_text())
    scenarios = document["scenarios"]
    ids = {item["scenarioId"] for item in scenarios}
    assert len(ids) >= 30
    assert {"pure_text", "single_tool", "multiple_tool", "tool_error", "permission_denial", "max_turns", "deadline", "cancel", "image", "checkpoint_resume"} <= ids
    assert all(item["q"] for item in scenarios)
    max_turns = next(item for item in scenarios if item["scenarioId"] == "max_turns")
    assert max_turns["expected"]["errorCode"] == "agent_max_turns_reached"
    categories = {item.get("category") for item in scenarios}
    assert {"sop", "handoff", "team", "scheduled"} <= categories
    assert all(item.get("suite") in {
        "core-regression",
        "core-resilience",
        "staffdeck-workflow",
        "known-gap",
    } for item in scenarios)
    assert all(set(item.get("pairs") or []) <= {"pilotdeck", "staffdeck"} for item in scenarios)


def test_scenario_loader_filters_suite_and_pair() -> None:
    runner = _load_runner()
    path = ROOT / "tools/agent-loop-parity/scenarios.json"

    pilotdeck_core = runner._load_scenarios(
        path,
        "all",
        suite="core-regression",
        pair="pilotdeck",
    )
    assert pilotdeck_core
    assert all(item["suite"] == "core-regression" for item in pilotdeck_core)
    assert all("pilotdeck" in item["pairs"] for item in pilotdeck_core)

    staffdeck_workflow = runner._load_scenarios(
        path,
        "all",
        suite="staffdeck-workflow",
        pair="staffdeck",
    )
    assert staffdeck_workflow
    assert all(item["suite"] == "staffdeck-workflow" for item in staffdeck_workflow)
    assert all(item["pairs"] == ["staffdeck"] for item in staffdeck_workflow)


def test_oracle_rejects_identically_wrong_traces() -> None:
    scenario = {
        "scenarioId": "x",
        "q": "q",
        "expected": {
            "terminalOutcome": "failed",
            "errorCode": "EXPECTED_ERROR",
            "toolCalls": ["lookup"],
        },
    }
    trace = [
        {"kind": "tool.call", "scenarioId": "x", "q": "q", "sequence": 0, "name": "lookup"},
        {"kind": "terminal", "scenarioId": "x", "q": "q", "sequence": 1, "outcome": "completed"},
    ]

    failures = validate_trace_expectations(trace, scenario, "pilotdeck")

    assert any(item.path == "terminal.outcome" for item in failures)
    assert any(item.path == "terminal.code" for item in failures)


def test_oracle_uses_pair_specific_expectation_override() -> None:
    scenario = {
        "scenarioId": "budget",
        "q": "q",
        "expected": {"terminalOutcome": "failed"},
        "expectedByPair": {
            "staffdeck": {"errorCode": "ACTION_BUDGET_EXHAUSTED", "frameStatus": "queued"},
        },
    }
    trace = [{
        "kind": "terminal",
        "scenarioId": "budget",
        "q": "q",
        "sequence": 0,
        "outcome": "failed",
        "code": "ACTION_BUDGET_EXHAUSTED",
        "frameStatus": "queued",
    }]

    assert validate_trace_expectations(trace, scenario, "staffdeck") == []


def test_oracle_reads_task_frame_fields_from_terminal_snapshot() -> None:
    scenario = {
        "scenarioId": "required-capability",
        "q": "q",
        "expected": {"requiredCapabilities": ["lookup"]},
    }
    trace = [{
        "kind": "terminal",
        "scenarioId": "required-capability",
        "q": "q",
        "sequence": 0,
        "outcome": "completed",
        "taskFrame": {"requiredCapabilities": ["lookup"]},
    }]

    assert validate_trace_expectations(trace, scenario, "staffdeck") == []


def test_oracle_accepts_only_a_present_awaiting_input_payload() -> None:
    scenario = {
        "scenarioId": "awaiting-input",
        "q": "q",
        "expected": {"awaitingInput": True},
    }
    awaiting = [{
        "kind": "terminal",
        "scenarioId": "awaiting-input",
        "q": "q",
        "sequence": 0,
        "session": {"awaitingInput": {"expected_fields": ["date"]}},
    }]
    missing = [{
        "kind": "terminal",
        "scenarioId": "awaiting-input",
        "q": "q",
        "sequence": 0,
        "session": {"awaitingInput": None},
    }]

    assert validate_trace_expectations(awaiting, scenario, "staffdeck") == []
    assert validate_trace_expectations(missing, scenario, "staffdeck")


def test_known_gap_requires_exact_semantic_difference() -> None:
    runner = _load_runner()
    scenario = {
        "scenarioId": "gap",
        "q": "q",
        "suite": "known-gap",
        "expectedDifferencePaths": ["trace[0].output"],
    }
    left = [{"kind": "terminal", "scenarioId": "gap", "q": "q", "sequence": 0, "outcome": "completed", "output": "native"}]
    right = [{"kind": "terminal", "scenarioId": "gap", "q": "q", "sequence": 0, "outcome": "completed", "output": "sidecar"}]
    comparison = compare_trace_details(left, right)

    assert runner._known_gap_matches(scenario, comparison.semantic)
    scenario["expectedDifferencePaths"] = ["trace[0].code"]
    assert not runner._known_gap_matches(scenario, comparison.semantic)


def test_declared_semantic_difference_rejects_any_extra_path() -> None:
    runner = _load_runner()
    scenario = {
        "declaredSemanticDifferencePaths": ["terminal.outcome", "terminal.stopReason"],
    }
    exact = [
        _TRACE.Difference("trace[0].outcome", "failed", "result_unknown"),
        _TRACE.Difference("trace[0].stopReason", "aborted_streaming", None),
    ]

    assert runner._declared_semantic_difference_matches(scenario, exact)
    assert not runner._declared_semantic_difference_matches(
        scenario,
        [*exact, _TRACE.Difference("trace[0].output", "", "late output")],
    )


def test_semantic_projection_ignores_format_only_differences() -> None:
    left = [{"kind": "terminal", "scenarioId": "x", "q": "q", "sequence": 0,
             "outcome": "completed", "output": "ok", "providerEnvelope": {"a": 1}}]
    right = [{"kind": "terminal", "scenarioId": "x", "q": "q", "sequence": 9,
              "outcome": "completed", "output": "ok", "providerEnvelope": {"b": 2}}]
    comparison = compare_trace_details(left, right)
    assert comparison.semantic == []
    assert comparison.format_warnings


def test_semantic_projection_rejects_task_frame_transition_difference() -> None:
    left = [{"kind": "taskframe", "scenarioId": "x", "q": "q", "sequence": 0,
             "taskFrame": {"status": "completed", "stepId": "a"}}]
    right = [{"kind": "taskframe", "scenarioId": "x", "q": "q", "sequence": 0,
              "taskFrame": {"status": "completed", "stepId": "b"}}]
    comparison = compare_trace_details(left, right)
    assert comparison.semantic


def test_semantic_projection_rejects_team_durable_state_difference() -> None:
    left = [{
        "kind": "team.state", "scenarioId": "team", "q": "q", "sequence": 0,
        "runs": [{"status": "running", "teamId": "team", "tlSessionId": "session"}],
        "tasks": [{"status": "pending", "teamId": "team", "assigneeAgentId": "member"}],
        "wakes": [{"status": "pending", "teamId": "team", "targetAgentId": "member"}],
    }]
    right = [{
        "kind": "team.state", "scenarioId": "team", "q": "q", "sequence": 0,
        "runs": [{"status": "running", "teamId": "team", "tlSessionId": "session"}],
        "tasks": [{"status": "pending", "teamId": "team", "assigneeAgentId": "other-member"}],
        "wakes": [{"status": "pending", "teamId": "team", "targetAgentId": "member"}],
    }]

    comparison = compare_trace_details(left, right)

    assert comparison.semantic
    assert comparison.semantic[0].path == "trace[0].tasks[0].assigneeAgentId"


def test_semantic_projection_rejects_team_member_execution_difference() -> None:
    left = [{
        "kind": "team.member_execution", "scenarioId": "team", "q": "q", "sequence": 0,
        "sessions": [{"status": "active", "agentId": "member"}],
        "reports": [{"status": "done", "summary": "investigated", "needsInput": False}],
    }]
    right = [{
        "kind": "team.member_execution", "scenarioId": "team", "q": "q", "sequence": 0,
        "sessions": [{"status": "active", "agentId": "member"}],
        "reports": [{"status": "escalated", "summary": None, "needsInput": False}],
    }]

    comparison = compare_trace_details(left, right)

    assert comparison.semantic
    assert comparison.semantic[0].path == "trace[0].reports[0].status"


def test_canonicalization_removes_transport_noise_but_preserves_semantics() -> None:
    value = {
        "messageId": "message-123456789",
        "timestamp": "2026-09-03T00:00:00Z",
        "toolCall": {"id": "call-123456789", "name": "lookup", "arguments": {"q": "same"}},
        "content": "same",
    }
    assert canonicalize(value) == {"content": "same", "toolCall": {"arguments": {"q": "same"}, "name": "lookup", "id": "<generated-id>"}}


def test_canonicalization_ignores_only_derived_image_bytes() -> None:
    image = {
        "type": "image",
        "source": "base64",
        "mimeType": "image/png",
        "data": "AA==",
        "bytes": 1,
    }
    assert canonicalize(image) == {
        "type": "image",
        "source": "base64",
        "mimeType": "image/png",
        "data": "AA==",
    }
    assert canonicalize({"type": "artifact", "bytes": 1}) == {
        "type": "artifact",
        "bytes": 1,
    }


def test_comparator_keeps_event_order_and_reports_first_difference() -> None:
    left = [{"kind": "tool.call", "sequence": 0, "name": "lookup"}, {"kind": "tool.result", "sequence": 1, "value": "ok"}]
    right = [{"kind": "tool.call", "sequence": 0, "name": "summarize"}, {"kind": "tool.result", "sequence": 1, "value": "ok"}]
    differences = compare_traces(left, right)
    assert differences[0].path == "trace[0].name"
    assert differences[0].left == "lookup"
    assert differences[0].right == "summarize"


def test_trace_loader_rejects_missing_contract_fields(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"kind": "terminal", "scenarioId": "pure_text", "q": "q"}) + "\n")
    try:
        load_trace(path)
    except ValueError as exc:
        assert "sequence" in str(exc)
    else:
        raise AssertionError("missing sequence must be rejected")


def test_mock_provider_is_repeatable_for_same_query() -> None:
    import subprocess

    process = subprocess.Popen(
        [sys.executable, str(MOCK_PATH), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    connection = HTTPConnection("127.0.0.1", int(ready["port"]))
    request = {"scenarioId": "pure_text", "q": "same q", "messages": [{"role": "user", "content": "same q"}]}
    bodies = []
    try:
        for _ in range(2):
            connection.request("POST", "/v1/chat/completions", body=json.dumps(request), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            bodies.append(json.loads(response.read()))
    finally:
        connection.close()
        process.terminate()
        process.wait(timeout=2)
    assert bodies[0] == bodies[1]
    assert bodies[0]["choices"][0]["message"]["content"] == "MOCK_ANSWER[pure_text]::same q"


def test_mock_provider_recognizes_canonical_image_blocks() -> None:
    process = subprocess.Popen(
        [sys.executable, str(MOCK_PATH), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    connection = HTTPConnection("127.0.0.1", int(ready["port"]))
    request = {
        "scenarioId": "image",
        "q": "describe",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": "base64",
                        "mediaType": "image/png",
                        "data": "AA==",
                    }
                ],
            }
        ],
    }
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(request),
            headers={"Content-Type": "application/json"},
        )
        content = json.loads(connection.getresponse().read())["choices"][0]["message"][
            "content"
        ]
    finally:
        connection.close()
        process.terminate()
        process.wait(timeout=2)
    assert content == "MOCK_ANSWER[image]::describe"


def test_mock_tool_returns_same_content_and_explicit_errors() -> None:
    import subprocess

    process = subprocess.Popen(
        [sys.executable, str(MOCK_PATH), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    try:
        connection = HTTPConnection("127.0.0.1", int(ready["port"]))
        request = {"scenarioId": "single_tool", "q": "same q", "name": "lookup", "arguments": {"q": "same q"}}
        results = []
        for _ in range(2):
            connection.request("POST", "/tools/execute", body=json.dumps(request), headers={"Content-Type": "application/json"})
            results.append(json.loads(connection.getresponse().read()))
        assert results[0] == results[1]
        assert results[0]["data"]["q"] == "same q"
        connection.request("POST", "/tools/execute", body=json.dumps({"name": "lookup_error", "arguments": {}}), headers={"Content-Type": "application/json"})
        error = json.loads(connection.getresponse().read())
        assert error["error"]["code"] == "MOCK_TOOL_ERROR"
    finally:
        connection.close()
        process.terminate()
        process.wait(timeout=2)


def test_mock_tool_deadline_prevents_side_effect_commit() -> None:
    import subprocess
    import time

    process = subprocess.Popen(
        [sys.executable, str(MOCK_PATH), "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    run_key = "expired-tool-deadline"
    connection = HTTPConnection("127.0.0.1", int(ready["port"]))
    try:
        connection.request(
            "POST",
            "/tools/execute",
            body=json.dumps(
                {
                    "scenarioId": "deadline_during_tool",
                    "name": "slow_side_effect",
                    "arguments": {"q": "deadline"},
                    "runKey": run_key,
                    "operationDeadlineEpochMs": int(time.time() * 1000) - 1,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        result = json.loads(connection.getresponse().read())
        assert result["type"] == "error"
        assert result["error"]["code"] == "CANCELLED"

        connection.request(
            "POST",
            "/control/state",
            body=json.dumps({"runKey": run_key}),
            headers={"Content-Type": "application/json"},
        )
        state = json.loads(connection.getresponse().read())
        assert state["sideEffects"] == {}
    finally:
        connection.close()
        process.terminate()
        process.wait(timeout=2)


def test_pilotdeck_sidecar_adapter_builds_complete_ordered_messages() -> None:
    adapter = _load_pilotdeck_sidecar_adapter()

    assert adapter.scenario_messages({"q": "current"}) == [
        {"role": "user", "content": "current"}
    ]
    assert adapter.scenario_messages(
        {
            "q": "current",
            "messages": [{"role": "assistant", "content": "history"}],
        }
    ) == [
        {"role": "assistant", "content": "history"},
        {"role": "user", "content": "current"},
    ]
    image_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "current"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AA=="},
            },
        ],
    }
    assert adapter.scenario_messages(
        {"q": "current", "messages": [image_message]}
    ) == [image_message]


def test_parity_runner_can_select_only_pilotdeck_pair() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUN_PATH), "--help"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--pair {all,pilotdeck,staffdeck}" in completed.stdout
    assert "--pilotdeck-surface {loop,gateway}" in completed.stdout
    assert "--adapter-timeout-seconds" in completed.stdout
    assert "--suite" in completed.stdout
    assert "--comparison {same-version,baseline,both}" in completed.stdout


def test_pilotdeck_gateway_adapter_uses_real_host_and_preserves_terminal_semantics() -> None:
    adapter = PILOTDECK_GATEWAY_ADAPTER_PATH.read_text()

    assert '"dist/src/cli/createLocalGateway.js"' in adapter
    assert '"dist/src/cli/pilotdeckServer.js"' in adapter
    assert '"dist/src/gateway/client/GatewayWsClient.js"' in adapter
    assert 'clientName: "test-control"' in adapter
    assert "cancelAnchor" in adapter
    assert "context.abortSignal?.addEventListener(\"abort\", forwardCancellation" in adapter
    assert "if (!context.abortSignal?.aborted) throw error" in adapter
    assert "productionSidecarEvidence.handshakeCompleted" in adapter
    assert 'writeFile(`${traceOut}.proof.json`' in adapter
    assert 'process.env.PARITY_SERVE_ONLY === "1"' in adapter
    assert 'PILOTDECK_AGENT_LOOP_TRANSPORT: mode === "sidecar" ? "stdio" : "native"' in adapter
    assert "agentLoopTransportObserver" in adapter


def test_staffdeck_adapter_environment_prefers_selected_checkout(tmp_path: Path) -> None:
    runner = _load_runner()
    source_root = tmp_path / "staffdeck-baseline"
    package_root = source_root / "backend" / "app"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("SOURCE = 'baseline'\n")
    env = runner._adapter_environment(
        scenario={"scenarioId": "pure_text", "q": "q"},
        mode="legacy",
        source_root=source_root,
        source_ref="origin/main",
        mock_url="http://127.0.0.1:1",
        output=tmp_path / "trace.jsonl",
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import app; print(app.__file__)"],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": env["PYTHONPATH"] + os.pathsep + str(ROOT / "backend")},
        text=True,
        capture_output=True,
        check=True,
    )
    assert Path(completed.stdout.strip()).resolve().is_relative_to(
        (source_root / "backend").resolve()
    )


def test_staffdeck_wrappers_share_complete_engine_adapter() -> None:
    adapters = ROOT / "tools/agent-loop-parity/adapters"
    legacy = (adapters / "staffdeck_legacy_impl.py").read_text()
    pilotdeck = (adapters / "staffdeck_pilotdeck_impl.py").read_text()
    engine = STAFFDECK_ENGINE_PATH.read_text()

    assert "from staffdeck_engine_impl import main" in legacy
    assert 'main("legacy")' in legacy
    assert "from staffdeck_engine_impl import main" in pilotdeck
    assert 'main("pilotdeck")' in pilotdeck
    assert "loop = AgentLoop(db)" in engine
    assert "response = loop.handle_turn(request)" in engine
    assert "PilotDeckAgentLoopClient" not in engine
    assert "HarnessTaskAgent" not in engine


def test_staffdeck_engine_uses_product_assembly_for_parity_inputs() -> None:
    engine = STAFFDECK_ENGINE_PATH.read_text()

    assert 'os.environ["PILOTDECK_AGENT_LOOP_ENABLED"]' in engine
    assert '"true" if MODE == "pilotdeck" else "false"' in engine
    assert "ChatTurnRequest(" in engine
    assert "attachments=_attachments(scenario)" in engine
    assert "agent_loop_max_actions=int(limits.get(\"remainingActions\") or 6)" in engine
    assert "cancel_chat_turn(session_id, client_turn_id)" in engine
    assert "store.save_agent_loop_checkpoint(" in engine
    assert 'recorder.add("checkpoint"' in engine
    assert 'recorder.add(\n            "terminal",' in engine
    assert 'recorder.add("user.output"' in engine
    assert '"scenarioId": scenario["scenarioId"]' in engine
    assert 'scenario["scenarioId"] = "pure_text"' not in engine
