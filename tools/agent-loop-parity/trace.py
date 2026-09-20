"""Canonical trace normalization and semantic comparison for parity runs."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VOLATILE_KEYS = {
    "timestamp", "startedAt", "completedAt", "createdAt", "updatedAt",
    "created_at", "updated_at",
    "messageId", "requestId", "streamId", "runId", "operationId", "idempotencyKey",
    "connectionGeneration", "moduleInstanceId", "processId", "pid",
}
_NULL_OPTIONAL_KEYS = {"code", "structuredResult"}
_VOLATILE_ID = re.compile(r"^(?:[a-z_-]+-)?(?:[0-9a-f]{8,}|[0-9]{6,})$")
_VOLATILE_MODEL_BLOCK_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:(?:text|thinking):[0-9]+$",
    re.IGNORECASE,
)


def canonicalize(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        derived_image_bytes = value.get("type") == "image" and value.get("source") == "base64"
        timeline_position = (
            value.get("version") == 1
            and isinstance(value.get("turnId"), str)
            and isinstance(value.get("id"), str)
            and isinstance(value.get("order"), int)
            and isinstance(value.get("revision"), int)
        )
        return {
            k: canonicalize(v, key=k)
            for k, v in sorted(value.items())
            if k not in _VOLATILE_KEYS
            and not (timeline_position and k == "turnId")
            and not (k in _NULL_OPTIONAL_KEYS and v is None)
            and not (derived_image_bytes and k == "bytes")
        }
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, str) and key in {"handoff_id", "source_turn_id", "sourceTurnId"} and value:
        return "<generated-id>"
    if isinstance(value, str) and key in {"blockId", "id"} and _VOLATILE_MODEL_BLOCK_ID.match(value):
        return f"<generated-model-block>:{value.rsplit(':', 2)[1]}:{value.rsplit(':', 1)[1]}"
    if isinstance(value, str) and key in {"id", "callId", "toolCallId"} and _VOLATILE_ID.match(value):
        return "<generated-id>"
    return value


def load_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError(f"{path}:{line_no}: trace record must be an object")
        required = {"kind", "scenarioId", "q", "sequence"}
        missing = sorted(required - value.keys())
        if missing:
            raise ValueError(f"{path}:{line_no}: trace record missing {', '.join(missing)}")
        records.append(canonicalize(value))
    return records


@dataclass(frozen=True)
class Difference:
    path: str
    left: Any
    right: Any


# Fields which are observable by a downstream model, tool, user, or StaffDeck
# state machine.  Everything else belongs to a transport/persistence envelope.
_SEMANTIC_EVENT_FIELDS = {
    "model.request": {"modelView", "messages", "systemPrompt", "tools", "metadata", "attempt"},
    "model.response": {"modelView", "message", "content", "tool_calls", "stopReason", "usage", "errors", "structuredResult", "attempt"},
    "model.error": {"code", "message", "retryable", "attempt"},
    "tool.call": {"name", "toolName", "arguments", "toolCallId", "context", "order", "sideEffectCount", "attempt", "concurrencySafe"},
    "tool.start": {"name", "toolName", "toolCallId", "order", "attempt", "concurrencySafe"},
    "tool.finish": {"name", "toolName", "toolCallId", "order", "success", "error", "sideEffectCount", "attempt", "concurrencySafe"},
    "tool.result": {"result", "data", "error", "toolName", "toolCallId", "success", "sideEffectCount", "attempt", "concurrencySafe"},
    "permission.request": {"toolName", "toolCallId", "mode", "canPrompt"},
    "permission.answer": {"toolName", "toolCallId", "allowed", "code"},
    "permission.decision": {"toolName", "toolCallId", "allowed", "code", "retryable"},
    "policy.context": {"toolName", "permissionMode", "runMode"},
    "policy.turn": {"permissionMode", "runMode"},
    "sidecar.lifecycle": {"state", "stage", "code", "attempt"},
    "fault.injected": {"target", "action", "stage", "attempt"},
    "side_effect.state": {"counts", "sideEffectCount"},
    "compact.boundary": {"compactionId", "reason", "messages", "metadata"},
    "checkpoint": {"status", "seedState", "messages", "activeStepId", "taskFrameId", "slots", "knowledgeBudget", "recoveryPoint", "sideEffectCount"},
    "taskframe": {"taskFrame", "status", "stepId", "nextStepId", "slots", "requiredCapabilities", "knowledgeBudget", "priorTaskResults"},
    "session.state": {"activeSkillId", "activeStepId", "pendingTasks", "awaitingInput", "handoff", "slots", "priorTaskResults"},
    # Team records are a host-owned durable outcome of a TL delegation. They
    # are not transport envelopes: a missing run/task/wake or a different
    # assignee changes the externally observable execution path.
    "team.state": {"runs", "tasks", "wakes"},
    "terminal": {"outcome", "code", "stopReason", "structuredResult", "output", "frameStatus", "runStatus", "taskFrame", "session"},
    "user.output": {"text"},
}

_TOOL_LIFECYCLE_PHASE = {
    "tool.call": 0,
    "tool.start": 1,
    "tool.finish": 2,
    "tool.result": 3,
}
_PARALLEL_TOOL_BATCH_KINDS = {*_TOOL_LIFECYCLE_PHASE, "permission.decision"}


def _semantic_record(record: dict[str, Any]) -> dict[str, Any]:
    kind = str(record.get("kind") or "")
    fields = _SEMANTIC_EVENT_FIELDS.get(kind)
    if fields is None:
        # Unknown event kinds are still meaningful if they carry explicit state
        # projection fields; otherwise they are envelope-only.
        fields = set().union(*_SEMANTIC_EVENT_FIELDS.values())
    projected: dict[str, Any] = {"kind": kind}
    for key in fields:
        if key in record:
            projected[key] = record[key]
    # The logical ordering of calls/results is semantic, while the JSONL
    # sequence and transport source are not.
    for key in ("logicalSequence", "phase"):
        if key in record:
            projected[key] = record[key]
    return canonicalize(projected)


def _tool_lifecycle_identity(record: dict[str, Any]) -> str | None:
    for key in ("toolCallId", "callId"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    result = record.get("result")
    if isinstance(result, dict):
        for key in ("toolCallId", "callId"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("name", "toolName"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return f"name:{value}"
    if isinstance(result, dict) and isinstance(result.get("toolName"), str):
        return f"name:{result['toolName']}"
    return None


def _canonicalize_parallel_tool_lifecycle(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize only proven-safe concurrent callback completion interleavings.

    The model receives tool results in call order. Host callback completion is
    otherwise wall-clock dependent, so it is not semantic when every call in
    the batch explicitly declares itself concurrency-safe and has a unique
    identity. All other lifecycle traces retain their original order.
    """
    normalized = list(records)
    index = 0
    while index < len(normalized):
        if normalized[index].get("kind") not in _PARALLEL_TOOL_BATCH_KINDS:
            index += 1
            continue
        end = index
        while end < len(normalized) and normalized[end].get("kind") in _PARALLEL_TOOL_BATCH_KINDS:
            end += 1
        group = normalized[index:end]
        call_records = [record for record in group if record.get("kind") == "tool.call"]
        call_ids = [_tool_lifecycle_identity(record) for record in call_records]
        call_by_name = {
            str(record.get("name") or record.get("toolName") or ""): call_id
            for record, call_id in zip(call_records, call_ids)
        }
        permission_indices = {
            str(record.get("toolName") or ""): offset
            for offset, record in enumerate(group)
            if record.get("kind") == "permission.decision"
        }
        call_indices = {
            call_id: offset
            for offset, (record, call_id) in enumerate(zip(group, [_tool_lifecycle_identity(item) for item in group]))
            if record.get("kind") == "tool.call" and call_id is not None
        }
        def identity(record: dict[str, Any]) -> str | None:
            if record.get("kind") == "permission.decision":
                return call_by_name.get(str(record.get("toolName") or ""))
            return _tool_lifecycle_identity(record)

        identities = [identity(record) for record in group]
        if (
            len(call_ids) > 1
            and None not in call_ids
            and len(set(call_ids)) == len(call_ids)
            and len(call_by_name) == len(call_ids)
            and (
                not permission_indices
                or (
                    len(permission_indices) == len(call_ids)
                    and set(permission_indices) == set(call_by_name)
                    and all(
                        permission_indices[name] < call_indices[call_id]
                        for name, call_id in call_by_name.items()
                    )
                )
            )
            and all(
                record.get("concurrencySafe") is True
                for record in group
                if record.get("kind") in _TOOL_LIFECYCLE_PHASE
            )
            and all(identity in call_ids for identity in identities)
        ):
            order = {identity: offset for offset, identity in enumerate(call_ids)}
            normalized[index:end] = [
                record
                for _, record in sorted(
                    enumerate(group),
                    key=lambda item: (
                        order[identity(item[1])],
                        -1 if item[1].get("kind") == "permission.decision" else _TOOL_LIFECYCLE_PHASE[item[1]["kind"]],
                        item[0],
                    ),
                )
            ]
        index = end
    return normalized


def project_semantic_trace(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _canonicalize_parallel_tool_lifecycle([_semantic_record(record) for record in records])


def project_format_trace(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return canonical envelopes for warning-only comparison."""
    return canonicalize(records)


@dataclass(frozen=True)
class Comparison:
    semantic: list[Difference]
    format_warnings: list[Difference]


def compare_traces(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[Difference]:
    return compare_trace_details(left, right).semantic


def _diff_values(left: Any, right: Any) -> list[Difference]:
    differences: list[Difference] = []

    def visit(a: Any, b: Any, path: str) -> None:
        if type(a) is not type(b):
            differences.append(Difference(path, a, b))
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    differences.append(Difference(f"{path}.{key}", a.get(key), b.get(key)))
                else:
                    visit(a[key], b[key], f"{path}.{key}")
            return
        if isinstance(a, list):
            if len(a) != len(b):
                differences.append(Difference(f"{path}.length", len(a), len(b)))
            for index, (item_a, item_b) in enumerate(zip(a, b)):
                visit(item_a, item_b, f"{path}[{index}]")
            return
        if a != b:
            differences.append(Difference(path, a, b))

    visit(left, right, "trace")
    return differences


def compare_trace_details(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> Comparison:
    semantic = _diff_values(project_semantic_trace(left), project_semantic_trace(right))
    format_differences = _diff_values(project_format_trace(left), project_format_trace(right))
    return Comparison(semantic=semantic, format_warnings=format_differences)


def _merged_expectation(scenario: dict[str, Any], pair: str) -> dict[str, Any]:
    expected = dict(scenario.get("expected") or {})
    pair_overrides = scenario.get("expectedByPair") or {}
    if isinstance(pair_overrides, dict) and isinstance(pair_overrides.get(pair), dict):
        expected.update(pair_overrides[pair])
    return expected


def _merged_adapter_expectation(scenario: dict[str, Any], pair: str, adapter: str | None) -> dict[str, Any]:
    expected = _merged_expectation(scenario, pair)
    overrides = scenario.get("expectedByAdapter") or {}
    if adapter and isinstance(overrides, dict) and isinstance(overrides.get(adapter), dict):
        expected.update(overrides[adapter])
    return expected


def _last(records: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((record for record in reversed(records) if record.get("kind") == kind), None)


def _first_defined(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _record_value(records: list[dict[str, Any]], key: str) -> Any:
    terminal = _last(records, "terminal") or {}
    taskframe = _last(records, "taskframe") or {}
    session = _last(records, "session.state") or {}
    checkpoint = _last(records, "checkpoint") or {}
    team_state = _last(records, "team.state") or {}
    team_tasks = team_state.get("tasks") if isinstance(team_state.get("tasks"), list) else []
    terminal_task_frame = terminal.get("taskFrame")
    if not isinstance(terminal_task_frame, dict):
        terminal_task_frame = {}
    task_frame_state = taskframe.get("taskFrame")
    if not isinstance(task_frame_state, dict):
        task_frame_state = {}
    session_state = terminal.get("session")
    if not isinstance(session_state, dict):
        session_state = {}
    mapping = {
        "terminalOutcome": terminal.get("outcome"),
        "errorCode": terminal.get("code"),
        "stopReason": terminal.get("stopReason"),
        "frameStatus": terminal.get("frameStatus") or taskframe.get("status") or task_frame_state.get("status") or terminal_task_frame.get("status"),
        "runStatus": terminal.get("runStatus"),
        "taskFrameStatus": taskframe.get("status") or task_frame_state.get("status"),
        "activeStepId": session.get("activeStepId") or session_state.get("activeStepId") or checkpoint.get("activeStepId"),
        "nextStepId": taskframe.get("nextStepId") or task_frame_state.get("nextStepId") or terminal_task_frame.get("nextStepId"),
        "awaitingInput": session.get("awaitingInput") or session_state.get("awaitingInput"),
        "handoff": session.get("handoff") or session_state.get("handoff"),
        "slots": _first_defined(
            session.get("slots"),
            taskframe.get("slots"),
            session_state.get("slots"),
            task_frame_state.get("slots"),
            terminal_task_frame.get("slots"),
            checkpoint.get("slots"),
        ),
        "knowledgeBudget": taskframe.get("knowledgeBudget") or task_frame_state.get("knowledgeBudget") or terminal_task_frame.get("knowledgeBudget") or checkpoint.get("knowledgeBudget"),
        "requiredCapabilities": taskframe.get("requiredCapabilities") or task_frame_state.get("requiredCapabilities") or terminal_task_frame.get("requiredCapabilities"),
        "priorTaskResults": taskframe.get("priorTaskResults") or session.get("priorTaskResults") or task_frame_state.get("priorTaskResults") or terminal_task_frame.get("priorTaskResults") or session_state.get("priorTaskResults"),
        "executionTarget": taskframe.get("executionTarget") or session.get("executionTarget") or task_frame_state.get("executionTarget") or terminal_task_frame.get("executionTarget") or session_state.get("executionTarget") or ("team_member" if team_tasks else None),
        "forcedSopVersion": terminal.get("forcedSopVersion") or taskframe.get("forcedSopVersion") or session.get("forcedSopVersion") or task_frame_state.get("forcedSopVersion") or terminal_task_frame.get("forcedSopVersion") or session_state.get("forcedSopVersion"),
        "output": terminal.get("output"),
        "modelAttempts": sum(record.get("kind") == "model.request" for record in records),
        "pendingTasks": session.get("pendingTasks") or session_state.get("pendingTasks"),
    }
    if key == "policyModes":
        return [
            record.get("permissionMode")
            for record in records
            if record.get("kind") == "policy.turn" and isinstance(record.get("permissionMode"), str)
        ]
    if key == "toolCalls":
        calls = [
            record.get("name") or record.get("toolName")
            for record in records
            if record.get("kind") == "tool.call"
        ]
        if calls:
            return calls
        # Gateway adapters may observe a built-in tool call only in the
        # canonical model response, before the tool lifecycle event arrives.
        # Preserve that semantic call list instead of treating it as no call.
        for record in records:
            if record.get("kind") != "model.response":
                continue
            message = record.get("modelView") or record.get("message") or {}
            tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
            if isinstance(tool_calls, list):
                return [
                    (item.get("function") or {}).get("name")
                    for item in tool_calls
                    if isinstance(item, dict) and isinstance(item.get("function"), dict)
                ]
        return []
    if key == "toolCallCount":
        calls = [record for record in records if record.get("kind") == "tool.call"]
        if calls:
            return len(calls)
        return sum(
            len((record.get("modelView") or {}).get("tool_calls") or [])
            for record in records
            if record.get("kind") == "model.response" and isinstance(record.get("modelView") or {}, dict)
        )
    if key == "sideEffectCount":
        state = _last(records, "side_effect.state") or {}
        if isinstance(state.get("sideEffectCount"), int):
            return state["sideEffectCount"]
        counts = [
            value
            for record in records
            for value in [record.get("sideEffectCount"), (record.get("result") or {}).get("sideEffectCount") if isinstance(record.get("result"), dict) else None]
            if isinstance(value, int)
        ]
        return max(counts, default=0)
    if key == "permissionAllowed":
        decisions = [
            record.get("allowed")
            for record in records
            if record.get("kind") in {"permission.answer", "permission.decision"}
            and isinstance(record.get("allowed"), bool)
        ]
        if not decisions:
            for record in records:
                if record.get("kind") != "tool.finish" or record.get("success") is not False:
                    continue
                error = record.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if code in {"permission_denied", "permission_required", "PERMISSION_DENIED"}:
                    decisions.append(False)
        return all(decisions) if decisions else None
    if key in {"toolErrorCode", "toolRetryable"}:
        errors = []
        for record in records:
            if record.get("kind") not in {"tool.finish", "tool.result"}:
                continue
            result = record.get("result") if isinstance(record.get("result"), dict) else record
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            if error:
                errors.append(error)
        if not errors:
            return None
        return errors[-1].get("code" if key == "toolErrorCode" else "retryable")
    if key == "modelVisibleTools":
        request_record = next((record for record in records if record.get("kind") == "model.request"), {})
        model_view = request_record.get("modelView") if isinstance(request_record.get("modelView"), dict) else request_record
        tools = model_view.get("tools") if isinstance(model_view, dict) else None
        names = [tool.get("name") or (tool.get("function") or {}).get("name") for tool in tools or [] if isinstance(tool, dict)]
        return names
    return mapping.get(key)


def validate_trace_expectations(
    records: list[dict[str, Any]],
    scenario: dict[str, Any],
    pair: str,
    adapter: str | None = None,
) -> list[Difference]:
    """Validate a single adapter trace against its scenario oracle."""
    expected = _merged_adapter_expectation(scenario, pair, adapter)
    failures: list[Difference] = []
    for key, wanted in expected.items():
        if key in {"requiresImage", "noToolSideEffects", "modelVisibleToolsExclude"}:
            if key == "requiresImage":
                requests = [record for record in records if record.get("kind") == "model.request"]
                actual = any(
                    isinstance(node, dict)
                    and (
                        node.get("type") == "image_url"
                        or (node.get("type") == "image" and node.get("source") == "base64")
                    )
                for request in requests
                for node in _walk(request.get("modelView") or request.get("request") or request.get("messages"))
                )
            elif key == "noToolSideEffects":
                actual = _record_value(records, "sideEffectCount") == 0
            else:
                visible = _record_value(records, "modelVisibleTools") or []
                excluded = wanted if isinstance(wanted, list) else [wanted]
                actual = all(str(name) not in visible for name in excluded)
                wanted = True
        elif key == "outputContains":
            output = _record_value(records, "output")
            actual = isinstance(output, str) and str(wanted) in output
            wanted = True
        elif key == "awaitingInput" and wanted is True:
            actual = _record_value(records, key) is not None
            wanted = True
        else:
            actual = _record_value(records, key)
        if canonicalize(actual) != canonicalize(wanted):
            failures.append(Difference(f"{_expectation_path(key)}", wanted, actual))
    return failures


def _expectation_path(key: str) -> str:
    return {
        "terminalOutcome": "terminal.outcome",
        "errorCode": "terminal.code",
        "stopReason": "terminal.stopReason",
    }.get(key, key)


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def write_report(
    path: Path,
    pair_name: str,
    left_path: Path,
    right_path: Path,
    differences: list[Difference] | Comparison,
) -> None:
    comparison = differences if isinstance(differences, Comparison) else Comparison(differences, [])
    semantic = comparison.semantic
    warnings = comparison.format_warnings
    lines = [f"# {pair_name}", "", f"- left: `{left_path}`", f"- right: `{right_path}`", ""]
    if not semantic:
        lines.append("PASS: no semantic differences after projection.")
    else:
        lines.append(f"FAIL: {len(semantic)} semantic difference(s).")
        lines.append("\n## Semantic Differences\n")
        for diff in semantic[:50]:
            lines.extend(["", f"- `{diff.path}`", f"  - left: `{json.dumps(diff.left, ensure_ascii=False, sort_keys=True)}`", f"  - right: `{json.dumps(diff.right, ensure_ascii=False, sort_keys=True)}`"])
    if warnings:
        lines.append("\n## Format Warnings\n")
        lines.append(f"{len(warnings)} envelope/serialization difference(s); these do not affect exit status.")
        for diff in warnings[:50]:
            lines.extend(["", f"- `{diff.path}`", f"  - left: `{json.dumps(diff.left, ensure_ascii=False, sort_keys=True)}`", f"  - right: `{json.dumps(diff.right, ensure_ascii=False, sort_keys=True)}`"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
