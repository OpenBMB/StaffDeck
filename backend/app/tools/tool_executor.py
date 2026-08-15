from __future__ import annotations

import base64
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.config import get_settings
from app.db.models import MCPServer, Tool
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token
from app.tools.http_request import prepare_get_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool, execute_mcp_tool_result
from app.tools.tool_schema import MCPAppDescriptor, ToolCall, ToolError, ToolResult


SECRET_PATTERN = re.compile(r"\$\{secret\.([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class ToolExecutionPolicy:
    timeout_seconds: float


class ToolExecutor:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        with self.db.no_autoflush:
            tool = self.db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
            ).first()
        if not tool:
            return self._error(tool_call.name, "NOT_FOUND", "工具不存在或未配置。")
        if not tool.enabled:
            return self._error(tool.name, "DISABLED", "工具当前未启用。")
        if agent_id and tool.id not in {
            row.id
            for row in visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)
        }:
            return self._error(tool.name, "NOT_ALLOWED", "当前员工未启用该工具。")
        if (
            active_skill_id
            and tool.allowed_skills_json
            and active_skill_id not in tool.allowed_skills_json
        ):
            return self._error(tool.name, "NOT_ALLOWED", "当前技能不允许调用该工具。")

        if (tool.tool_type or "http") == "mcp":
            return self._execute_mcp_tool(
                tool,
                tool_call.arguments,
                agent_id=agent_id,
                session_id=session_id,
                active_skill_id=active_skill_id,
                timeout_seconds_override=timeout_seconds_override,
            )
        if (tool.tool_type or "http") == "a2a":
            return self._execute_a2a_tool(
                tool,
                tool_call.arguments,
                timeout_seconds_override=timeout_seconds_override,
            )
        if (tool.tool_type or "http") != "http":
            return self._error(
                tool.name, "UNSUPPORTED_TOOL_TYPE", f"不支持的工具类型：{tool.tool_type}"
            )

        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        policy = self._execution_policy(
            tool,
            timeout_seconds_override=timeout_seconds_override,
        )
        try:
            with httpx.Client(timeout=policy.timeout_seconds) as client:
                if tool.method.upper() == "GET":
                    request_url, request_kwargs = prepare_get_request(tool.url, tool_call.arguments)
                    response = client.request(
                        tool.method.upper(), request_url, headers=headers, **request_kwargs
                    )
                else:
                    response = client.request(
                        tool.method.upper(), tool.url, headers=headers, json=tool_call.arguments
                    )
                response.raise_for_status()
                return ToolResult(
                    tool_name=tool.name,
                    success=True,
                    data=self._response_data(response),
                    error=None,
                )
        except httpx.TimeoutException:
            return self._error(
                tool.name,
                "TIMEOUT",
                f"工具调用超过 {policy.timeout_seconds:g} 秒未返回。",
            )
        except httpx.HTTPStatusError as exc:
            return self._error(
                tool.name,
                "HTTP_ERROR",
                f"工具返回异常状态码：{exc.response.status_code}",
            )
        except Exception as exc:
            return self._error(tool.name, "EXECUTION_ERROR", str(exc))

    def _execute_a2a_tool(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        """Invoke an A2A agent through the JSON-RPC 2.0 SendMessage method."""

        policy = self._execution_policy(tool, timeout_seconds_override=timeout_seconds_override)
        config = tool.config_json if isinstance(tool.config_json, dict) else {}
        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        headers.setdefault("Content-Type", "application/json")
        a2a_version = str(config.get("a2a_version") or "1.0").strip()
        if a2a_version:
            headers.setdefault("A2A-Version", a2a_version)
        message = arguments.get("message")
        if not isinstance(message, dict):
            text_value = arguments.get("text") or arguments.get("query")
            part = (
                {"text": str(text_value)}
                if text_value is not None
                else {"text": self._json_text(arguments)}
            )
            message = {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [part],
            }
        else:
            message = dict(message)
            message.setdefault("messageId", uuid.uuid4().hex)
            message.setdefault("role", "ROLE_USER")
        output_modes = config.get("accepted_output_modes")
        if not isinstance(output_modes, list) or not output_modes:
            output_modes = ["text/plain", "application/json"]
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "SendMessage",
            "params": {
                "message": message,
                "configuration": {
                    "acceptedOutputModes": [str(item) for item in output_modes],
                },
            },
        }
        try:
            with httpx.Client(timeout=policy.timeout_seconds) as client:
                response = client.post(tool.url, headers=headers, json=payload)
                response.raise_for_status()
            data = self._response_data(response)
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                error = data["error"]
                return self._error(
                    tool.name,
                    "A2A_ERROR",
                    str(error.get("message") or "A2A Agent 返回错误。"),
                )
            result = data.get("result") if isinstance(data, dict) and "result" in data else data
            return ToolResult(tool_name=tool.name, success=True, data=result, error=None)
        except httpx.TimeoutException:
            return self._error(
                tool.name,
                "TIMEOUT",
                f"A2A 调用超过 {policy.timeout_seconds:g} 秒未返回。",
            )
        except httpx.HTTPStatusError as exc:
            return self._error(
                tool.name,
                "A2A_HTTP_ERROR",
                f"A2A Agent 返回异常状态码：{exc.response.status_code}",
            )
        except Exception as exc:
            return self._error(tool.name, "A2A_EXECUTION_ERROR", str(exc))

    @staticmethod
    def _json_text(value: dict[str, Any]) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _execute_mcp_tool(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        active_skill_id: str | None = None,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        try:
            config, tool_name = self._resolve_mcp_config(tool)
            policy = self._execution_policy(
                tool,
                timeout_seconds_override=timeout_seconds_override,
            )
            if config.get("apps_mode") == "auto":
                envelope = execute_mcp_tool_result(
                    config,
                    arguments,
                    timeout_seconds=policy.timeout_seconds,
                    tool_name=tool_name,
                )
            else:
                envelope = {
                    "data": execute_mcp_tool(
                        config,
                        arguments,
                        timeout_seconds=policy.timeout_seconds,
                        tool_name=tool_name,
                    ),
                    "meta": {},
                }
            app_config = (tool.config_json or {}).get("mcp_apps")
            app_descriptor: MCPAppDescriptor | None = None
            if isinstance(app_config, dict) and config.get("apps_mode") == "auto":
                resource_uri = str(app_config.get("resource_uri") or "").strip()
                visibility = app_config.get("visibility")
                if not isinstance(visibility, list):
                    visibility = ["model", "app"]
                if resource_uri and "app" in visibility:
                    app_descriptor = MCPAppDescriptor(
                        server_id=str(tool.mcp_server_id),
                        resource_uri=resource_uri,
                        tool_name=tool.name,
                        visibility=[str(value) for value in visibility],
                        tenant_id=tool.tenant_id,
                        agent_id=agent_id,
                        session_id=session_id,
                        active_skill_id=active_skill_id,
                        initial_result=envelope.get("data"),
                        initial_meta=(
                            envelope.get("meta")
                            if isinstance(envelope.get("meta"), dict)
                            else {}
                        ),
                    )
            return ToolResult(
                tool_name=tool.name,
                success=True,
                data=envelope.get("data"),
                error=None,
                mcp_app=app_descriptor,
                mcp_metadata=(
                    envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
                ),
            )
        except MCPClientError as exc:
            return self._error(tool.name, "MCP_ERROR", str(exc))
        except Exception as exc:
            return self._error(tool.name, "MCP_EXECUTION_ERROR", str(exc))

    def _execution_policy(
        self,
        tool: Tool,
        *,
        timeout_seconds_override: float | None = None,
    ) -> ToolExecutionPolicy:
        execution = (tool.config_json or {}).get("execution")
        raw_timeout = execution.get("timeout_seconds") if isinstance(execution, dict) else None
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            timeout_seconds = self.settings.tool_timeout_seconds
        if not 1 <= timeout_seconds <= 300:
            timeout_seconds = self.settings.tool_timeout_seconds
        if timeout_seconds_override is not None:
            timeout_seconds = min(timeout_seconds, max(float(timeout_seconds_override), 0.1))
        return ToolExecutionPolicy(timeout_seconds=timeout_seconds)

    def _resolve_mcp_config(self, tool: Tool) -> tuple[dict[str, Any], str | None]:
        """Resolve an MCP tool through its persisted MCP server relation."""
        tool_config = tool.config_json or {}
        tool_name = (
            str(tool_config.get("tool") or tool_config.get("tool_name") or "").strip() or None
        )
        if not tool.mcp_server_id:
            raise MCPClientError("MCP 工具未关联 Server。")
        server = self.db.get(MCPServer, tool.mcp_server_id)
        if server is None or server.tenant_id != tool.tenant_id:
            raise MCPClientError("MCP 工具关联的 Server 不存在或已删除。")
        if not server.enabled:
            raise MCPClientError("MCP 工具关联的 Server 当前已停用。")
        return self._server_client_config(server), tool_name

    def _server_client_config(self, server: MCPServer) -> dict[str, Any]:
        transport = server.transport or "streamable_http"
        config: dict[str, Any] = {"transport": transport}
        if transport in {"streamable_http", "sse"}:
            config["url"] = server.url or ""
            if server.headers_json:
                config["headers"] = dict(server.headers_json)
        elif transport == "stdio":
            config["command"] = server.command or ""
            config["args"] = list(server.args_json or [])
            if server.env_json:
                config["env"] = dict(server.env_json)
            if server.cwd:
                config["cwd"] = server.cwd
        elif transport == "builtin":
            config["server"] = "builtin.demo"
        config["apps_mode"] = server.apps_mode or "disabled"
        return config

    def _response_data(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text

    def _resolve_headers(self, headers: dict[str, Any], auth: dict[str, Any]) -> dict[str, str]:
        resolved = {key: self._resolve_secret(str(value)) for key, value in headers.items()}
        auth_type = str(auth.get("type") or "").strip().lower()
        if auth_type == "bearer" and auth.get("token"):
            resolved["Authorization"] = f"Bearer {self._resolve_secret(str(auth['token']))}"
        elif auth_type == "basic" and "Authorization" not in resolved:
            basic = auth.get("basic")
            if (
                isinstance(basic, dict)
                and basic.get("username") is not None
                and basic.get("password") is not None
            ):
                username = self._resolve_secret(str(basic["username"]))
                password = self._resolve_secret(str(basic["password"]))
                credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
                resolved["Authorization"] = f"Basic {credentials}"
        elif auth_type not in {"bearer", "basic"}:
            # Auth JSON is also allowed as a literal header map for integrations
            # that use custom schemes (for example X-API-Key or a vendor token).
            for key, value in auth.items():
                if key == "type" or value is None:
                    continue
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                resolved[str(key)] = self._resolve_secret(str(value))
        return resolved

    def _request_headers(
        self,
        url: str,
        headers: dict[str, str],
        *,
        normalized_tool_base_url: str | None = None,
    ) -> dict[str, str]:
        if not self._is_internal_mock_url(url, normalized_tool_base_url=normalized_tool_base_url):
            return headers
        resolved = dict(headers)
        resolved[INTERNAL_SERVICE_HEADER] = internal_service_token()
        return resolved

    def _is_internal_mock_url(
        self,
        url: str,
        *,
        normalized_tool_base_url: str | None = None,
    ) -> bool:
        target = urlsplit(url)
        if not target.path.startswith("/api/mock/"):
            return False
        if not target.scheme and not target.netloc:
            return True
        configured = urlsplit(normalized_tool_base_url or self.settings.normalized_tool_base_url)
        return (
            target.scheme.lower(),
            target.hostname,
            target.port or _default_port(target.scheme),
        ) == (
            configured.scheme.lower(),
            configured.hostname,
            configured.port or _default_port(configured.scheme),
        )

    def _resolve_secret(self, value: str) -> str:
        def repl(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")

        return SECRET_PATTERN.sub(repl, value)

    def _error(self, tool_name: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            success=False,
            data=None,
            error=ToolError(code=code, message=message),
        )


def _default_port(scheme: str) -> int | None:
    return 443 if scheme.lower() == "https" else 80 if scheme.lower() == "http" else None
