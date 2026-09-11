from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.config import get_settings
from app.db.models import ChatSession, ExternalBusinessTask, MCPServer, Tool, utc_now
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token
from app.skills.tool_authorization import SopToolAuthorization
from app.tools.a2a_client import A2AClient, A2AClientError
from app.tools.external_tasks import callback_token_hash, new_callback_token
from app.tools.http_request import prepare_get_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool, execute_mcp_tool_result
from app.tools.tool_schema import MCPAppDescriptor, ToolCall, ToolError, ToolResult

SECRET_PATTERN = re.compile(r"\$\{secret\.([A-Z0-9_]+)\}")


def _json_path(value: Any, path: str) -> Any:
    current = value
    for part in (item for item in path.split(".") if item):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


@dataclass(frozen=True)
class ToolExecutionPolicy:
    timeout_seconds: float


@dataclass(frozen=True)
class HttpExecutionResponse:
    result: ToolResult
    status_code: int | None = None


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
        invocation_id: str | None = None,
        task_frame_id: str | None = None,
        resume_step_id: str | None = None,
        timeout_seconds_override: float | None = None,
        user_id: str | None = None,
        *,
        sop_authorization: SopToolAuthorization | None = None,
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
        if sop_authorization is not None:
            if (
                sop_authorization.tenant_id != tenant_id
                or sop_authorization.parent_skill_id != active_skill_id
            ):
                return self._error(tool.name, "NOT_ALLOWED", "SOP 授权上下文不匹配。")
            if not sop_authorization.permits_skill(tool):
                return self._error(tool.name, "NOT_ALLOWED", "当前技能不允许调用该工具。")
            if (
                str(tool.capability_scope).replace("-", "_") == "sop_specific"
                and not sop_authorization.explicitly_allows(tool)
            ):
                return self._error(tool.name, "NOT_ALLOWED", "当前 SOP 节点未授权该工具。")
        elif (
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
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
                timeout_seconds_override=timeout_seconds_override,
            )
        if (tool.tool_type or "http") != "http":
            return self._error(
                tool.name, "UNSUPPORTED_TOOL_TYPE", f"不支持的工具类型：{tool.tool_type}"
            )

        execution = (
            tool.config_json.get("execution", {}) if isinstance(tool.config_json, dict) else {}
        )
        if execution.get("execution_mode") == "detached":
            if not user_id and session_id:
                session = self.db.get(ChatSession, session_id)
                if session and session.tenant_id == tenant_id:
                    user_id = session.user_id
            return self._execute_detached_http(
                tool,
                tool_call.arguments,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
                task_frame_id=task_frame_id,
                resume_step_id=resume_step_id,
                timeout_seconds_override=timeout_seconds_override,
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

    def execute_sync_http(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        """Execute the HTTP request directly for the detached worker."""
        return self.execute_http_with_metadata(
            tool,
            arguments,
            timeout_seconds_override=timeout_seconds_override,
        ).result

    def execute_http_with_metadata(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        timeout_seconds_override: float | None = None,
        additional_headers: dict[str, str] | None = None,
    ) -> HttpExecutionResponse:
        """Execute HTTP while retaining the response status for async protocols."""
        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        headers.update(additional_headers or {})
        policy = self._execution_policy(
            tool,
            timeout_seconds_override=timeout_seconds_override,
        )
        try:
            with httpx.Client(timeout=policy.timeout_seconds) as client:
                if tool.method.upper() == "GET":
                    request_url, request_kwargs = prepare_get_request(tool.url, arguments)
                    response = client.request(
                        tool.method.upper(), request_url, headers=headers, **request_kwargs
                    )
                else:
                    response = client.request(
                        tool.method.upper(), tool.url, headers=headers, json=arguments
                    )
                response.raise_for_status()
                return HttpExecutionResponse(
                    result=ToolResult(
                        tool_name=tool.name,
                        success=True,
                        data=self._response_data(response),
                        error=None,
                    ),
                    status_code=response.status_code,
                )
        except httpx.TimeoutException:
            return HttpExecutionResponse(
                result=self._error(
                    tool.name,
                    "TIMEOUT",
                    f"工具调用超过 {policy.timeout_seconds:g} 秒未返回。",
                )
            )
        except httpx.HTTPStatusError as exc:
            return HttpExecutionResponse(
                result=self._error(
                    tool.name,
                    "HTTP_ERROR",
                    f"工具返回异常状态码：{exc.response.status_code}",
                ),
                status_code=exc.response.status_code,
            )
        except Exception as exc:
            return HttpExecutionResponse(
                result=self._error(tool.name, "EXECUTION_ERROR", str(exc))
            )

    def _execute_detached_http(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        user_id: str | None,
        agent_id: str | None,
        session_id: str | None,
        invocation_id: str | None,
        task_frame_id: str | None,
        resume_step_id: str | None,
        timeout_seconds_override: float | None,
    ) -> ToolResult:
        if not user_id:
            return self._error(
                tool.name,
                "USER_CONTEXT_REQUIRED",
                "Detached tools require an authenticated user or a user-owned session.",
            )
        execution = tool.config_json.get("execution", {})
        if not isinstance(execution, dict):
            execution = {}
        async_strategy = str(execution.get("async_strategy") or "staffdeck_worker")
        idempotency_key = (
            f"staffdeck:{tool.tenant_id}:{tool.id}:{invocation_id}"
            if invocation_id
            else None
        )
        if idempotency_key:
            existing = self.db.exec(
                select(ExternalBusinessTask).where(
                    ExternalBusinessTask.idempotency_key == idempotency_key
                )
            ).first()
            if existing is not None:
                return self._detached_acceptance_result(tool, existing)
        if session_id and task_frame_id:
            existing = self.db.exec(select(ExternalBusinessTask).where(
                ExternalBusinessTask.tenant_id == tool.tenant_id,
                ExternalBusinessTask.user_id == user_id,
                ExternalBusinessTask.session_id == session_id,
                ExternalBusinessTask.task_frame_id == task_frame_id,
                ExternalBusinessTask.tool_id == tool.id,
                ExternalBusinessTask.status.in_([
                    "queued", "submitting", "accepted", "working", "outcome_unknown",
                ]),
            ).order_by(ExternalBusinessTask.created_at.desc())).first()
            if existing is not None:
                if dict(existing.request_json or {}) != arguments:
                    return self._error(tool.name, "EXTERNAL_TASK_ALREADY_PENDING",
                                       f"当前 TaskFrame 已有任务 {existing.id}；请先用 external_task_status 查询，不能重复提交。")
                return self._detached_acceptance_result(tool, existing)
        callback_token = new_callback_token()
        status_url = str(execution.get("status_url") or "").strip() or None
        poll_interval_seconds = max(
            1.0, float(execution.get("poll_interval_seconds") or 5)
        )
        max_tracking_seconds = max(
            1, int(execution.get("max_tracking_seconds") or 86400)
        )
        task = ExternalBusinessTask(
            tenant_id=tool.tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation_id,
            task_frame_id=task_frame_id,
            resume_step_id=resume_step_id,
            idempotency_key=idempotency_key,
            tool_id=tool.id,
            request_json=arguments,
            callback_token_hash=callback_token_hash(callback_token),
            status="queued",
            status_url=status_url,
            status_config_json={
                "async_strategy": async_strategy,
                "task_id_field": str(execution.get("task_id_field") or "taskId"),
                "status_field": str(execution.get("status_field") or "status"),
                "result_field": str(execution.get("result_field") or "result"),
                "status_mapping": dict(execution.get("status_mapping") or {}),
            },
            poll_interval_seconds=poll_interval_seconds,
            next_poll_at=None,
            expires_at=utc_now() + timedelta(seconds=max_tracking_seconds),
        )
        self.db.add(task)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            if idempotency_key:
                existing = self.db.exec(select(ExternalBusinessTask).where(
                    ExternalBusinessTask.idempotency_key == idempotency_key,
                )).first()
                if existing is not None:
                    return self._detached_acceptance_result(tool, existing)
            raise
        if not task.idempotency_key:
            task.idempotency_key = f"staffdeck:{tool.tenant_id}:{tool.id}:{task.id}"
        task.accepted_at = utc_now()
        task.updated_at = task.accepted_at
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return self._detached_acceptance_result(tool, task)

    @staticmethod
    def _detached_acceptance_result(
        tool: Tool,
        task: ExternalBusinessTask,
    ) -> ToolResult:
        state_text = {
            "queued": "已进入后台队列，尚未提交到外部接口",
            "submitting": "正在提交到外部接口",
            "accepted": "外部接口已受理",
            "working": "正在处理",
            "completed": "已完成",
            "failed": "执行失败",
            "cancelled": "已取消",
            "expired": "已超时",
            "outcome_unknown": "提交结果不确定，请核对外部系统，勿重复提交",
        }.get(task.status, task.status)
        return ToolResult(
            tool_name=tool.name,
            success=True,
            data={
                "accepted": True,
                "detached": True,
                "status": task.status,
                "task_id": task.id,
                "staffdeck_task_id": task.id,
                "provider_task_id": task.external_task_id,
                "status_query": {
                    "method": "GET",
                    "path": (
                        f"/api/enterprise/external-business-tasks/{task.id}"
                        f"?tenant_id={tool.tenant_id}&tool_id={tool.id}"
                    ),
                    "tenant_id": tool.tenant_id,
                    "guidance": (
                        "Use external_task_status with this StaffDeck task_id to query the "
                        "existing task. Do not invoke the submission tool again to check status."
                    ),
                },
                "user_reply": (
                    f"任务 #{task.id}：{state_text}。"
                    f"您随时可以对我说 “查询 #{task.id} 状态” 查看结果。"
                ),
            },
            error=None,
        )

    def _execute_a2a_tool(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        invocation_id: str | None = None,
        timeout_seconds_override: float | None = None,
    ) -> ToolResult:
        """Invoke an A2A agent and wait for its durable Task lifecycle."""

        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        headers.setdefault("Content-Type", "application/json")
        config = tool.config_json if isinstance(tool.config_json, dict) else {}
        a2a_version = str(config.get("a2a_version") or "1.0").strip()
        if a2a_version:
            headers.setdefault("A2A-Version", a2a_version)
        try:
            data = A2AClient(
                self.db,
                tool,
                headers=headers,
                timeout_seconds=timeout_seconds_override,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
            ).execute(arguments)
            return ToolResult(tool_name=tool.name, success=True, data=data, error=None)
        except A2AClientError as exc:
            return self._error(tool.name, exc.code, str(exc))
        except httpx.HTTPStatusError as exc:
            return self._error(
                tool.name,
                "A2A_HTTP_ERROR",
                f"A2A Agent 返回异常状态码：{exc.response.status_code}",
            )
        except Exception as exc:
            return self._error(tool.name, "A2A_EXECUTION_ERROR", str(exc))

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
        if not 1 <= timeout_seconds <= 3600:
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
