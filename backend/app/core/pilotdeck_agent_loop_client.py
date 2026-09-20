from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.harness_agent import HarnessExecutionFenced
from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement

STAFFDECK_RESULT_CARRIER_PREFIX = "__STAFFDECK_TASK_RESULT__="
STAFFDECK_RESULT_CARRIER_SUFFIX = "__END__"


class PilotDeckAgentLoopError(RuntimeError):
    """Failure crossing the PilotDeck module boundary."""

    def __init__(self, code: str, message: str, *, result_unknown: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.result_unknown = result_unknown


class StaffDeckModuleError(RuntimeError):
    """Structured module failure returned by a StaffDeck-owned bridge."""

    def __init__(self, code: str, message: str, *, retryability: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryability = retryability


ModuleBridge = Callable[[str, dict[str, Any]], Mapping[str, Any]]
TraceSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class PilotDeckExecutionIdentity:
    tenant_id: str
    session_id: str
    turn_id: str
    run_id: str
    operation_id: str
    idempotency_key: str
    step_id: str | None = None


@dataclass(frozen=True)
class PilotDeckExecutionContext:
    """Ephemeral host-owned context projected into one sidecar execution."""

    remaining_actions: int | None = None
    conversation_context: dict[str, Any] | None = None
    permission_context: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    context_override: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        override = dict(self.context_override or {})
        if self.conversation_context is not None:
            messages = self.conversation_context.get("messages")
            if isinstance(messages, list):
                if self.context_override is None:
                    payload["messages"] = list(messages)
                elif "messages" not in override:
                    override["messages"] = list(messages)
        if self.permission_context is not None:
            payload["permissionContext"] = dict(self.permission_context)
        execution_context: dict[str, Any] = {}
        if self.remaining_actions is not None:
            execution_context["remainingActions"] = int(self.remaining_actions)
        if self.metadata:
            execution_context.update(self.metadata)
        if execution_context:
            payload["executionContext"] = execution_context
        if override:
            payload["contextOverride"] = override
        return payload


class PilotDeckAgentLoopClient:
    """Synchronous StaffDeck client for a PilotDeck AgentLoop sidecar."""

    def __init__(
        self,
        command: str | list[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = 900.0,
        startup_timeout_seconds: float = 10.0,
    ) -> None:
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.command:
            raise ValueError("PilotDeck sidecar command must not be empty")
        self.cwd = cwd
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.startup_timeout_seconds = max(0.1, float(startup_timeout_seconds))
        self._process: subprocess.Popen[bytes] | None = None
        self._message_counter = 0
        self._connection_generation: str | None = None
        self._stdout_buffer = bytearray()

    def close(self) -> None:
        process = self._process
        self._process = None
        self._connection_generation = None
        self._stdout_buffer.clear()
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def execute(
        self,
        requirement: TaskRequirement,
        *,
        identity: PilotDeckExecutionIdentity,
        checkpoint: dict[str, Any] | None = None,
        execution_context: PilotDeckExecutionContext | None = None,
        context_override: dict[str, Any] | None = None,
        bridge: ModuleBridge | None = None,
        trace_sink: TraceSink | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        deadline_monotonic: float | None = None,
    ) -> TaskExecutionResult:
        self._ensure_process()
        self._require_process()
        request_id = self._next_id("execute")
        message_id = self._next_id("message")
        operation_deadline = _deadline_iso(deadline_monotonic, self.timeout_seconds)
        request_payload = {
            "task": {
                "prompt": json.dumps(requirement.model_dump(mode="json"), ensure_ascii=False),
            },
            "tools": [
                {
                    **item.model_dump(mode="json"),
                    "readOnly": bool(
                        item.metadata.get("readOnly", item.metadata.get("read_only", False))
                    ),
                    # HarnessTaskAgent executes one capability action at a time;
                    # keep the StaffDeck projection serial even when a manifest
                    # advertises a generally concurrency-safe tool.
                    "concurrencySafe": False,
                }
                for item in requirement.capability_manifest.available
                if item.available
            ],
            **(execution_context.as_payload() if execution_context is not None else {}),
        }
        if context_override:
            existing_override = request_payload.get("contextOverride")
            request_payload["contextOverride"] = {
                **(existing_override if isinstance(existing_override, dict) else {}),
                **context_override,
            }
            if "tools" not in request_payload["contextOverride"]:
                request_payload["contextOverride"]["tools"] = list(request_payload["tools"])
        seed_state = _agent_loop_seed_state(checkpoint)
        if seed_state is not None:
            request_payload["seedState"] = seed_state
        request = {
            "kind": "request",
            "messageId": message_id,
            "method": "execute",
            "runId": identity.run_id,
            "operationId": identity.operation_id,
            "requestId": request_id,
            "sessionId": identity.session_id,
            "turnId": identity.turn_id,
            "idempotencyKey": identity.idempotency_key,
            "operationDeadline": operation_deadline,
            "payload": request_payload,
        }
        self._write(request)
        stream_id: str | None = None
        expected_sequence = 0
        started = time.monotonic()
        cancel_sent = False
        cancel_observed_at: float | None = None
        read_poll_seconds = 0.25
        executed_capability_results: list[dict[str, Any]] = []
        while True:
            remaining = self._remaining_timeout(started, deadline_monotonic)
            if remaining <= 0:
                self._send_cancel(identity, request_id, "deadline_exceeded")
                raise PilotDeckAgentLoopError(
                    "DEADLINE_EXCEEDED",
                    "PilotDeck AgentLoop operation exceeded its deadline.",
                    result_unknown=True,
                )
            if is_cancelled and is_cancelled() and not cancel_sent:
                self._send_cancel(identity, request_id, "staffdeck_cancelled")
                cancel_sent = True
                cancel_observed_at = time.monotonic()
            if cancel_observed_at is not None and time.monotonic() - cancel_observed_at > 2.0:
                raise PilotDeckAgentLoopError(
                    "CANCELLED_UNCONFIRMED",
                    "PilotDeck AgentLoop did not confirm cancellation before the grace period expired.",
                    result_unknown=True,
                )
            try:
                message = self._read(min(remaining, read_poll_seconds))
            except PilotDeckAgentLoopError as exc:
                if exc.code == "SIDECAR_TIMEOUT":
                    continue
                raise
            if message.get("kind") == "request" and message.get("method") == "module_call":
                response = self._dispatch_module_call(message, bridge)
                capability_result = _capability_result_from_module_exchange(message, response)
                if capability_result is not None:
                    executed_capability_results.append(capability_result)
                self._write(response)
                continue
            if message.get("kind") == "error":
                raise PilotDeckAgentLoopError(
                    str(message.get("code") or "MODULE_ERROR"),
                    str(message.get("message") or "PilotDeck module error"),
                    result_unknown=message.get("retryability") == "retry_after_status",
                )
            if message.get("kind") == "response":
                if message.get("inReplyTo") != message_id:
                    continue
                if message.get("requestId") != request_id:
                    raise PilotDeckAgentLoopError("REQUEST_MISMATCH", "PilotDeck execute response request identity changed.")
                if message.get("ok") is not True:
                    raise PilotDeckAgentLoopError(
                        str(message.get("code") or "EXECUTE_REJECTED"),
                        str(message.get("error", {}).get("message") if isinstance(message.get("error"), dict) else "PilotDeck execute request was rejected."),
                        result_unknown=message.get("outcome") == "result_unknown",
                    )
                if message.get("final") is True:
                    raise PilotDeckAgentLoopError("UNEXPECTED_EXECUTE_RESPONSE", "PilotDeck execute terminal result must be an event.")
                if message.get("streamId"):
                    stream_id = str(message["streamId"])
                continue
            if message.get("kind") != "event":
                continue
            if (
                message.get("runId") != identity.run_id
                or message.get("operationId") != identity.operation_id
                or message.get("requestId") != request_id
            ):
                continue
            if stream_id is None:
                stream_id = str(message.get("streamId") or "")
            if str(message.get("streamId") or "") != stream_id:
                raise PilotDeckAgentLoopError("STREAM_MISMATCH", "PilotDeck stream identity changed.")
            sequence = int(message.get("sequence", -1))
            if sequence < expected_sequence:
                continue
            if sequence > expected_sequence:
                raise PilotDeckAgentLoopError("SEQUENCE_GAP", "PilotDeck stream sequence has a gap.", result_unknown=True)
            expected_sequence += 1
            payload = message.get("payload")
            if message.get("final") is not True and isinstance(payload, dict) and trace_sink:
                trace_sink(str(message.get("eventType") or "agent.event"), dict(payload))
            if message.get("final") is True:
                outcome = str(message.get("outcome") or "failed")
                if outcome == "cancelled":
                    raise PilotDeckAgentLoopError("CANCELLED", "PilotDeck AgentLoop was cancelled.")
                if outcome == "result_unknown":
                    error = message.get("error")
                    detail = error.get("message") if isinstance(error, dict) else None
                    raise PilotDeckAgentLoopError(
                        str(message.get("code") or "RESULT_UNKNOWN"),
                        str(detail or "PilotDeck result requires reconciliation."),
                        result_unknown=True,
                    )
                if cancel_sent:
                    raise PilotDeckAgentLoopError(
                        "CANCELLED_AFTER_REQUEST",
                        "PilotDeck completed after StaffDeck cancellation was observed.",
                        result_unknown=True,
                    )
                if outcome != "completed":
                    module_failure = payload.get("moduleFailure") if isinstance(payload, dict) else None
                    module_failure_code = (
                        str(module_failure.get("code") or "")
                        if isinstance(module_failure, dict)
                        else ""
                    )
                    if (
                        str(message.get("code") or "") == "ACTION_BUDGET_EXHAUSTED"
                        or module_failure_code == "ACTION_BUDGET_EXHAUSTED"
                    ):
                        return self._finalize_result(
                            TaskExecutionResult(
                                task_frame_id=requirement.task_frame_id,
                                status="action_budget",
                                reply_fragment="当前任务已达到本轮自动执行上限，需要下一轮继续。",
                                action_count=0,
                                error={"code": "ACTION_BUDGET_EXHAUSTED", "message": "StaffDeck action budget exhausted."},
                            ),
                            payload if isinstance(payload, dict) else {},
                            checkpoint,
                            executed_capability_results,
                            requirement.required_capability_names,
                        )
                    error = message.get("error")
                    detail = error.get("message") if isinstance(error, dict) else None
                    raise PilotDeckAgentLoopError(
                        str(message.get("code") or "EXECUTE_FAILED"),
                        str(detail or "PilotDeck AgentLoop execution failed."),
                    )
                return self._result_from_payload(
                    payload,
                    message,
                    requirement,
                    checkpoint,
                    executed_capability_results,
                    requirement.required_capability_names,
                )

    def _dispatch_module_call(
        self,
        request: Mapping[str, Any],
        bridge: ModuleBridge | None,
    ) -> dict[str, Any]:
        module = str(request.get("module") or "")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            if bridge is None:
                raise PilotDeckAgentLoopError("MODULE_BRIDGE_UNAVAILABLE", f"No StaffDeck bridge for {module}.")
            result = dict(bridge(module, payload))
            return {
                "kind": "response",
                "messageId": self._next_id("module-result"),
                "inReplyTo": str(request.get("messageId") or ""),
                "requestId": str(request.get("requestId") or ""),
                "ok": True,
                "final": True,
                "outcome": "completed",
                "payload": result,
            }
        except StaffDeckModuleError as exc:
            return {
                "kind": "response",
                "messageId": self._next_id("module-error"),
                "inReplyTo": str(request.get("messageId") or ""),
                "requestId": str(request.get("requestId") or ""),
                "ok": False,
                "final": True,
                "outcome": "failed",
                "code": exc.code,
                "retryability": exc.retryability,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except PilotDeckAgentLoopError as exc:
            return {
                "kind": "response",
                "messageId": self._next_id("module-error"),
                "inReplyTo": str(request.get("messageId") or ""),
                "requestId": str(request.get("requestId") or ""),
                "ok": False,
                "final": True,
                "outcome": "result_unknown" if exc.result_unknown else "failed",
                "code": exc.code,
                "error": {"message": str(exc)},
            }
        except HarnessExecutionFenced as exc:
            return {
                "kind": "response",
                "messageId": self._next_id("module-error"),
                "inReplyTo": str(request.get("messageId") or ""),
                "requestId": str(request.get("requestId") or ""),
                "ok": False,
                "final": True,
                "outcome": "result_unknown",
                "code": "RESULT_UNKNOWN",
                "error": {"code": "RESULT_UNKNOWN", "message": str(exc)},
            }
        except Exception as exc:  # noqa: BLE001 - preserve arbitrary host module failures
            return {
                "kind": "response",
                "messageId": self._next_id("module-error"),
                "inReplyTo": str(request.get("messageId") or ""),
                "requestId": str(request.get("requestId") or ""),
                "ok": False,
                "final": True,
                "outcome": "failed",
                "code": str(getattr(exc, "code", "STAFFDECK_MODULE_ERROR")),
                "error": {"code": str(getattr(exc, "code", "STAFFDECK_MODULE_ERROR")), "message": str(exc)},
            }

    def _send_cancel(self, identity: PilotDeckExecutionIdentity, request_id: str, reason: str) -> None:
        try:
            self._write({
                "kind": "request",
                "messageId": self._next_id("cancel"),
                "method": "cancel",
                "runId": identity.run_id,
                "operationId": identity.operation_id,
                "requestId": request_id,
                "reason": reason,
            })
        except PilotDeckAgentLoopError:
            pass

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=os.environ.copy(),
            )
            hello_id = self._next_id("hello")
            self._write({
                "kind": "request",
                "messageId": hello_id,
                "method": "hello",
                "payload": {"protocolVersion": "2.0", "client": "staffdeck"},
            })
            response = self._read(self.startup_timeout_seconds)
            if response.get("kind") != "response" or response.get("ok") is not True:
                raise PilotDeckAgentLoopError("HANDSHAKE_FAILED", "PilotDeck sidecar hello failed.")
            self._connection_generation = str(response.get("connectionGeneration") or "")
        except (OSError, PilotDeckAgentLoopError):
            self.close()
            raise

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            raise PilotDeckAgentLoopError("SIDECAR_NOT_RUNNING", "PilotDeck sidecar is not running.", result_unknown=True)
        return self._process

    def _write(self, message: Mapping[str, Any]) -> None:
        process = self._require_process() if self._process is not None else self._process
        if process is None or process.stdin is None:
            raise PilotDeckAgentLoopError("SIDECAR_NOT_RUNNING", "PilotDeck sidecar stdin is unavailable.", result_unknown=True)
        try:
            process.stdin.write((json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        except OSError as exc:
            raise PilotDeckAgentLoopError("SIDECAR_WRITE_FAILED", str(exc), result_unknown=True) from exc

    def _read(self, timeout: float) -> dict[str, Any]:
        process = self._require_process()
        if process.stdout is None:
            raise PilotDeckAgentLoopError("SIDECAR_STDOUT_UNAVAILABLE", "PilotDeck sidecar stdout is unavailable.", result_unknown=True)
        deadline = time.monotonic() + max(0.01, timeout)
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[: newline + 1]
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PilotDeckAgentLoopError("SIDECAR_TIMEOUT", "Timed out waiting for PilotDeck sidecar.", result_unknown=True)
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise PilotDeckAgentLoopError("SIDECAR_TIMEOUT", "Timed out waiting for PilotDeck sidecar.", result_unknown=True)
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise PilotDeckAgentLoopError("SIDECAR_EXITED", "PilotDeck sidecar exited before a terminal result.", result_unknown=True)
            self._stdout_buffer.extend(chunk)
        try:
            value = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PilotDeckAgentLoopError("INVALID_SIDECAR_JSON", "PilotDeck sidecar returned invalid JSON.", result_unknown=True) from exc
        if not isinstance(value, dict):
            raise PilotDeckAgentLoopError("INVALID_SIDECAR_MESSAGE", "PilotDeck sidecar message must be an object.", result_unknown=True)
        return value

    def _result_from_payload(
        self,
        payload: Any,
        message: Mapping[str, Any],
        requirement: TaskRequirement,
        checkpoint: dict[str, Any] | None,
        executed_capability_results: list[dict[str, Any]],
        required_capability_names: list[str],
    ) -> TaskExecutionResult:
        outcome = str(message.get("outcome") or "failed")
        if outcome != "completed":
            raise PilotDeckAgentLoopError(
                str(message.get("code") or "EXECUTE_FAILED"),
                "PilotDeck returned a non-completed result payload.",
            )
        raw_payload = payload if isinstance(payload, dict) else {}
        raw_result = raw_payload.get("result") if isinstance(raw_payload.get("result"), dict) else raw_payload
        if isinstance(raw_result, dict):
            carrier_text = _final_message_text(raw_payload)
            if not carrier_text:
                carrier_text = _last_assistant_message_text(raw_payload.get("messages"))
            carrier_result, _ = _extract_staffdeck_result(carrier_text, requirement.task_frame_id)
            if carrier_result is not None:
                return self._finalize_result(
                    carrier_result,
                    raw_payload,
                    checkpoint,
                    executed_capability_results,
                    required_capability_names,
                )
            try:
                return self._finalize_result(
                    TaskExecutionResult.model_validate(raw_result),
                    raw_payload,
                    checkpoint,
                    executed_capability_results,
                    required_capability_names,
                )
            except (TypeError, ValueError):
                text = _final_message_text(raw_payload) or _final_message_text(raw_result)
                parsed = _parse_task_result_text(text, requirement.task_frame_id)
                if parsed is not None:
                    return self._finalize_result(
                        parsed,
                        raw_payload,
                        checkpoint,
                        executed_capability_results,
                        required_capability_names,
                    )
                agent_result = raw_result
                if agent_result.get("type") == "success":
                    result = TaskExecutionResult(
                        task_frame_id=requirement.task_frame_id,
                        status="completed",
                        reply_fragment=text,
                        action_count=max(1, int(agent_result.get("turns") or 1)),
                        structured_result=agent_result.get("structuredOutput"),
                    )
                    return self._finalize_result(
                        result,
                        raw_payload,
                        checkpoint,
                        executed_capability_results,
                        required_capability_names,
                    )
        raise PilotDeckAgentLoopError(
            "INVALID_RESULT",
            "PilotDeck returned an invalid TaskExecutionResult.",
        )

    def _finalize_result(
        self,
        result: TaskExecutionResult,
        payload: Mapping[str, Any],
        checkpoint: dict[str, Any] | None,
        executed_capability_results: list[dict[str, Any]],
        required_capability_names: list[str],
    ) -> TaskExecutionResult:
        """Keep StaffDeck-owned checkpoint fields while adding a safe generic snapshot."""
        if executed_capability_results:
            result.capability_results = [
                *result.capability_results,
                *executed_capability_results,
            ]
            # The legacy harness charges each capability action and its
            # terminal model decision.  Only count calls observed on this
            # sidecar execution; inherited canonical history is not new work.
            result.action_count = max(
                result.action_count,
                len(executed_capability_results) + 1,
            )
        successful_capabilities = {
            str(item.get("tool_name") or "")
            for item in result.capability_results
            if item.get("success") is True
        }
        missing_required = [
            name
            for name in required_capability_names
            if name not in successful_capabilities
        ]
        if result.status == "completed" and missing_required:
            result.status = "action_budget"
            result.reply_fragment = "当前任务已达到本轮自动执行上限，需要下一轮继续。"
            result.error = {
                "code": "ACTION_BUDGET_EXHAUSTED",
                "message": "StaffDeck action budget exhausted.",
            }
        merged = dict(checkpoint or {})
        messages = payload.get("messages")
        if isinstance(messages, list):
            merged["agentLoopMessages"] = _checkpoint_messages(messages)
        seed_state = _agent_loop_seed_state(checkpoint)
        if seed_state is not None:
            merged["agentLoopSeedState"] = seed_state
        result.loop_checkpoint = merged
        return result

    def _remaining_timeout(self, started: float, deadline_monotonic: float | None) -> float:
        remaining = self.timeout_seconds - (time.monotonic() - started)
        if deadline_monotonic is not None:
            remaining = min(remaining, deadline_monotonic - time.monotonic())
        return remaining

    def _next_id(self, prefix: str) -> str:
        self._message_counter += 1
        return f"{prefix}-{self._message_counter}"


def _capability_result_from_module_exchange(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Persist only the capability invocation observed in this execution."""

    if str(request.get("module") or "") != "capability" or response.get("ok") is not True:
        return None
    request_payload = request.get("payload")
    response_payload = response.get("payload")
    if not isinstance(request_payload, Mapping) or not isinstance(response_payload, Mapping):
        return None
    name = str(response_payload.get("toolName") or request_payload.get("name") or "").strip()
    if not name:
        return None
    failure = response_payload.get("type") == "error"
    raw_error = response_payload.get("error")
    return {
        "tool_name": name,
        "success": not failure,
        "data": response_payload.get("data"),
        "error": dict(raw_error) if isinstance(raw_error, Mapping) else None,
    }


def _final_message_text(payload: Mapping[str, Any]) -> str:
    final_message = payload.get("finalMessage")
    if not isinstance(final_message, Mapping):
        return ""
    content = final_message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, Mapping) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _last_assistant_message_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for raw in reversed(messages):
        if not isinstance(raw, Mapping) or raw.get("role") != "assistant":
            continue
        content = raw.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            continue
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return ""


def _extract_staffdeck_result(
    text: str,
    task_frame_id: str,
) -> tuple[TaskExecutionResult | None, str]:
    """Decode the StaffDeck-only opaque carrier from an assistant text result."""

    start = text.find(STAFFDECK_RESULT_CARRIER_PREFIX)
    if start < 0:
        return None, text
    payload_start = start + len(STAFFDECK_RESULT_CARRIER_PREFIX)
    end = text.find(STAFFDECK_RESULT_CARRIER_SUFFIX, payload_start)
    if end < 0:
        return None, text
    encoded = text[payload_start:end]
    try:
        value = json.loads(encoded)
    except (TypeError, json.JSONDecodeError):
        return None, text
    if not isinstance(value, dict):
        return None, text
    candidate = dict(value)
    candidate.setdefault("task_frame_id", task_frame_id)
    if not candidate.get("reply_fragment") and isinstance(candidate.get("reply"), str):
        candidate["reply_fragment"] = candidate["reply"]
    if candidate.get("status") not in {
        "completed",
        "awaiting_user",
        "handoff",
        "failed",
        "blocked",
        "action_budget",
    }:
        if candidate.get("handoff") is True or candidate.get("action") == "handoff":
            candidate["status"] = "handoff"
        elif candidate.get("action") == "finish":
            candidate["status"] = "completed"
        else:
            return None, text
    if candidate.get("error") is not None and not isinstance(candidate.get("error"), dict):
        candidate["error"] = {"message": str(candidate["error"])}
    if candidate.get("retryability") is not None:
        error = dict(candidate.get("error") or {})
        error.setdefault("retryability", candidate["retryability"])
        candidate["error"] = error
    try:
        result = TaskExecutionResult.model_validate(candidate)
    except (TypeError, ValueError):
        return None, text
    cleaned = (text[:start] + text[end + len(STAFFDECK_RESULT_CARRIER_SUFFIX):]).strip()
    if not result.reply_fragment and cleaned:
        result.reply_fragment = cleaned
    return result, cleaned


def _agent_loop_seed_state(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project only the generic PilotDeck seed state from host checkpoint data."""

    if not isinstance(checkpoint, dict):
        return None
    seed_state = checkpoint.get("agentLoopSeedState")
    return dict(seed_state) if isinstance(seed_state, dict) else None


def _checkpoint_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Persist canonical messages without transient image data URLs."""
    sanitized: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        content = item.get("content")
        if isinstance(content, list):
            safe_content: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    safe_content.append({
                        "type": "text",
                        "text": "[image omitted from checkpoint; reattached for the next execution]",
                    })
                else:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = str(block.get("text") or "")
                        _, cleaned = _extract_staffdeck_result(text, "")
                        safe_content.append({**block, "text": cleaned})
                    else:
                        safe_content.append(block)
            item["content"] = safe_content
        sanitized.append(item)
    return sanitized


def _parse_task_result_text(text: str, task_frame_id: str) -> TaskExecutionResult | None:
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    value.setdefault("task_frame_id", task_frame_id)
    if value.get("status") not in {
        "completed",
        "awaiting_user",
        "handoff",
        "failed",
        "blocked",
        "action_budget",
    }:
        return None
    try:
        return TaskExecutionResult.model_validate(value)
    except (TypeError, ValueError):
        return None

def _deadline_iso(deadline_monotonic: float | None, timeout_seconds: float) -> str:
    seconds = timeout_seconds if deadline_monotonic is None else max(0.0, deadline_monotonic - time.monotonic())
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


__all__ = [
    "ModuleBridge",
    "PilotDeckAgentLoopClient",
    "PilotDeckAgentLoopError",
    "PilotDeckExecutionContext",
    "PilotDeckExecutionIdentity",
    "StaffDeckModuleError",
]
