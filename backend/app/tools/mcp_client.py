from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

import httpx

from app.security.managed_subprocess import ManagedProcess, ManagedProcessError
from app.tools.mcp_builtin import (
    BuiltinMCPError,
    builtin_mcp_tool_definitions,
    execute_builtin_mcp,
)


class MCPClientError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Transport 归一化
# --------------------------------------------------------------------------- #

def normalize_transport(config: dict[str, Any]) -> str:
    """从连接配置推断 transport 类型。

    优先使用显式 transport 字段；否则根据 server/command/url 推断，
    以兼容历史配置。streamable_http 归一化为 http。
    """
    raw = str(config.get("transport") or "").strip().lower()
    if raw == "streamable_http":
        return "http"
    if raw:
        return raw
    server = str(config.get("server") or config.get("server_id") or "").strip()
    if server == "builtin.demo":
        return "builtin"
    if config.get("command"):
        return "stdio"
    if config.get("url") or config.get("endpoint"):
        return "http"
    return "builtin"


# --------------------------------------------------------------------------- #
# 对外入口：调用工具 / 列举工具
# --------------------------------------------------------------------------- #

def execute_mcp_tool(
    config: dict[str, Any],
    arguments: dict[str, Any],
    timeout_seconds: float = 10,
    tool_name: str | None = None,
) -> Any:
    """连接 MCP server 并调用单个工具。

    config 是「server 连接配置」（transport/url/command/headers 等）。
    tool_name 若显式传入则优先使用，否则回退到 config 里的 tool 字段
    （兼容历史「一个 config 一个 tool」的形态）。
    """
    normalized = dict(config or {})
    transport = normalize_transport(normalized)
    name = _resolve_tool_name(normalized, tool_name)

    if transport == "builtin":
        try:
            return execute_builtin_mcp({**normalized, "tool": name}, arguments)
        except BuiltinMCPError as exc:
            raise MCPClientError(str(exc)) from exc
    if transport == "stdio":
        return _StdioSession(normalized, timeout_seconds).call_tool(name, arguments)
    if transport in {"http", "streamable_http"}:
        return _HttpSession(normalized, timeout_seconds).call_tool(name, arguments)
    if transport == "sse":
        return _SseSession(normalized, timeout_seconds).call_tool(name, arguments)
    raise MCPClientError(f"不支持的 MCP transport：{transport or '<empty>'}")


def list_mcp_tools(
    config: dict[str, Any],
    timeout_seconds: float = 10,
) -> list[dict[str, Any]]:
    """连接 MCP server 并通过 tools/list 发现工具列表。

    返回标准化后的工具定义列表，每项包含 name / description /
    input_schema / output_schema（若 server 提供）。
    """
    normalized = dict(config or {})
    transport = normalize_transport(normalized)

    if transport == "builtin":
        try:
            raw = builtin_mcp_tool_definitions(normalized)
        except BuiltinMCPError as exc:
            raise MCPClientError(str(exc)) from exc
    elif transport == "stdio":
        raw = _StdioSession(normalized, timeout_seconds).list_tools()
    elif transport in {"http", "streamable_http"}:
        raw = _HttpSession(normalized, timeout_seconds).list_tools()
    elif transport == "sse":
        raw = _SseSession(normalized, timeout_seconds).list_tools()
    else:
        raise MCPClientError(f"不支持的 MCP transport：{transport or '<empty>'}")

    return [_normalize_tool_definition(item) for item in raw if isinstance(item, dict)]


def _resolve_tool_name(config: dict[str, Any], override: str | None) -> str:
    name = str(override or config.get("tool") or config.get("tool_name") or config.get("name") or "").strip()
    if not name:
        raise MCPClientError("MCP 调用缺少 tool 名称。")
    return name


def _normalize_tool_definition(item: dict[str, Any]) -> dict[str, Any]:
    input_schema = item.get("inputSchema") or item.get("input_schema") or {}
    output_schema = item.get("outputSchema") or item.get("output_schema") or {}
    return {
        "name": str(item.get("name") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "input_schema": input_schema if isinstance(input_schema, dict) else {},
        "output_schema": output_schema if isinstance(output_schema, dict) else {},
    }


# --------------------------------------------------------------------------- #
# JSON-RPC 会话基类
# --------------------------------------------------------------------------- #

class _MCPSession:
    """封装一次 MCP 连接的 initialize + list/call 交互。

    子类实现 `_request`（单次 JSON-RPC 请求/响应）和资源管理。
    """

    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        with self:
            self._initialize()
            result = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
            return _extract_tool_result(result)

    def list_tools(self) -> list[dict[str, Any]]:
        with self:
            self._initialize()
            result = self._request("tools/list", {})
            tools = result.get("tools") if isinstance(result, dict) else None
            return tools if isinstance(tools, list) else []

    def _initialize(self) -> None:
        self._request("initialize", _initialize_params())
        self._notify("notifications/initialized", {})

    # 子类实现 ---------------------------------------------------------------
    def __enter__(self) -> "_MCPSession":
        return self

    def __exit__(self, *exc: Any) -> None:  # pragma: no cover - default no-op
        return None

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #

class _PipeReader:
    """Read a text pipe without relying on select(), which rejects pipes on Windows."""

    def __init__(self, stream: TextIO, max_line_size: int = 4 * 1024 * 1024) -> None:
        self._stream = stream
        self._max_line_size = max_line_size
        self._events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=128)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mcp-stdio-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                line = self._stream.readline(self._max_line_size + 1)
                if not line:
                    break
                if len(line) > self._max_line_size:
                    self._put(
                        (
                            "error",
                            MCPClientError(
                                f"MCP stdio 单条响应超过 {self._max_line_size} 字符限制。"
                            ),
                        )
                    )
                    return
                self._put(("line", line))
        except (OSError, ValueError) as exc:
            if not self._stopped.is_set():
                self._put(("error", exc))
        finally:
            self._put(("eof", None))

    def _put(self, event: tuple[str, object]) -> None:
        while not self._stopped.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def next_event(self, timeout: float) -> tuple[str, object]:
        try:
            return self._events.get(timeout=max(timeout, 0))
        except queue.Empty as exc:
            raise TimeoutError from exc

    def close(self) -> None:
        self._stopped.set()
        with suppress(OSError, ValueError):
            self._stream.close()
        self._thread.join(timeout=0.2)


class _StderrCollector:
    def __init__(self, stream: TextIO, limit: int = 1000) -> None:
        self._stream = stream
        self._limit = limit
        self._parts: list[str] = []
        self._length = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="mcp-stderr-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while chunk := self._stream.readline(256):
                with self._lock:
                    if self._length < self._limit:
                        kept = chunk[: self._limit - self._length]
                        self._parts.append(kept)
                        self._length += len(kept)
        except (OSError, ValueError):
            return

    def text(self) -> str:
        with self._lock:
            value = "".join(self._parts).strip()
        return f" stderr: {value}" if value else ""

    def wait(self, timeout: float = 0.1) -> None:
        self._thread.join(timeout)

    def close(self) -> None:
        with suppress(OSError, ValueError):
            self._stream.close()
        self._thread.join(timeout=0.2)


class _StdioSession(_MCPSession):
    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        super().__init__(config, timeout_seconds)
        self._proc: subprocess.Popen[str] | None = None
        self._managed_process: ManagedProcess | None = None
        self._stdout_reader: _PipeReader | None = None
        self._stderr_collector: _StderrCollector | None = None
        self._next_id = 0

    def __enter__(self) -> "_StdioSession":
        command = _stdio_command(self.config)
        env = os.environ.copy()
        raw_env = self.config.get("env")
        if isinstance(raw_env, Mapping):
            env.update({str(key): str(value) for key, value in raw_env.items()})
        cwd = str(self.config["cwd"]) if self.config.get("cwd") else None
        _validate_stdio_launch(command, cwd)
        try:
            self._managed_process = ManagedProcess.start(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
            self._proc = self._managed_process.process
        except FileNotFoundError as exc:
            if cwd and not os.path.isdir(cwd):
                raise MCPClientError(f"MCP stdio 工作目录不存在：{cwd}") from exc
            raise MCPClientError(
                f"无法启动 MCP stdio：找不到命令 {command[0]!r}，请确认它已安装并在 PATH 中。"
            ) from exc
        except PermissionError as exc:
            raise MCPClientError(f"无法启动 MCP stdio：没有权限执行 {command[0]!r}。") from exc
        except ManagedProcessError as exc:
            raise MCPClientError(f"MCP stdio 受控进程启动失败：{exc}") from exc
        except OSError as exc:
            raise MCPClientError(f"无法启动 MCP stdio 命令 {command[0]!r}：{exc}") from exc
        if self._proc.stdout is None or self._proc.stderr is None:
            self._managed_process.close()
            self._managed_process = None
            self._proc = None
            raise MCPClientError("MCP stdio 进程管道创建失败。")
        self._stdout_reader = _PipeReader(self._proc.stdout)
        self._stderr_collector = _StderrCollector(self._proc.stderr)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._managed_process is not None:
            self._managed_process.close()
            self._managed_process = None
        self._proc = None
        if self._stdout_reader is not None:
            self._stdout_reader.close()
        if self._stderr_collector is not None:
            self._stderr_collector.close()
        self._stdout_reader = None
        self._stderr_collector = None

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        proc = self._require_proc()
        self._next_id += 1
        request_id = self._next_id
        timeout = max(self.timeout_seconds, 0.1)
        deadline = time.monotonic() + timeout
        _send_json(
            proc,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout_seconds=timeout,
        )
        response = _read_response(
            proc,
            self._require_stdout_reader(),
            expected_id=request_id,
            timeout_seconds=max(deadline - time.monotonic(), 0),
            stderr=self._stderr_collector,
        )
        _raise_json_rpc_error(response)
        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        proc = self._require_proc()
        _send_json(
            proc,
            {"jsonrpc": "2.0", "method": method, "params": params},
            timeout_seconds=max(self.timeout_seconds, 0.1),
        )

    def _require_proc(self) -> subprocess.Popen[str]:
        if self._proc is None:
            raise MCPClientError("MCP stdio 会话未启动。")
        return self._proc

    def _require_stdout_reader(self) -> _PipeReader:
        if self._stdout_reader is None:
            raise MCPClientError("MCP stdio stdout 不可用。")
        return self._stdout_reader


def _stdio_command(config: dict[str, Any]) -> list[str]:
    command = config.get("command")
    args = config.get("args") or []
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str) and command.strip():
        parts = [command.strip()]
    else:
        raise MCPClientError("stdio MCP 连接缺少 command。")
    if not isinstance(args, list):
        raise MCPClientError("stdio MCP 连接的 args 必须是数组。")
    return [*parts, *[str(arg) for arg in args]]


def _validate_stdio_launch(command: list[str], cwd: str | None) -> None:
    workdir = Path(cwd).expanduser() if cwd else Path.cwd()
    if cwd and not workdir.is_dir():
        raise MCPClientError(f"MCP stdio 工作目录不存在：{workdir}")

    executable = Path(command[0]).name.lower()
    if executable not in {"node", "node.exe", "bun", "bun.exe", "deno", "deno.exe"}:
        return
    entrypoint = next((arg for arg in command[1:] if not arg.startswith("-")), "")
    if not entrypoint or Path(entrypoint).suffix.lower() not in {".js", ".cjs", ".mjs", ".ts"}:
        return
    entrypoint_path = Path(entrypoint).expanduser()
    resolved = entrypoint_path if entrypoint_path.is_absolute() else workdir / entrypoint_path
    if not resolved.is_file():
        raise MCPClientError(
            f"MCP stdio 入口文件不存在：{resolved}。请检查 Args，或将 cwd 设置为入口文件所在目录。"
        )


# --------------------------------------------------------------------------- #
# HTTP (streamable_http) transport
# --------------------------------------------------------------------------- #

class _HttpSession(_MCPSession):
    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        super().__init__(config, timeout_seconds)
        self._client: httpx.Client | None = None
        self._next_id = 0
        self._session_id: str | None = None

    def __enter__(self) -> "_HttpSession":
        self._client = httpx.Client(timeout=self.timeout_seconds)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._client is not None:
            with suppress(Exception):
                self._client.close()
            self._client = None

    def _endpoint(self) -> str:
        url = str(self.config.get("url") or self.config.get("endpoint") or "").strip()
        if not url:
            raise MCPClientError("HTTP MCP 连接缺少 url/endpoint。")
        return url

    def _headers(self) -> dict[str, str]:
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **{str(k): str(v) for k, v in raw.items()},
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        client = self._require_client()
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            response = client.post(self._endpoint(), headers=self._headers(), json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MCPClientError(f"HTTP MCP 返回异常状态码：{exc.response.status_code}") from exc
        except Exception as exc:
            raise MCPClientError(str(exc)) from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        body = _parse_http_mcp_response(response)
        if not isinstance(body, dict):
            raise MCPClientError("HTTP MCP 返回内容不是 JSON-RPC object。")
        _raise_json_rpc_error(body)
        return body.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        client = self._require_client()
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with suppress(Exception):
            client.post(self._endpoint(), headers=self._headers(), json=payload)

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise MCPClientError("HTTP MCP 会话未启动。")
        return self._client


def _parse_http_mcp_response(response: httpx.Response) -> Any:
    """解析 HTTP MCP 响应，兼容纯 JSON 和 SSE 格式（text/event-stream）。"""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        payload = _last_sse_json(response.text)
        if payload is None:
            raise MCPClientError("SSE 响应中未找到有效的 JSON-RPC data 行。")
        return payload
    try:
        return response.json()
    except Exception as exc:
        raise MCPClientError(f"HTTP MCP 响应解析失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# SSE transport
# --------------------------------------------------------------------------- #

class _SseSession(_MCPSession):
    """SSE transport（MCP 2024-11-05 HTTP+SSE）。

    连接流程：GET server url 建立 SSE 流，从首个 `event: endpoint`
    拿到用于发送 JSON-RPC 的消息端点；后续请求 POST 到该端点，
    响应通过 SSE 流按 id 匹配返回。
    """

    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        super().__init__(config, timeout_seconds)
        self._client: httpx.Client | None = None
        self._stream_ctx: Any = None
        self._events: Any = None
        self._message_url: str | None = None
        self._next_id = 0

    def __enter__(self) -> "_SseSession":
        self._client = httpx.Client(timeout=httpx.Timeout(self.timeout_seconds, read=None))
        url = str(self.config.get("url") or self.config.get("endpoint") or "").strip()
        if not url:
            raise MCPClientError("SSE MCP 连接缺少 url/endpoint。")
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        headers = {"Accept": "text/event-stream", **{str(k): str(v) for k, v in raw.items()}}
        self._stream_ctx = self._client.stream("GET", url, headers=headers)
        response = self._stream_ctx.__enter__()
        response.raise_for_status()
        self._events = _iter_sse_events(response)
        self._message_url = self._await_endpoint(url)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._stream_ctx is not None:
            with suppress(Exception):
                self._stream_ctx.__exit__(*exc)
            self._stream_ctx = None
        if self._client is not None:
            with suppress(Exception):
                self._client.close()
            self._client = None

    def _await_endpoint(self, base_url: str) -> str:
        deadline = time.monotonic() + max(self.timeout_seconds, 0.1)
        for event, data in self._events:
            if event == "endpoint":
                return _resolve_endpoint(base_url, data.strip())
            if time.monotonic() > deadline:
                break
        raise MCPClientError("SSE MCP 未返回 endpoint 事件。")

    def _post_headers(self) -> dict[str, str]:
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        return {"Content-Type": "application/json", **{str(k): str(v) for k, v in raw.items()}}

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        client = self._require_client()
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            posted = client.post(str(self._message_url), headers=self._post_headers(), json=payload)
            posted.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MCPClientError(f"SSE MCP 返回异常状态码：{exc.response.status_code}") from exc
        except Exception as exc:
            raise MCPClientError(str(exc)) from exc
        body = self._await_response(request_id)
        _raise_json_rpc_error(body)
        return body.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        client = self._require_client()
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with suppress(Exception):
            client.post(str(self._message_url), headers=self._post_headers(), json=payload)

    def _await_response(self, expected_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + max(self.timeout_seconds, 0.1)
        for event, data in self._events:
            if event in {"message", ""}:
                with suppress(json.JSONDecodeError):
                    payload = json.loads(data)
                    if isinstance(payload, dict) and payload.get("id") == expected_id:
                        return payload
            if time.monotonic() > deadline:
                break
        raise MCPClientError(f"SSE MCP 等待响应超时：id={expected_id}")

    def _require_client(self) -> httpx.Client:
        if self._client is None or self._message_url is None:
            raise MCPClientError("SSE MCP 会话未启动。")
        return self._client


def _iter_sse_events(response: httpx.Response):
    """迭代 SSE 流，逐个 yield (event_type, data)。"""
    event_type = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_type or "message", "\n".join(data_lines)
            event_type = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())


def _resolve_endpoint(base_url: str, endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    from urllib.parse import urljoin

    return urljoin(base_url, endpoint)


def _last_sse_json(text: str) -> Any:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
            with suppress(json.JSONDecodeError):
                return json.loads(data)
    return None


# --------------------------------------------------------------------------- #
# 共享工具函数
# --------------------------------------------------------------------------- #

def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "StaffDeck", "version": "0.1.0"},
    }


def _send_json(
    proc: subprocess.Popen[str],
    payload: dict[str, Any],
    timeout_seconds: float | None = None,
) -> None:
    if proc.stdin is None:
        raise MCPClientError("MCP stdio stdin 不可用。")
    outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

    def write() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            outcome.put(None)
        except (BrokenPipeError, OSError, UnicodeError, ValueError) as exc:
            outcome.put(exc)

    writer = threading.Thread(target=write, name="mcp-stdin-writer", daemon=True)
    writer.start()
    try:
        error = outcome.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise MCPClientError("向 MCP stdio server 发送请求超时。") from exc
    if error is not None:
        exit_code = proc.poll()
        suffix = f"（退出码 {exit_code}）" if exit_code is not None else ""
        raise MCPClientError(f"无法向 MCP stdio server 发送请求{suffix}：{error}") from error


def _read_response(
    proc: subprocess.Popen[str],
    reader: _PipeReader,
    expected_id: int,
    timeout_seconds: float,
    stderr: _StderrCollector | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPClientError(f"MCP stdio 等待响应超时：id={expected_id}{_stderr_text(stderr)}")
        try:
            event, value = reader.next_event(remaining)
        except TimeoutError as exc:
            raise MCPClientError(
                f"MCP stdio 等待响应超时：id={expected_id}{_stderr_text(stderr)}"
            ) from exc
        if event == "error":
            raise MCPClientError(f"读取 MCP stdio 响应失败：{value}{_stderr_text(stderr)}")
        if event == "eof":
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=0.1)
            if stderr is not None:
                stderr.wait()
            exit_code = proc.poll()
            suffix = f"（退出码 {exit_code}）" if exit_code is not None else "（stdout 已关闭）"
            raise MCPClientError(f"MCP stdio server 在返回响应前退出{suffix}。{_stderr_text(stderr)}".strip())
        line = str(value)
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id") == expected_id:
            return payload


def _raise_json_rpc_error(payload: dict[str, Any]) -> None:
    if "error" not in payload:
        return
    error = payload.get("error") or {}
    if isinstance(error, dict):
        message = str(error.get("message") or error)
    else:
        message = str(error)
    raise MCPClientError(message)


def _extract_tool_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise MCPClientError(_content_text(result.get("content")) or "MCP tool returned isError=true。")
    content = result.get("content")
    if not isinstance(content, list):
        return result
    extracted: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            extracted.append(item)
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            extracted.append(_parse_text_content(text))
        else:
            extracted.append(item)
    if len(extracted) == 1:
        return extracted[0]
    return extracted


def _parse_text_content(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return ""
    with suppress(json.JSONDecodeError):
        return json.loads(stripped)
    return text


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _stderr_text(collector: _StderrCollector | None) -> str:
    return collector.text() if collector is not None else ""
