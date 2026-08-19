from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import A2ATaskEvent, A2ATaskRun, Tool, utc_now


_TERMINAL_STATES = {"completed", "failed", "canceled", "cancelled", "rejected"}
_INTERRUPTED_STATES = {"input-required", "auth-required"}
_RUN_LOCKS: dict[str, threading.RLock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


class A2AClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class A2AClient:
    """A durable A2A v1 client with streaming, polling and continuation support."""

    def __init__(
        self,
        db: Session,
        tool: Tool,
        *,
        headers: dict[str, str],
        timeout_seconds: float | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        self.db = db
        self.tool = tool
        self.headers = dict(headers)
        self.config = tool.config_json if isinstance(tool.config_json, dict) else {}
        settings = get_settings()
        configured_timeout = _positive_float(
            (self.config.get("execution") or {}).get("timeout_seconds")
            if isinstance(self.config.get("execution"), dict)
            else None,
            settings.a2a_task_timeout_seconds,
        )
        self.timeout_seconds = max(
            1.0,
            min(configured_timeout, timeout_seconds)
            if timeout_seconds is not None
            else configured_timeout,
        )
        self.poll_interval = _positive_float(
            self.config.get("poll_interval_seconds"), settings.a2a_poll_interval_seconds
        )
        self.agent_id = agent_id
        self.session_id = session_id
        self.invocation_id = invocation_id
        self.endpoint_url = tool.url
        self.protocol_binding = "JSONRPC"
        self.protocol_version = str(self.config.get("a2a_version") or "1.0")
        self.agent_card: dict[str, Any] = {}

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        lock_key = self._invocation_lock_key()
        with _run_lock(lock_key):
            return self._execute_locked(arguments)

    def _execute_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        existing = self._existing_invocation()
        if existing is not None:
            return self._resume_existing(existing)
        self._discover_agent()
        continuation = self._continuation(arguments)
        message = self._message(arguments, continuation)
        run = A2ATaskRun(
            direction="client",
            tenant_id=self.tool.tenant_id,
            tool_id=self.tool.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            invocation_id=self.invocation_id,
            endpoint_url=self.endpoint_url,
            agent_card_url=self._agent_card_url(),
            protocol_binding=self.protocol_binding,
            protocol_version=self.protocol_version,
            remote_task_id=continuation.get("task_id"),
            context_id=continuation.get("context_id"),
            status="submitted",
            request_json={"arguments": arguments, "message": message},
            agent_card_json=self.agent_card,
            started_at=utc_now(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self._event(run, "submitted", {"message": message})

        deadline = time.monotonic() + self.timeout_seconds
        try:
            result = self._send(run, message, deadline=deadline)
            return self._finalize(run, result)
        except A2AClientError as exc:
            run.status = "failed"
            run.error_json = {"code": exc.code, "message": str(exc)}
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            self.db.add(run)
            self.db.commit()
            self._event(run, "failed", run.error_json)
            raise

    def _existing_invocation(self) -> A2ATaskRun | None:
        if not self.invocation_id:
            return None
        return self.db.exec(
            select(A2ATaskRun)
            .where(
                A2ATaskRun.direction == "client",
                A2ATaskRun.tenant_id == self.tool.tenant_id,
                A2ATaskRun.tool_id == self.tool.id,
                A2ATaskRun.invocation_id == self.invocation_id,
            )
            .order_by(A2ATaskRun.created_at.desc())
        ).first()

    def _resume_existing(self, run: A2ATaskRun) -> dict[str, Any]:
        self.endpoint_url = run.endpoint_url or self.tool.url
        self.protocol_binding = run.protocol_binding or "JSONRPC"
        self.protocol_version = run.protocol_version or self.protocol_version
        self.agent_card = dict(run.agent_card_json or {})
        if not self.agent_card:
            self._discover_agent()

        if run.status in _TERMINAL_STATES | _INTERRUPTED_STATES:
            if run.status in {"failed", "rejected"}:
                error = run.error_json or {}
                raise A2AClientError(
                    str(error.get("code") or "A2A_TASK_FAILED"),
                    str(error.get("message") or f"A2A Task {run.status}。"),
                )
            if run.status in {"canceled", "cancelled"}:
                raise A2AClientError("A2A_CANCELLED", "A2A Task 已取消。")
            return self._response_from_run(run)

        request = run.request_json if isinstance(run.request_json, dict) else {}
        message = request.get("message")
        if not isinstance(message, dict):
            arguments = request.get("arguments")
            if not isinstance(arguments, dict):
                raise A2AClientError("A2A_RECOVERY_INVALID", "A2A 恢复记录缺少原始请求。")
            message = self._message(arguments, self._continuation(arguments))

        run.recovery_attempts += 1
        run.updated_at = utc_now()
        self.db.add(run)
        self.db.commit()
        self._event(
            run,
            "recovery_started",
            {"attempt": run.recovery_attempts, "task_id": run.remote_task_id},
        )
        deadline = time.monotonic() + self.timeout_seconds
        try:
            if run.remote_task_id:
                seed = run.result_json if isinstance(run.result_json, dict) else {}
                if not _task_state(seed):
                    seed = {
                        "id": run.remote_task_id,
                        "contextId": run.context_id,
                        "status": {"state": run.status or "working"},
                    }
                result = self._wait_if_needed(run, seed, deadline=deadline)
            else:
                # Reuse the original messageId. A2A servers use it as the
                # idempotency identity when the first response was lost.
                result = self._send(run, message, deadline=deadline)
            return self._finalize(run, result)
        except A2AClientError as exc:
            run.status = "failed"
            run.error_json = {"code": exc.code, "message": str(exc)}
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            self.db.add(run)
            self.db.commit()
            self._event(run, "recovery_failed", run.error_json)
            raise

    def _response_from_run(self, run: A2ATaskRun) -> dict[str, Any]:
        task = run.result_json if isinstance(run.result_json, dict) else {}
        return {
            "a2a_run_id": run.id,
            "task_id": run.remote_task_id,
            "context_id": run.context_id,
            "state": run.status,
            "awaiting_input": run.status == "input-required",
            "task": task,
            "message": _status_message(task),
            "artifacts": list(run.artifacts_json or []),
        }

    def _invocation_lock_key(self) -> str:
        identity = self.invocation_id or uuid.uuid4().hex
        return f"{self.tool.tenant_id}:{self.tool.id}:{identity}"

    def _discover_agent(self) -> None:
        if self.config.get("discover_agent_card", True) is False:
            return
        card_url = self._agent_card_url()
        try:
            with httpx.Client(timeout=min(self.timeout_seconds, 15.0)) as client:
                response = client.get(card_url, headers=self.headers)
                response.raise_for_status()
                card = response.json()
        except Exception as exc:
            if self.config.get("require_agent_card") is True:
                raise A2AClientError("A2A_AGENT_CARD_ERROR", str(exc)) from exc
            return
        if not isinstance(card, dict):
            raise A2AClientError("A2A_AGENT_CARD_INVALID", "Agent Card 必须是 JSON 对象。")
        self.agent_card = card
        interfaces = card.get("supportedInterfaces") or card.get("supported_interfaces")
        if isinstance(interfaces, list):
            selected = next(
                (
                    item
                    for item in interfaces
                    if isinstance(item, dict)
                    and str(item.get("protocolBinding") or item.get("protocol_binding") or "").upper()
                    in {"JSONRPC", "JSON-RPC"}
                    and str(item.get("url") or "").strip()
                ),
                None,
            )
            if selected:
                self.endpoint_url = str(selected["url"]).strip()
                self.protocol_binding = "JSONRPC"
                self.protocol_version = str(
                    selected.get("protocolVersion")
                    or selected.get("protocol_version")
                    or self.protocol_version
                )
        elif str(card.get("url") or "").strip():
            self.endpoint_url = str(card["url"]).strip()

    def _send(
        self,
        run: A2ATaskRun,
        message: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        streaming = bool(
            (self.agent_card.get("capabilities") or {}).get("streaming")
            if isinstance(self.agent_card.get("capabilities"), dict)
            else False
        )
        if self.config.get("streaming") is False:
            streaming = False
        if streaming:
            try:
                last = self._stream_method(
                    run, "SendStreamingMessage", self._send_params(message), deadline=deadline
                )
                if last is not None:
                    return self._wait_if_needed(run, last, deadline=deadline)
            except (A2AClientError, httpx.HTTPError):
                if self.config.get("require_streaming") is True:
                    raise
                self._event(run, "stream_fallback", {})
        result = self._rpc("SendMessage", self._send_params(message), deadline=deadline)
        self._record_result(run, result, event_type="message_result")
        return self._wait_if_needed(run, result, deadline=deadline)

    def _wait_if_needed(
        self,
        run: A2ATaskRun,
        result: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        task = _task_from_event(result)
        if task is None:
            return result
        self._update_task_identity(run, task)
        state = _task_state(task)
        if state in _TERMINAL_STATES | _INTERRUPTED_STATES:
            return task
        if not run.remote_task_id:
            raise A2AClientError("A2A_TASK_INVALID", "working Task 缺少 task id。")

        capabilities = self.agent_card.get("capabilities")
        can_stream = isinstance(capabilities, dict) and bool(capabilities.get("streaming"))
        if can_stream and self.config.get("subscribe", True) is not False:
            try:
                streamed = self._stream_method(
                    run,
                    "SubscribeToTask",
                    {"id": run.remote_task_id},
                    deadline=deadline,
                )
                if streamed is not None:
                    streamed_task = _task_from_event(streamed) or streamed
                    if _task_state(streamed_task) in _TERMINAL_STATES | _INTERRUPTED_STATES:
                        return streamed_task
            except (A2AClientError, httpx.HTTPError):
                self._event(run, "subscribe_fallback", {})

        while time.monotonic() < deadline:
            self.db.refresh(run)
            if run.cancel_requested:
                self._cancel_remote(run, deadline=deadline)
                raise A2AClientError("A2A_CANCELLED", "A2A 任务已取消。")
            polled = self._rpc("GetTask", {"id": run.remote_task_id}, deadline=deadline)
            self._record_result(run, polled, event_type="task_polled")
            task = _task_from_event(polled) or polled
            self._update_task_identity(run, task)
            if _task_state(task) in _TERMINAL_STATES | _INTERRUPTED_STATES:
                return task
            time.sleep(min(self.poll_interval, max(deadline - time.monotonic(), 0.0)))
        self._cancel_remote(run, deadline=deadline, best_effort=True)
        raise A2AClientError("A2A_TIMEOUT", f"A2A Task 超过 {self.timeout_seconds:g} 秒未完成。")

    def _stream_method(
        self,
        run: A2ATaskRun,
        method: str,
        params: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any] | None:
        payload = self._payload(method, params)
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        if run.last_event_id:
            headers["Last-Event-ID"] = run.last_event_id
        last: dict[str, Any] | None = None
        accumulated_task: dict[str, Any] | None = None
        timeout = max(deadline - time.monotonic(), 0.1)
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", self.endpoint_url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for event_id, data in _iter_sse(response.iter_lines()):
                    if time.monotonic() >= deadline:
                        break
                    if not data:
                        continue
                    try:
                        envelope = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                        error = envelope["error"]
                        raise A2AClientError(
                            "A2A_ERROR", str(error.get("message") or "A2A 流返回错误。")
                        )
                    result = envelope.get("result") if isinstance(envelope, dict) else None
                    if not isinstance(result, dict):
                        continue
                    last = result
                    run.last_event_id = event_id or run.last_event_id
                    self._record_result(run, result, event_type="stream_event", event_id=event_id)
                    task = _task_from_event(result)
                    if task is not None:
                        accumulated_task = _merge_task(accumulated_task, task)
                        self._update_task_identity(run, task)
                        if _task_state(task) in _TERMINAL_STATES | _INTERRUPTED_STATES:
                            return accumulated_task
        return accumulated_task or last

    def _rpc(self, method: str, params: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        timeout = max(deadline - time.monotonic(), 0.1)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                self.endpoint_url,
                headers=self.headers,
                json=self._payload(method, params),
            )
            response.raise_for_status()
            envelope = response.json()
        if not isinstance(envelope, dict):
            raise A2AClientError("A2A_RESPONSE_INVALID", "A2A 响应不是 JSON 对象。")
        if isinstance(envelope.get("error"), dict):
            error = envelope["error"]
            raise A2AClientError("A2A_ERROR", str(error.get("message") or "A2A Agent 返回错误。"))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise A2AClientError("A2A_RESPONSE_INVALID", "A2A 响应缺少 result 对象。")
        return result

    def _cancel_remote(
        self, run: A2ATaskRun, *, deadline: float, best_effort: bool = False
    ) -> None:
        if not run.remote_task_id:
            return
        try:
            result = self._rpc("CancelTask", {"id": run.remote_task_id}, deadline=deadline)
            self._record_result(run, result, event_type="cancelled")
        except Exception:
            if not best_effort:
                raise

    def _finalize(self, run: A2ATaskRun, result: dict[str, Any]) -> dict[str, Any]:
        task = _task_from_event(result) or result
        state = _task_state(task) or "completed"
        artifacts = _artifacts(task)
        artifacts = self._hydrate_artifacts(artifacts)
        run.status = state
        run.result_json = task
        run.artifacts_json = artifacts
        run.finished_at = utc_now() if state in _TERMINAL_STATES | _INTERRUPTED_STATES else None
        run.updated_at = utc_now()
        self._update_task_identity(run, task, commit=False)
        self.db.add(run)
        self.db.commit()
        self._event(run, state, {"task": task, "artifacts": artifacts})
        if state in {"failed", "rejected"}:
            message = _status_message(task) or f"A2A Task {state}。"
            raise A2AClientError("A2A_TASK_FAILED", message)
        if state in {"canceled", "cancelled"}:
            raise A2AClientError("A2A_CANCELLED", "A2A Task 已取消。")
        return {
            "a2a_run_id": run.id,
            "task_id": run.remote_task_id,
            "context_id": run.context_id,
            "state": state,
            "awaiting_input": state == "input-required",
            "task": task,
            "message": _status_message(task),
            "artifacts": artifacts,
        }

    def _continuation(self, arguments: dict[str, Any]) -> dict[str, str | None]:
        task_id = str(arguments.get("taskId") or arguments.get("task_id") or "").strip() or None
        context_id = (
            str(arguments.get("contextId") or arguments.get("context_id") or "").strip() or None
        )
        if not task_id and self.session_id:
            previous = self.db.exec(
                select(A2ATaskRun)
                .where(
                    A2ATaskRun.direction == "client",
                    A2ATaskRun.tenant_id == self.tool.tenant_id,
                    A2ATaskRun.tool_id == self.tool.id,
                    A2ATaskRun.session_id == self.session_id,
                    A2ATaskRun.status.in_(["input-required", "auth-required"]),
                )
                .order_by(A2ATaskRun.updated_at.desc())
            ).first()
            if previous:
                task_id = previous.remote_task_id
                context_id = previous.context_id
        return {"task_id": task_id, "context_id": context_id}

    def _message(
        self, arguments: dict[str, Any], continuation: dict[str, str | None]
    ) -> dict[str, Any]:
        supplied = arguments.get("message")
        if isinstance(supplied, dict):
            message = dict(supplied)
        else:
            text = arguments.get("text") or arguments.get("query")
            if text is None:
                text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            message = {"role": "ROLE_USER", "parts": [{"text": str(text)}]}
        message.setdefault("messageId", uuid.uuid4().hex)
        message.setdefault("role", "ROLE_USER")
        if continuation.get("task_id"):
            message["taskId"] = continuation["task_id"]
        if continuation.get("context_id"):
            message["contextId"] = continuation["context_id"]
        return message

    def _send_params(self, message: dict[str, Any]) -> dict[str, Any]:
        modes = self.config.get("accepted_output_modes")
        if not isinstance(modes, list) or not modes:
            modes = ["text/plain", "application/json"]
        return {
            "message": message,
            "configuration": {"acceptedOutputModes": [str(item) for item in modes]},
        }

    def _payload(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}

    def _agent_card_url(self) -> str:
        configured = str(self.config.get("agent_card_url") or "").strip()
        if configured:
            return configured
        parsed = urlsplit(self.tool.url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/.well-known/agent-card.json", "", ""))

    def _record_result(
        self,
        run: A2ATaskRun,
        result: dict[str, Any],
        *,
        event_type: str,
        event_id: str | None = None,
    ) -> None:
        task = _task_from_event(result)
        if task:
            self._update_task_identity(run, task, commit=False)
            state = _task_state(task)
            if state:
                run.status = state
        run.updated_at = utc_now()
        self.db.add(run)
        self.db.commit()
        self._event(run, event_type, result, event_id=event_id)

    def _update_task_identity(
        self, run: A2ATaskRun, task: dict[str, Any], *, commit: bool = True
    ) -> None:
        run.remote_task_id = str(task.get("id") or task.get("taskId") or run.remote_task_id or "") or None
        run.context_id = str(task.get("contextId") or run.context_id or "") or None
        if commit:
            run.updated_at = utc_now()
            self.db.add(run)
            self.db.commit()

    def _event(
        self,
        run: A2ATaskRun,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> None:
        last = self.db.exec(
            select(A2ATaskEvent)
            .where(A2ATaskEvent.run_id == run.id)
            .order_by(A2ATaskEvent.sequence.desc())
        ).first()
        self.db.add(
            A2ATaskEvent(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=(last.sequence + 1) if last else 1,
                external_event_id=event_id,
                event_type=event_type,
                data_json=data,
            )
        )
        self.db.commit()

    def _hydrate_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        max_bytes = int(self.config.get("artifact_max_bytes") or 25 * 1024 * 1024)
        for artifact in artifacts:
            value = json.loads(json.dumps(artifact))
            for part in value.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                file_part = part.get("file")
                if not isinstance(file_part, dict) or file_part.get("bytes"):
                    continue
                uri = str(file_part.get("uri") or "").strip()
                if not uri or urlsplit(uri).scheme not in {"http", "https"}:
                    continue
                with httpx.Client(timeout=min(self.timeout_seconds, 60.0)) as client:
                    response = client.get(uri, headers=self.headers)
                    response.raise_for_status()
                    content = response.content
                if len(content) > max_bytes:
                    raise A2AClientError("A2A_ARTIFACT_TOO_LARGE", "A2A Artifact 超过大小限制。")
                file_part["bytes"] = base64.b64encode(content).decode("ascii")
            hydrated.append(value)
        return hydrated


def _iter_sse(lines: Iterator[str]) -> Iterator[tuple[str | None, str]]:
    event_id: str | None = None
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield event_id, "\n".join(data_lines)
            event_id = None
            data_lines = []
        elif line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event_id, "\n".join(data_lines)


def _run_lock(key: str) -> threading.RLock:
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(key, threading.RLock())


def _task_from_event(value: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(value.get("task"), dict):
        return value["task"]
    if isinstance(value.get("statusUpdate"), dict):
        update = value["statusUpdate"]
        return {
            "id": update.get("taskId"),
            "contextId": update.get("contextId"),
            "status": update.get("status"),
        }
    if isinstance(value.get("artifactUpdate"), dict):
        update = value["artifactUpdate"]
        return {
            "id": update.get("taskId"),
            "contextId": update.get("contextId"),
            "artifacts": [update.get("artifact")],
        }
    if isinstance(value.get("status"), (dict, str)) and (value.get("id") or value.get("taskId")):
        return value
    return None


def _merge_task(
    current: dict[str, Any] | None,
    update: dict[str, Any],
) -> dict[str, Any]:
    """Merge A2A status/artifact deltas without discarding prior task data."""

    merged = dict(current or {})
    for key, value in update.items():
        if key == "artifacts" and isinstance(value, list):
            existing = [item for item in merged.get("artifacts") or [] if isinstance(item, dict)]
            by_id = {
                str(item.get("artifactId") or item.get("artifact_id") or index): item
                for index, item in enumerate(existing)
            }
            for index, artifact in enumerate(value):
                if not isinstance(artifact, dict):
                    continue
                artifact_id = str(
                    artifact.get("artifactId")
                    or artifact.get("artifact_id")
                    or len(by_id) + index
                )
                by_id[artifact_id] = artifact
            merged["artifacts"] = list(by_id.values())
        elif value is not None:
            merged[key] = value
    return merged


def _task_state(task: dict[str, Any]) -> str:
    status = task.get("status")
    if isinstance(status, dict):
        status = status.get("state")
    value = str(status or task.get("state") or "").strip().lower().replace("_", "-")
    return value.removeprefix("task-state-")


def _status_message(task: dict[str, Any]) -> str | None:
    status = task.get("status")
    message = status.get("message") if isinstance(status, dict) else None
    if isinstance(message, dict):
        parts = message.get("parts") or []
        texts = [str(part.get("text")) for part in parts if isinstance(part, dict) and part.get("text")]
        return "\n".join(texts) or None
    if isinstance(message, str):
        return message
    return None


def _artifacts(task: dict[str, Any]) -> list[dict[str, Any]]:
    value = task.get("artifacts")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
