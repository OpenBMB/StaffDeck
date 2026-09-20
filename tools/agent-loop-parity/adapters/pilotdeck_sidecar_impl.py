"""Drive a built PilotDeck sidecar against the shared deterministic mock."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def write_trace(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")


def scenario_messages(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    messages = [
        dict(message)
        for message in scenario.get("messages") or []
        if isinstance(message, dict)
    ]
    if messages and messages[-1].get("role") == "user":
        return messages
    return [*messages, {"role": "user", "content": str(scenario["q"])}]


def model_fault_at(scenario: dict[str, Any], attempt: int) -> dict[str, Any] | None:
    faults = scenario.get("faults") if isinstance(scenario.get("faults"), dict) else {}
    for fault in faults.get("model") or []:
        if isinstance(fault, dict) and int(fault.get("at") or 1) == attempt:
            return fault
    return None


def model_fault_error(action: str) -> tuple[str, str, bool] | None:
    if action == "retryable_error":
        return ("provider_unavailable", "Deterministic temporary provider failure.", True)
    if action == "non_retryable_error":
        return ("invalid_model_response", "Deterministic permanent provider failure.", False)
    if action == "malformed_response":
        return ("invalid_model_response", "Deterministic malformed provider response.", False)
    if action == "stream_interruption":
        return ("provider_stream_interrupted", "Deterministic provider stream interruption.", False)
    return None


def main() -> int:
    scenario = json.loads(os.environ["PARITY_SCENARIO_JSON"])
    source = Path(os.environ["PARITY_SOURCE_ROOT"])
    mock = os.environ["PARITY_MOCK_BASE_URL"]
    output = Path(os.environ["PARITY_TRACE_OUT"])
    command = ["node", str(source / "dist/src/cli/pilotdeck-agent-loop-sidecar.js")]
    process = subprocess.Popen(command, cwd=source, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdin and process.stdout
    records: list[dict[str, Any]] = []
    records_lock = threading.Lock()
    stdin_lock = threading.Lock()
    worker_lock = threading.Lock()
    workers: list[threading.Thread] = []
    sequence = 0
    model_attempt = 0
    scenario_id = str(scenario["scenarioId"])
    q = str(scenario["q"])
    run_key = os.environ.get("PARITY_RUN_KEY") or f"{scenario_id}:pilotdeck-sidecar"
    messages = scenario_messages(scenario)
    permission = scenario.get("permission") if isinstance(scenario.get("permission"), dict) else {}
    scenario_has_permission_policy = any(
        isinstance(permission.get(behavior), list) and permission[behavior]
        for behavior in ("allow", "deny", "ask")
    )
    concurrency_safe_tools = {"lookup", "summarize"}
    tools = [{"name": name, "description": name, "kind": "custom", "inputSchema": {"type": "object"}, "readOnly": not scenario_has_permission_policy and name not in {"restricted", "loop", "read_file"}, "concurrencySafe": name in concurrency_safe_tools} for name in scenario.get("tools", [])]
    payload: dict[str, Any] = {
        "agent": {"provider": "parity", "model": "deterministic", "cwd": str(source), "systemPrompt": "Return the deterministic answer.", "runMode": "normal"},
        "messages": messages, "tools": tools,
        "hostModules": {"capability": {"methods": ["execute"]}, "permission": {"methods": ["decide"]}},
        # The host permission port owns resolved scenario policy. Giving these rules
        # to PermissionRuntime would bypass that port before the adapter answers.
        "permissionContext": {"mode": permission.get("mode", "default"), "canPrompt": bool(permission.get("canPrompt")), "bypassAvailable": False, "rules": {"allow": [], "deny": [], "ask": []}},
        "seedState": scenario.get("seedState"), "executionContext": {"scenarioId": scenario_id},
    }
    limits = scenario.get("limits") if isinstance(scenario.get("limits"), dict) else {}
    if limits.get("maxTurns"):
        payload["agent"]["maxTurns"] = limits["maxTurns"]
    now = dt.datetime.now(dt.UTC)
    deadline_ms = int(limits.get("deadlineMs") or 0)
    request = {"kind": "request", "messageId": "execute-1", "method": "execute", "runId": "run-parity", "operationId": f"op-{scenario_id}", "requestId": "request-1", "sessionId": "session-parity", "turnId": "turn-parity", "idempotencyKey": "parity-effect", "payload": payload}
    if deadline_ms:
        request["operationDeadline"] = (now + dt.timedelta(milliseconds=deadline_ms)).isoformat().replace("+00:00", "Z")
    if payload.get("seedState") is not None:
        records.append({"kind": "checkpoint", "scenarioId": scenario_id, "q": q, "sequence": sequence, "status": "seeded", "seedState": payload["seedState"]})
        sequence += 1
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    started = time.monotonic()
    cancel_sent = threading.Event()
    tool_cancel_scheduled = threading.Event()
    deadline_expired = threading.Event()

    def record(kind: str, **extra: Any) -> None:
        nonlocal sequence
        with records_lock:
            records.append({"kind": kind, "scenarioId": scenario_id, "q": q, "sequence": sequence, **extra})
            sequence += 1

    def send(response: dict[str, Any]) -> None:
        with stdin_lock:
            if process.poll() is not None or process.stdin.closed:
                return
            process.stdin.write(json.dumps(response) + "\n")
            process.stdin.flush()

    def allowed_for(tool_name: str, call_payload: dict[str, Any]) -> bool:
        asked = tool_name in permission.get("ask", [])
        if tool_name == "read_file":
            input_value = call_payload.get("input") if isinstance(call_payload.get("input"), dict) else call_payload.get("arguments")
            path = str((input_value or {}).get("path") or "") if isinstance(input_value, dict) else ""
            allowed_paths = scenario.get("seedState", {}).get("allowedReadFiles", []) if isinstance(scenario.get("seedState"), dict) else []
            return path in allowed_paths
        return tool_name not in permission.get("deny", []) and not (asked and permission.get("answer") == "deny")

    def request_cancel() -> None:
        if cancel_sent.is_set() or process.poll() is not None:
            return
        cancel_sent.set()
        post(f"{mock}/control/cancel", {"runKey": run_key})
        send({"kind": "request", "messageId": "cancel-1", "method": "cancel", "runId": "run-parity", "operationId": f"op-{scenario_id}", "requestId": "request-1", "reason": "parity_cancel"})

    def cancel_after(delay_ms: int) -> None:
        if delay_ms <= 0:
            return
        threading.Event().wait(delay_ms / 1000)
        request_cancel()

    def cancel_later() -> None:
        cancel_after(int(limits.get("cancelAfterMs") or 0))

    threading.Thread(target=cancel_later, daemon=True).start()

    def cancel_mock_at_deadline() -> None:
        deadline_ms = int(limits.get("deadlineMs") or 0)
        if deadline_ms <= 0:
            return
        threading.Event().wait(deadline_ms / 1000)
        deadline_expired.set()
        try:
            post(f"{mock}/control/cancel", {"runKey": run_key})
        except Exception:
            # The sidecar deadline remains authoritative when the controllable
            # fault endpoint has already been torn down.
            pass

    threading.Thread(target=cancel_mock_at_deadline, daemon=True).start()

    def respond_to_module_call(
        message: dict[str, Any],
        module: str,
        call_payload: dict[str, Any],
        attempt: int | None = None,
        fault: dict[str, Any] | None = None,
    ) -> None:
        if module == "model":
            fault_error = model_fault_error(str(fault.get("action") or "")) if fault else None
            if fault_error:
                code, failure_message, retryable = fault_error
                record("fault.injected", target="model", action=fault["action"], attempt=attempt)
                record("model.error", code=code, message=failure_message, retryable=retryable, attempt=attempt)
                response = {"kind": "response", "messageId": f"model-response-{attempt}", "inReplyTo": message["messageId"], "requestId": message.get("requestId"), "ok": False, "final": True, "outcome": "failed", "code": code, "error": {"message": failure_message, "retryable": retryable}}
            else:
                model_request = call_payload.get("request") if isinstance(call_payload.get("request"), dict) else {}
                model_response = post(f"{mock}/v1/chat/completions", {"scenarioId": scenario_id, "q": q, "runKey": run_key, "messages": model_request.get("messages", messages), "delays": scenario.get("delays"), "faults": scenario.get("faults")})
                choice = model_response["choices"][0]["message"]
                events: list[dict[str, Any]] = [{"type": "message_start", "role": "assistant"}]
                if choice.get("tool_calls"):
                    for call in choice["tool_calls"]:
                        events.append({"type": "tool_call_end", "toolCall": {"id": call["id"], "name": call["function"]["name"], "input": json.loads(call["function"]["arguments"])}})
                    events.append({"type": "message_end", "finishReason": "tool_call"})
                else:
                    events.extend([{"type": "text_delta", "text": choice.get("content") or ""}, {"type": "message_end", "finishReason": "stop"}])
                response = {"kind": "response", "messageId": f"model-response-{attempt}", "inReplyTo": message["messageId"], "requestId": message.get("requestId"), "ok": True, "payload": {"events": events}}
                if not deadline_expired.is_set():
                    record("model.response", attempt=attempt, response=choice)
        elif module == "capability":
            tool_name = str(call_payload.get("name") or "")
            tool_call_id = call_payload.get("toolCallId")
            allowed = allowed_for(tool_name, call_payload)
            if allowed:
                result = post(f"{mock}/tools/execute", {"scenarioId": scenario_id, "q": q, "runKey": run_key, "name": tool_name, "arguments": call_payload.get("arguments") or {}, "permissionAllowed": True, "delays": scenario.get("delays"), "toolDelays": scenario.get("toolDelays"), "faults": scenario.get("faults")})
            else:
                result = {
                    "type": "error",
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "message": "Deterministic permission denial.",
                        "retryable": False,
                    },
                }
            result.setdefault("toolCallId", call_payload.get("toolCallId"))
            result.setdefault("toolName", tool_name)
            result["content"] = ([{"type": "text", "text": json.dumps(result.get("data", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))}] if result.get("type") == "success" else [{"type": "text", "text": result.get("error", {}).get("message", "mock tool error")}])
            result.setdefault("startedAt", "2026-01-01T00:00:00.000Z")
            result.setdefault("completedAt", "2026-01-01T00:00:00.000Z")
            response = {"kind": "response", "messageId": f"tool-response-{message['messageId']}", "inReplyTo": message["messageId"], "requestId": message.get("requestId"), "ok": True, "payload": result}
            trace_result: dict[str, Any] = {"type": result.get("type"), "toolName": result.get("toolName")}
            if result.get("type") == "success":
                trace_result["data"] = result.get("data")
            else:
                trace_result["error"] = result.get("error")
            is_confirmed_cancellation = (
                isinstance(result.get("error"), dict)
                and result["error"].get("code") == "CANCELLED"
            )
            if allowed and (not deadline_expired.is_set() or is_confirmed_cancellation):
                record(
                    "tool.result",
                    toolCallId=tool_call_id,
                    concurrencySafe=tool_name in concurrency_safe_tools,
                    result=trace_result,
                )
        elif module == "permission":
            tool = call_payload.get("tool") if isinstance(call_payload.get("tool"), dict) else {}
            tool_name = str(tool.get("name") or "")
            allowed = allowed_for(tool_name, call_payload)
            decision = {"type": "allow", "reason": {"type": "tool", "toolName": tool_name, "message": "allowed"}} if allowed else {"type": "deny", "message": "Deterministic permission denial.", "reason": {"type": "tool", "toolName": tool_name, "message": "denied"}}
            response = {"kind": "response", "messageId": f"permission-response-{message['messageId']}", "inReplyTo": message["messageId"], "requestId": message.get("requestId"), "ok": True, "payload": {"decision": decision}}
        else:
            response = {"kind": "response", "messageId": f"module-response-{message['messageId']}", "inReplyTo": message["messageId"], "requestId": message.get("requestId"), "ok": True, "payload": {"accepted": True}}
        send(response)

    def start_module_worker(message: dict[str, Any]) -> None:
        nonlocal model_attempt
        module = str(message.get("module") or "")
        call_payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        attempt: int | None = None
        fault: dict[str, Any] | None = None
        if module == "model":
            with records_lock:
                model_attempt += 1
                attempt = model_attempt
            model_request = call_payload.get("request") if isinstance(call_payload.get("request"), dict) else {}
            record("model.request", attempt=attempt, request=model_request)
            fault = model_fault_at(scenario, attempt)
        elif module == "capability":
            tool_name = str(call_payload.get("name") or "")
            allowed = allowed_for(tool_name, call_payload)
            record("permission.decision", toolName=tool_name, allowed=allowed)
            if allowed:
                record(
                    "tool.call",
                    name=tool_name,
                    toolCallId=call_payload.get("toolCallId"),
                    concurrencySafe=tool_name in concurrency_safe_tools,
                    arguments=call_payload.get("arguments") or {},
                )
            if allowed and not tool_cancel_scheduled.is_set() and int(limits.get("cancelAfterToolStartMs") or 0) > 0:
                tool_cancel_scheduled.set()
                threading.Thread(target=cancel_after, args=(int(limits["cancelAfterToolStartMs"]),), daemon=True).start()
        elif module == "permission":
            tool = call_payload.get("tool") if isinstance(call_payload.get("tool"), dict) else {}
            tool_name = str(tool.get("name") or "")
            record("permission.decision", toolName=tool_name, allowed=allowed_for(tool_name, call_payload))

        worker = threading.Thread(target=respond_to_module_call, args=(message, module, call_payload, attempt, fault), daemon=True)
        with worker_lock:
            workers.append(worker)
        worker.start()

    def drain_module_workers() -> None:
        with worker_lock:
            active_workers = list(workers)
        for worker in active_workers:
            worker.join(timeout=3)

    try:
        for line in process.stdout:
            if time.monotonic() - started > 15:
                record("terminal", outcome="result_unknown", code="ADAPTER_TIMEOUT")
                break
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("kind") == "request" and message.get("method") == "module_call":
                start_module_worker(message)
                continue
            if (
                message.get("kind") == "response"
                and message.get("inReplyTo") == request["messageId"]
                and message.get("final") is True
            ):
                # A streaming execute may be rejected before it gets a stream
                # (for example, an already-expired operation deadline). It is
                # still the protocol terminal and must not be mistaken for a
                # missing sidecar event.
                record(
                    "terminal",
                    outcome=message.get("outcome"),
                    code=message.get("code"),
                    stopReason="aborted_streaming" if message.get("code") == "DEADLINE_EXCEEDED" else None,
                    structuredResult=None,
                    output="",
                )
                break
            if message.get("kind") == "event":
                if message.get("final"):
                    # A terminal outcome is only observable after every accepted
                    # module call has produced its cancellation/result projection.
                    drain_module_workers()
                    terminal_payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    result = terminal_payload.get("result") if isinstance(terminal_payload.get("result"), dict) else {}
                    final_message = result.get("finalMessage") if isinstance(result.get("finalMessage"), dict) else {}
                    content = final_message.get("content") if isinstance(final_message.get("content"), list) else []
                    output_text = "".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text")
                    record("terminal", outcome=message.get("outcome"), code=message.get("code"), stopReason=result.get("stopReason"), structuredResult=result.get("structuredOutput"), output=output_text)
                    break
                else:
                    payload_event = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    if payload_event.get("type") == "text_delta":
                        record("user.output", text=payload_event.get("text", ""))
    finally:
        drain_module_workers()
        with stdin_lock:
            if not process.stdin.closed:
                process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    write_trace(output, records)
    return 0 if records and any(item["kind"] == "terminal" for item in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
