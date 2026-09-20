"""Deterministic provider/tool backend for cross-host AgentLoop parity tests."""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class MockHandler(BaseHTTPRequestHandler):
    server_version = "AgentLoopParityMock/1"

    def log_message(self, *_args: Any) -> None:
        return

    def _json(self, status: int, body: dict[str, Any]) -> None:
        record_path = getattr(self.server, "record_path", None)
        if record_path is not None:
            with record_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"direction": "response", "status": status, "body": body}, ensure_ascii=False) + "\n")
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "agent-loop-parity-mock"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(size) or b"{}")
            record_path = getattr(self.server, "record_path", None)
            if record_path is not None:
                with record_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"direction": "request", "path": self.path, "body": request}, ensure_ascii=False) + "\n")
            if self.path == "/v1/chat/completions":
                self._model(request)
            elif self.path == "/tools/execute":
                self._tool(request)
            elif self.path == "/control/cancel":
                run_key = str(request.get("runKey") or "default")
                with self.server.state_lock:
                    self.server.cancelled.add(run_key)
                self._json(200, {"ok": True, "runKey": run_key})
            elif self.path == "/control/state":
                run_key = str(request.get("runKey") or "default")
                with self.server.state_lock:
                    counts = dict(self.server.side_effects.get(run_key, {}))
                    attempts = dict(self.server.attempts.get(run_key, {}))
                self._json(200, {"runKey": run_key, "sideEffects": counts, "attempts": attempts})
            else:
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "Unknown mock endpoint."}})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - protocol boundary
            self._json(400, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}})

    def _model(self, request: dict[str, Any]) -> None:
        scenario = str(request.get("scenarioId") or "")
        run_key = str(request.get("runKey") or "default")
        with self.server.state_lock:
            attempts = self.server.attempts.setdefault(run_key, {})
            attempts["model"] = int(attempts.get("model", 0)) + 1
            attempt = attempts["model"]
        faults = request.get("faults") if isinstance(request.get("faults"), dict) else {}
        model_fault = next(
            (
                item
                for item in faults.get("model", [])
                if isinstance(item, dict) and int(item.get("at") or 1) == attempt
            ),
            None,
        )
        if isinstance(model_fault, dict):
            action = model_fault.get("action")
            if action == "retryable_error":
                self._json(503, {"error": {"code": "provider_unavailable", "message": "Deterministic temporary provider failure.", "retryable": True}})
                return
            if action == "non_retryable_error":
                self._json(400, {"error": {"code": "invalid_model_response", "message": "Deterministic permanent provider failure.", "retryable": False}})
                return
            if action == "malformed_response":
                self._json(200, {"choices": []})
                return
        messages = request.get("messages") if isinstance(request.get("messages"), list) else []
        q = str(request.get("q") or "") or _query_from_messages(messages)
        delays = request.get("delays") if isinstance(request.get("delays"), dict) else {}
        delay_ms = int(delays.get("modelMs") or 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        has_tool_result = any(_is_tool_result(item) for item in messages)
        if request.get("planModePolicy") is True:
            turn_index = int(request.get("turnIndex") or 0)
            turn_model_attempt = int(request.get("turnModelAttempt") or 1)
            if turn_index == 3:
                if turn_model_attempt == 1:
                    self._json(200, _tool_completion([("todo_write", {"markdown": "- [ ] Run the write probe"})]))
                    return
                if turn_model_attempt == 2:
                    self._json(200, _tool_completion([("parity_write_probe", {})]))
                    return
                self._json(200, _completion(f"MOCK_PLAN_MODE_TURN[{turn_index}]::{q}"))
                return
            if turn_model_attempt == 1:
                tools = ["enter_plan_mode", "parity_write_probe", "exit_plan_mode", "parity_write_probe"]
                tool_name = tools[turn_index]
                arguments = {"plan_file_path": ".pilotdeck/plans/parity-plan.md"} if tool_name == "exit_plan_mode" else {}
                self._json(200, _tool_completion([(tool_name, arguments)]))
                return
            self._json(200, _completion(f"MOCK_PLAN_MODE_TURN[{turn_index}]::{q}"))
            return
        image_present = any(
            "image_url" in item
            or (item.get("type") == "image" and item.get("source") == "base64")
            for item in _walk(messages)
        )
        if not scenario:
            self._production_model(messages, q, has_tool_result)
            return
        if scenario == "pure_text" or scenario in {"image", "checkpoint_resume", "multimodal_image_and_text", "stale_event", "write_snapshot_resume", "sop_scheduled_task", "sop_handoff_resume"}:
            content = f"MOCK_ANSWER[{scenario}]::{q}"
            if scenario == "image" and not image_present:
                content = "MOCK_IMAGE_MISSING"
            self._json(200, _completion(content))
            return
        if scenario in {"multiple_tool", "multi_tool_ordered", "multi_tool_mixed_permission", "multi_tool_mixed_error", "sop_sibling_tasks", "sop_task_dependency"} and not has_tool_result:
            names = {
                "multi_tool_mixed_permission": ["lookup", "restricted"],
                "multi_tool_mixed_error": ["lookup", "lookup_error"],
            }.get(scenario, ["lookup", "summarize"])
            self._json(200, _tool_completion([(name, {"q": q}) for name in names]))
            return
        if scenario == "sop_multi_action_budget":
            self._json(200, _tool_completion([("loop", {"q": q})]))
            return
        if scenario == "sop_knowledge_budget_exhausted":
            knowledge_results = sum(
                1 for item in _walk(messages) if _is_tool_result(item)
            )
            if knowledge_results <= 2:
                self._json(200, _tool_completion([("knowledge_search", {"q": q})]))
                return
        if scenario in {"single_tool", "tool_error", "permission_denial", "max_turns", "permission_allow", "permission_ask_approve", "permission_ask_deny", "allowed_read_files", "denied_read_files", "cancel_during_tool", "deadline_during_tool", "sidecar_restart_before_effect", "sidecar_restart_after_effect", "duplicate_execute", "sop_single_step_complete", "sop_step_advance", "sop_conditional_transition", "sop_slot_update_and_resume", "sop_known_slot_reuse", "sop_required_capability_gate", "sop_required_capability_failure", "sop_required_knowledge_search", "sop_knowledge_budget_exhausted", "sop_checkpoint_resume", "sop_failed_step_recovery", "sop_team_task", "sop_cancel_during_tool", "sop_deadline_during_tool", "sop_unknown_requeue", "large_tool_result", "tool_retryable_error", "tool_non_retryable_error"} and not has_tool_result:
            tool = {
                "single_tool": "lookup", "tool_error": "lookup_error", "permission_denial": "restricted", "max_turns": "loop",
                "permission_allow": "lookup", "permission_ask_approve": "lookup", "permission_ask_deny": "lookup",
                "allowed_read_files": "read_file", "denied_read_files": "read_file", "cancel_during_tool": "slow_side_effect",
                "deadline_during_tool": "slow_side_effect", "sidecar_restart_before_effect": "side_effect", "sidecar_restart_after_effect": "side_effect", "duplicate_execute": "side_effect",
                "sop_single_step_complete": "lookup", "sop_step_advance": "lookup", "sop_conditional_transition": "lookup",
                "sop_slot_update_and_resume": "lookup", "sop_known_slot_reuse": "lookup", "sop_required_capability_gate": "lookup",
                "sop_required_capability_failure": "lookup_error", "sop_required_knowledge_search": "knowledge_search",
                "sop_knowledge_budget_exhausted": "knowledge_search", "sop_checkpoint_resume": "lookup", "sop_failed_step_recovery": "retryable_error",
                "sop_team_task": "lookup", "large_tool_result": "large_result", "tool_retryable_error": "retryable_error",
                "tool_non_retryable_error": "non_retryable_error", "sop_cancel_during_tool": "slow_side_effect",
                "sop_deadline_during_tool": "slow_side_effect", "sop_unknown_requeue": "side_effect",
            }[scenario]
            arguments = {"q": q}
            if tool == "read_file":
                arguments = {"path": "parity-input.txt" if scenario == "allowed_read_files" else "secret.txt"}
            self._json(200, _tool_completion([(tool, arguments)]))
            return
        if scenario in {"sop_missing_required_slot", "sop_handoff_node", "sop_handoff_routing", "sop_blocked_transition"}:
            status = {"sop_missing_required_slot": "awaiting_user", "sop_handoff_node": "handoff", "sop_handoff_routing": "handoff", "sop_blocked_transition": "blocked"}[scenario]
            self._json(200, _completion(json.dumps({"action": "finish", "status": status, "reply_fragment": f"MOCK_{status.upper()}::{q}", "slot_updates": {}, "next_step_id": None}, ensure_ascii=False)))
            return
        self._json(200, _completion(f"MOCK_AFTER_TOOL[{scenario}]::{q}"))

    def _production_model(self, messages: list[Any], q: str, has_tool_result: bool) -> None:
        """Serve the real StaffDeck OpenAI-compatible request shape.

        The parity scenarios use explicit scenario fields, while a deployed
        StaffDeck request only contains stage prompts and canonical messages.
        Keep this deterministic and deliberately small so it remains a test
        provider rather than a second product planner.
        """
        user_text = "\n".join(
            str(item.get("content") or "")
            for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
        )
        goal_match = re.search(r'"goal"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', user_text)
        if goal_match:
            try:
                q = json.loads(f'"{goal_match.group(1)}"')
            except json.JSONDecodeError:
                q = goal_match.group(1)
        # Production StaffDeck prompts use either an inline space or a newline
        # after the stage marker. Keep the mock tolerant of both wire shapes so
        # a formatting difference cannot silently bypass the real Harness path.
        if re.search(r"当前阶段\s*[:：]\s*TurnPlanner", user_text):
            self._json(200, _completion(json.dumps({
                "decision": "answer_only",
                "confidence": 1,
                "user_intent": q,
                "reason": "deterministic mock",
                "task_frames": [{
                    "task_id": "",
                    "kind": "conversation",
                    "decision": "answer_only",
                    "user_intent": q,
                    "requirements": [q],
                    "slot_hints": {},
                    "depends_on_task_ids": [],
                    "execution_target": "self",
                    "activation_condition": {},
                }],
                "task_updates": [],
            }, ensure_ascii=False, separators=(",", ":"))))
            return
        # Deterministic production-shaped SOP fixture used by the real
        # deployment runner.  The marker is part of the test query, not a
        # StaffDeck-specific protocol branch in the sidecar.
        if "E2E_HANDOFF" in user_text and not re.search(r"当前阶段\s*[:：]\s*TurnPlanner", user_text):
            self._json(200, _completion(json.dumps({
                "action": "handoff",
                "status": "handoff",
                "reply_fragment": "MOCK_HANDOFF::" + q,
                "slot_updates": {},
                "next_step_id": None,
                "task_summary": "deterministic handoff fixture",
                "handoff": True,
            }, ensure_ascii=False, separators=(",", ":"))))
            return
        if has_tool_result:
            self._json(200, _completion(json.dumps({
                "action": "finish",
                "status": "completed",
                "reply_fragment": f"MOCK_AFTER_TOOL::{q}",
                "slot_updates": {},
                "next_step_id": None,
                "task_summary": "deterministic mock",
                "structured_result": None,
            }, ensure_ascii=False, separators=(",", ":"))))
            return
        if "本轮输入（仅用于当前调用" in user_text or "TaskRequirement" in user_text:
            self._json(200, _completion(json.dumps({
                "action": "finish",
                "status": "completed",
                "reply_fragment": f"MOCK_ANSWER::{q}",
                "slot_updates": {},
                "next_step_id": None,
                "task_summary": "deterministic mock",
                "structured_result": None,
            }, ensure_ascii=False, separators=(",", ":"))))
            return
        self._json(200, _completion(f"MOCK_RESPONSE::{q}"))

    def _tool(self, request: dict[str, Any]) -> None:
        scenario = str(request.get("scenarioId") or "")
        delays = request.get("delays") if isinstance(request.get("delays"), dict) else {}
        name = str(request.get("name") or "")
        run_key = str(request.get("runKey") or "default")
        faults = request.get("faults") if isinstance(request.get("faults"), dict) else {}
        barrier_target = int(faults.get("toolBarrier") or 0)
        if barrier_target > 1 and "pilotdeck" in run_key:
            with self.server.barrier_condition:
                arrived = int(self.server.barriers.get(run_key, 0)) + 1
                self.server.barriers[run_key] = arrived
                self.server.barrier_condition.notify_all()
                deadline = time.monotonic() + 2
                while self.server.barriers.get(run_key, 0) < barrier_target:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._json(500, {"error": {"code": "BARRIER_TIMEOUT", "message": "Tool batch did not reach the deterministic barrier."}})
                        return
                    self.server.barrier_condition.wait(timeout=remaining)
        tool_delays = request.get("toolDelays") if isinstance(request.get("toolDelays"), dict) else {}
        delay_ms = int(tool_delays.get(name) or delays.get("toolMs") or 0)
        deadline_epoch_ms = int(request.get("operationDeadlineEpochMs") or 0)

        def cancelled_or_expired() -> bool:
            with self.server.state_lock:
                cancelled = run_key in self.server.cancelled
            return cancelled or (
                deadline_epoch_ms > 0
                and int(time.time() * 1000) >= deadline_epoch_ms
            )

        remaining_ms = delay_ms
        while remaining_ms > 0:
            if cancelled_or_expired():
                self._json(200, {"type": "error", "toolName": name, "error": {"code": "CANCELLED", "message": "Deterministic tool cancellation.", "retryable": False}})
                return
            interval = min(10, remaining_ms)
            time.sleep(interval / 1000)
            remaining_ms -= interval
        if cancelled_or_expired():
            self._json(200, {"type": "error", "toolName": name, "error": {"code": "CANCELLED", "message": "Deterministic tool cancellation.", "retryable": False}})
            return
        args = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
        if name == "lookup_error":
            self._json(200, {"type": "error", "toolName": name, "error": {"code": "MOCK_TOOL_ERROR", "message": "Deterministic tool failure.", "retryable": False}})
            return
        if name == "retryable_error":
            self._json(200, {"type": "error", "toolName": name, "error": {"code": "MOCK_RETRYABLE_ERROR", "message": "Deterministic retryable failure.", "retryable": True}})
            return
        if name == "non_retryable_error":
            self._json(200, {"type": "error", "toolName": name, "error": {"code": "MOCK_NON_RETRYABLE_ERROR", "message": "Deterministic permanent failure.", "retryable": False}})
            return
        if name == "restricted" and request.get("permissionAllowed") is not True:
            self._json(200, {"type": "error", "toolName": name, "error": {"code": "PERMISSION_DENIED", "message": "Deterministic permission denial.", "retryable": False}})
            return
        count_side_effect = request.get("planModePolicy") is not True or name == "parity_write_probe"
        with self.server.state_lock:
            side_effects = self.server.side_effects.setdefault(run_key, {})
            if count_side_effect:
                side_effects[name] = int(side_effects.get(name, 0)) + 1
                side_effect_count = side_effects[name]
            else:
                side_effect_count = None
        data: dict[str, Any] = {"q": args.get("q"), "value": f"MOCK_TOOL_RESULT::{name}::{_stable(args)}"}
        if name == "parity_enter_plan_mode":
            data["requestedMode"] = "plan"
        elif name == "parity_exit_plan_mode":
            data["requestedMode"] = "default"
        if name in {"side_effect", "slow_side_effect", "duplicate_execute", "parity_write_probe"} and side_effect_count is not None:
            data["sideEffectCount"] = side_effect_count
        if name == "read_file":
            data["path"] = args.get("path")
            data["content"] = "deterministic file content"
        if name == "large_result":
            data["payload"] = "x" * 10000
        if name == "knowledge_search":
            data["knowledgeBaseIds"] = args.get("knowledge_base_ids") or ["handbook"]
            data["evidence"] = "Deterministic handbook evidence."
        self._json(200, {"type": "success", "toolName": name, "data": data})


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _query_from_messages(messages: list[Any]) -> str:
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and "本轮用户输入" in content:
            match = re.search(r"本轮用户输入\s*[:：]\s*(.*?)(?:\n\s*\n|\n\s*当前阶段\s*[:：])", content, re.DOTALL)
            if match:
                return match.group(1).strip()
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            marker = "本轮用户输入："
            if marker in content:
                return content.split(marker, 1)[1].split("当前阶段：", 1)[0].strip()
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
    return "real deployment query"
def _is_tool_result(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") == "tool":
        return True
    content = item.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )
def _completion(content: str) -> dict[str, Any]:
    return {"id": "mock-response", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _tool_completion(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {"id": "mock-response", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"mock-call-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, sort_keys=True)}} for i, (name, args) in enumerate(calls)]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), MockHandler)
    server.record_path = args.record
    server.side_effects = {}
    server.attempts = {}
    server.cancelled = set()
    server.state_lock = threading.Lock()
    server.barriers = {}
    server.barrier_condition = threading.Condition(server.state_lock)
    print(json.dumps({"ready": True, "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
