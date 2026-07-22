from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    require_overall_agent,
    resource_binding_metadata,
    user_creator_metadata,
    visible_tool_rows,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import AgentProfile, AgentResourceBinding, MCPServer, Tool, User, utc_now
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
    require_tenant_admin,
)
from app.security.tenant import ensure_tenant
from app.tools import ToolExecutor
from app.tools.http_request import prepare_get_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool, list_mcp_tools
from app.tools.tool_schema import (
    MCPDiscoverRequest,
    MCPDiscoverResponse,
    MCPDiscoveredTool,
    MCPServerConnection,
    MCPServerCreateRequest,
    MCPServerRead,
    MCPServerUpdateRequest,
    MCPSyncRequest,
    MCPSyncResponse,
    ToolBucketRead,
    ToolCall,
    ToolCreateRequest,
    ToolError,
    ToolExecutionPolicy,
    ToolProbeRequest,
    ToolProbeResponse,
    ToolRead,
    ToolResult,
    ToolTestRequest,
    ToolUpdateRequest,
)

router = APIRouter(prefix="/api/enterprise/tools", tags=["enterprise:tools"])
mcp_router = APIRouter(prefix="/api/enterprise/mcp-servers", tags=["enterprise:mcp-servers"])


def tool_read(row: Tool, metadata: dict[str, Any] | None = None) -> ToolRead:
    config = dict(row.config_json or {})
    return ToolRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket or "未分桶",
        tool_type=row.tool_type or "http",
        method=row.method,
        url=row.url,
        headers=row.headers_json or {},
        auth=row.auth_json or {},
        mcp_config={key: value for key, value in config.items() if key != "execution"},
        execution_policy=_read_execution_policy(config),
        input_schema=row.input_schema or {},
        output_schema=row.output_schema or {},
        allowed_skills=row.allowed_skills_json or [],
        mcp_server_id=row.mcp_server_id,
        enabled=row.enabled,
        metadata=dict(metadata or {}),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ToolRead], dependencies=[Depends(require_agent_scope_viewer)])
def list_tools(
    tenant_id: str = Query(...),
    bucket: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> list[ToolRead]:
    ensure_tenant(db, tenant_id)
    rows = _visible_tool_rows(db, tenant_id, bucket, agent_id)
    metadata_by_id = resource_binding_metadata(db, tenant_id, agent_id, "tool")
    return [tool_read(row, metadata_by_id.get(row.id)) for row in rows]


@router.get(
    "/buckets",
    response_model=list[ToolBucketRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_tool_buckets(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> list[ToolBucketRead]:
    ensure_tenant(db, tenant_id)
    rows = _visible_tool_rows(db, tenant_id, None, agent_id)
    grouped: dict[str, ToolBucketRead] = {}
    for row in rows:
        bucket = row.bucket or "未分桶"
        item = grouped.setdefault(
            bucket, ToolBucketRead(bucket=bucket, total=0, enabled_count=0, disabled_count=0)
        )
        item.total += 1
        if row.enabled:
            item.enabled_count += 1
        else:
            item.disabled_count += 1
        item.tool_ids.append(row.id)
    return sorted(grouped.values(), key=lambda item: (-item.total, item.bucket))


@router.post("", response_model=ToolRead)
def create_tool(
    request: ToolCreateRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolRead:
    ensure_tenant(db, request.tenant_id)
    existing = db.exec(
        select(Tool).where(Tool.tenant_id == request.tenant_id, Tool.name == request.name)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Tool name already exists for this tenant")
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    row = Tool(
        tenant_id=request.tenant_id,
        name=request.name,
        display_name=request.display_name,
        description=request.description,
        bucket=_normalize_bucket(request.bucket),
        tool_type=request.tool_type,
        method=request.method,
        url=request.url,
        headers_json=request.headers,
        auth_json=request.auth,
        config_json=_tool_config(request.mcp_config, request.execution_policy),
        input_schema=request.input_schema,
        output_schema=request.output_schema,
        allowed_skills_json=request.allowed_skills,
        enabled=request.enabled,
    )
    db.add(row)
    db.flush()
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    metadata_by_id = resource_binding_metadata(db, request.tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.post("/probe", response_model=ToolProbeResponse)
def probe_tool(
    request: ToolProbeRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolProbeResponse:
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if request.tool_type == "mcp":
        timeout_seconds = _probe_timeout_seconds(request)
        try:
            data = execute_mcp_tool(
                request.mcp_config,
                request.sample_arguments,
                timeout_seconds=timeout_seconds,
            )
        except MCPClientError as exc:
            return ToolProbeResponse(
                success=False,
                status_code=400,
                error=ToolError(code="MCP_ERROR", message=str(exc)),
            )
        except Exception as exc:
            return ToolProbeResponse(
                success=False,
                status_code=500,
                error=ToolError(code="MCP_PROBE_ERROR", message=str(exc)),
            )
        return ToolProbeResponse(
            success=True,
            status_code=200,
            data_preview=data,
            inferred_output_schema=_infer_json_schema(data),
            error=None,
        )
    headers = ToolExecutor(db)._resolve_headers(request.headers, request.auth)  # noqa: SLF001
    url = _normalize_probe_url(request.url)
    timeout_seconds = _probe_timeout_seconds(request)
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            if request.method.upper() == "GET":
                request_url, request_kwargs = prepare_get_request(url, request.sample_arguments)
                response = client.request(
                    request.method.upper(), request_url, headers=headers, **request_kwargs
                )
            else:
                response = client.request(
                    request.method.upper(), url, headers=headers, json=request.sample_arguments
                )
    except httpx.TimeoutException:
        return ToolProbeResponse(
            success=False,
            error=ToolError(code="TIMEOUT", message="工具探测超时。"),
        )
    except Exception as exc:
        return ToolProbeResponse(
            success=False,
            error=ToolError(code="PROBE_ERROR", message=str(exc)),
        )

    data_preview = _response_preview(response)
    success = 200 <= response.status_code < 300
    return ToolProbeResponse(
        success=success,
        status_code=response.status_code,
        data_preview=data_preview,
        inferred_output_schema=_infer_json_schema(data_preview) if success else {},
        error=None
        if success
        else ToolError(
            code="HTTP_ERROR", message=f"工具探测返回异常状态码：{response.status_code}"
        ),
    )


@router.get(
    "/{tool_id}", response_model=ToolRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_tool(
    tool_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> ToolRead:
    row = _get_tool(db, tenant_id, tool_id)
    _ensure_tool_visible(db, tenant_id, row, agent_id)
    metadata_by_id = resource_binding_metadata(db, tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.put("/{tool_id}", response_model=ToolRead)
def update_tool(
    tool_id: str,
    request: ToolUpdateRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolRead:
    row = _get_tool(db, request.tenant_id, tool_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    _ensure_tool_visible(db, request.tenant_id, row, agent_id)
    if agent and not agent.is_overall:
        source_tool_id = row.id
        source_was_open_gallery = is_open_gallery_resource(db, request.tenant_id, "tool", row)
        row = _ensure_private_tool_for_agent(db, request.tenant_id, agent, row)
        if not source_was_open_gallery and request.name.strip() != row.name:
            raise HTTPException(status_code=400, detail="Tool name cannot be modified")
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        source_tool_id = row.id
        if request.name.strip() != row.name:
            raise HTTPException(status_code=400, detail="Tool name cannot be modified")
    row.display_name = request.display_name
    row.description = request.description
    row.bucket = _normalize_bucket(request.bucket)
    row.tool_type = request.tool_type
    row.method = request.method
    row.url = request.url
    row.headers_json = request.headers
    row.auth_json = request.auth
    row.config_json = _tool_config(
        request.mcp_config,
        request.execution_policy,
        existing=row.config_json,
    )
    row.input_schema = request.input_schema
    row.output_schema = request.output_schema
    row.allowed_skills_json = request.allowed_skills
    row.enabled = request.enabled
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        if source_tool_id != row.id:
            source_binding = _tool_binding(db, request.tenant_id, agent.id, source_tool_id)
            if source_binding:
                source_binding.status = "deleted"
                source_binding.updated_at = utc_now()
                db.add(source_binding)
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata if source_tool_id != row.id else None,
        )
    else:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    metadata_by_id = resource_binding_metadata(db, request.tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.delete("/{tool_id}")
def delete_tool(
    tool_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_tool(db, tenant_id, tool_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _tool_binding(db, tenant_id, agent.id, row.id)
        if binding:
            binding.status = "deleted"
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
            return {"status": "hidden"}
        raise HTTPException(status_code=404, detail="Tool not visible to this agent")
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "tool", row):
            raise HTTPException(status_code=404, detail="Tool not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "tool", row.id)
        db.commit()
        return {"status": "hidden"}
    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{tool_id}/test", response_model=ToolResult)
def test_tool(
    tool_id: str,
    request: ToolTestRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolResult:
    ensure_current_user_tenant(request.tenant_id, current_user)
    row = _get_tool(db, request.tenant_id, tool_id)
    _ensure_tool_visible(db, request.tenant_id, row, agent_id)
    return ToolExecutor(db).execute(
        request.tenant_id,
        ToolCall(name=row.name, arguments=request.arguments),
        agent_id=agent_id,
    )


def _get_tool(db: Session, tenant_id: str, tool_id: str) -> Tool:
    ensure_tenant(db, tenant_id)
    row = db.get(Tool, tool_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Tool not found")
    return row


def _visible_tool_rows(
    db: Session,
    tenant_id: str,
    bucket: str | None = None,
    agent_id: str | None = None,
) -> list[Tool]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    rows = visible_tool_rows(db, tenant_id, agent_id, include_inactive=True)
    if bucket and bucket != "__all__":
        return [row for row in rows if row.bucket == bucket]
    return rows


def _ensure_tool_visible(db: Session, tenant_id: str, row: Tool, agent_id: str | None) -> None:
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent and not agent.is_overall:
        binding = _tool_binding(db, tenant_id, agent.id, row.id)
        if not binding or not is_bound_resource_visible_for_agent(
            db, tenant_id, "tool", row, binding
        ):
            raise HTTPException(status_code=404, detail="Tool not visible to this agent")
    if (not agent or agent.is_overall) and not is_open_gallery_resource(db, tenant_id, "tool", row):
        raise HTTPException(status_code=404, detail="Tool not visible in open gallery")


def _tool_binding(
    db: Session, tenant_id: str, agent_id: str, tool_id: str
) -> AgentResourceBinding | None:
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.resource_id == tool_id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()


def _ensure_private_tool_for_agent(
    db: Session, tenant_id: str, agent: AgentProfile, row: Tool
) -> Tool:
    if not is_open_gallery_resource(db, tenant_id, "tool", row):
        return row
    now = utc_now()
    clone = Tool(
        tenant_id=tenant_id,
        name=_unique_tool_name(db, tenant_id, row.name, agent.id),
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket,
        tool_type=row.tool_type,
        method=row.method,
        url=row.url,
        headers_json=dict(row.headers_json or {}),
        auth_json=dict(row.auth_json or {}),
        config_json=dict(row.config_json or {}),
        input_schema=dict(row.input_schema or {}),
        output_schema=dict(row.output_schema or {}),
        allowed_skills_json=list(row.allowed_skills_json or []),
        mcp_server_id=row.mcp_server_id,
        enabled=row.enabled,
        created_at=now,
        updated_at=now,
    )
    db.add(clone)
    db.flush()
    return clone


def _tool_name_taken(db: Session, tenant_id: str, name: str, exclude_id: str | None = None) -> bool:
    stmt = select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == name)
    if exclude_id:
        stmt = stmt.where(Tool.id != exclude_id)
    return db.exec(stmt).first() is not None


def _unique_tool_name(
    db: Session,
    tenant_id: str,
    base_name: str,
    agent_id: str,
    exclude_id: str | None = None,
) -> str:
    base = (base_name or "tool").strip() or "tool"
    suffix_base = f"{base}-{agent_id[:8]}"
    candidate = suffix_base
    suffix = 2
    while _tool_name_taken(db, tenant_id, candidate, exclude_id=exclude_id):
        candidate = f"{suffix_base}-{suffix}"
        suffix += 1
    return candidate


def _normalize_bucket(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or "未分桶"


def _normalize_probe_url(url: str) -> str:
    stripped = url.strip()
    if stripped.startswith("/"):
        return f"{get_settings().normalized_tool_base_url}{stripped}"
    return stripped


def _tool_config(
    mcp_config: dict[str, Any],
    execution_policy: object | None,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = {
        key: value for key, value in (mcp_config or {}).items() if key != "execution"
    }
    if execution_policy is not None:
        config["execution"] = execution_policy.model_dump(mode="json")
    elif isinstance((existing or {}).get("execution"), dict):
        config["execution"] = dict(existing["execution"])
    return config


def _read_execution_policy(config: dict[str, Any]) -> ToolExecutionPolicy | None:
    execution = config.get("execution")
    if not isinstance(execution, dict):
        return None
    try:
        return ToolExecutionPolicy.model_validate(execution)
    except ValueError:
        return None


def _probe_timeout_seconds(request: ToolProbeRequest) -> float:
    if request.execution_policy is not None:
        return request.execution_policy.timeout_seconds
    return get_settings().tool_timeout_seconds


def _response_preview(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        text = response.text
        return text[:2000] if len(text) > 2000 else text


def _infer_json_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        properties = {str(key): _infer_json_schema(item) for key, item in value.items()}
        return {"type": "object", "properties": properties, "required": list(properties.keys())}
    if isinstance(value, list):
        item_schema = _infer_json_schema(value[0]) if value else {}
        return {"type": "array", "items": item_schema}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


# --------------------------------------------------------------------------- #
# MCP Servers（工具集）
# --------------------------------------------------------------------------- #


def _server_connection(row: MCPServer) -> MCPServerConnection:
    return MCPServerConnection(
        transport=row.transport,  # type: ignore[arg-type]
        url=row.url,
        headers=row.headers_json or {},
        command=row.command,
        args=row.args_json or [],
        env=row.env_json or {},
        cwd=row.cwd,
    )


def _connection_to_client_config(connection: MCPServerConnection) -> dict[str, Any]:
    """把结构化连接配置转成 mcp_client 认识的扁平 config。"""
    config: dict[str, Any] = {"transport": connection.transport}
    if connection.transport in {"streamable_http", "sse"}:
        config["url"] = connection.url or ""
        if connection.headers:
            config["headers"] = dict(connection.headers)
    elif connection.transport == "stdio":
        config["command"] = connection.command or ""
        config["args"] = list(connection.args or [])
        if connection.env:
            config["env"] = dict(connection.env)
        if connection.cwd:
            config["cwd"] = connection.cwd
    elif connection.transport == "builtin":
        config["server"] = "builtin.demo"
    return config


def mcp_server_read(row: MCPServer, db: Session) -> MCPServerRead:
    tool_count = len(db.exec(select(Tool.id).where(Tool.mcp_server_id == row.id)).all())
    return MCPServerRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket or "MCP 工具",
        connection=_server_connection(row),
        enabled=row.enabled,
        last_synced_at=row.last_synced_at.isoformat() if row.last_synced_at else None,
        tool_count=tool_count,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@mcp_router.get(
    "", response_model=list[MCPServerRead], dependencies=[Depends(require_tenant_admin)]
)
def list_mcp_servers(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> list[MCPServerRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(MCPServer).where(MCPServer.tenant_id == tenant_id).order_by(MCPServer.name)
    ).all()
    return [mcp_server_read(row, db) for row in rows]


@mcp_router.post("", response_model=MCPServerRead)
def create_mcp_server(
    request: MCPServerCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPServerRead:
    ensure_tenant(db, request.tenant_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    existing = db.exec(
        select(MCPServer).where(
            MCPServer.tenant_id == request.tenant_id, MCPServer.name == request.name
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="MCP server name already exists for this tenant"
        )
    conn = request.connection
    row = MCPServer(
        tenant_id=request.tenant_id,
        name=request.name,
        display_name=request.display_name,
        description=request.description,
        bucket=_normalize_bucket(request.bucket) if request.bucket else "MCP 工具",
        transport=conn.transport,
        url=conn.url,
        headers_json=conn.headers,
        command=conn.command,
        args_json=conn.args,
        env_json=conn.env,
        cwd=conn.cwd,
        enabled=request.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return mcp_server_read(row, db)


@mcp_router.get(
    "/{server_id}", response_model=MCPServerRead, dependencies=[Depends(require_tenant_admin)]
)
def get_mcp_server(
    server_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> MCPServerRead:
    row = _get_mcp_server(db, tenant_id, server_id)
    return mcp_server_read(row, db)


@mcp_router.put("/{server_id}", response_model=MCPServerRead)
def update_mcp_server(
    server_id: str,
    request: MCPServerUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPServerRead:
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    conn = request.connection
    row.name = request.name
    row.display_name = request.display_name
    row.description = request.description
    row.bucket = _normalize_bucket(request.bucket) if request.bucket else "MCP 工具"
    row.transport = conn.transport
    row.url = conn.url
    row.headers_json = conn.headers
    row.command = conn.command
    row.args_json = conn.args
    row.env_json = conn.env
    row.cwd = conn.cwd
    row.enabled = request.enabled
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return mcp_server_read(row, db)


@mcp_router.delete("/{server_id}")
def delete_mcp_server(
    server_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    remove_tools: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    row = _get_mcp_server(db, tenant_id, server_id)
    if remove_tools:
        tools = db.exec(select(Tool).where(Tool.mcp_server_id == server_id)).all()
        for tool in tools:
            db.delete(tool)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@mcp_router.post("/discover", response_model=MCPDiscoverResponse)
def discover_mcp_tools_adhoc(
    request: MCPDiscoverRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPDiscoverResponse:
    """未保存 Server 时，用连接配置直接探测 tools/list。"""
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if request.connection is None:
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MISSING_CONNECTION", message="缺少 MCP 连接配置。"),
        )
    return _discover_response(request.connection)


@mcp_router.post("/{server_id}/discover", response_model=MCPDiscoverResponse)
def discover_mcp_tools(
    server_id: str,
    request: MCPDiscoverRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPDiscoverResponse:
    """已保存 Server：拉取 tools/list，并标注哪些已导入为 Tool。"""
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    connection = request.connection or _server_connection(row)
    response = _discover_response(connection)
    if response.success:
        row.discovered_tools_json = [tool.model_dump() for tool in response.tools]
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        existing = _server_tools_by_leaf_name(db, server_id)
        for tool in response.tools:
            match = existing.get(tool.name)
            if match is not None:
                tool.imported = True
                tool.tool_id = match.id
                tool.enabled = match.enabled
    return response


@mcp_router.post("/{server_id}/sync", response_model=MCPSyncResponse)
def sync_mcp_tools(
    server_id: str,
    request: MCPSyncRequest,
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> MCPSyncResponse:
    """把发现到的工具落成 Tool 行（新建/更新 schema），可选择导入的子集。"""
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    connection = _server_connection(row)
    discovery = _discover_response(connection)
    if not discovery.success:
        return MCPSyncResponse(success=False, error=discovery.error)

    row.discovered_tools_json = [tool.model_dump() for tool in discovery.tools]
    row.last_synced_at = utc_now()
    row.updated_at = utc_now()

    selected = set(request.tool_names or [])
    existing = _server_tools_by_leaf_name(db, server_id)
    imported: list[str] = []
    updated: list[str] = []
    touched_tool_ids: list[str] = []

    for tool in discovery.tools:
        if selected and tool.name not in selected:
            continue
        current = existing.get(tool.name)
        if current is None:
            new_row = Tool(
                tenant_id=row.tenant_id,
                name=_scoped_tool_name(row.name, tool.name),
                display_name=tool.name,
                description=tool.description,
                bucket=row.bucket or "MCP 工具",
                tool_type="mcp",
                method="POST",
                url=f"mcp://{row.name}/{tool.name}",
                headers_json={},
                auth_json={},
                config_json={"tool": tool.name},
                input_schema=tool.input_schema or {},
                output_schema=tool.output_schema or {},
                allowed_skills_json=[],
                mcp_server_id=row.id,
                enabled=True,
            )
            db.add(new_row)
            db.flush()
            touched_tool_ids.append(new_row.id)
            imported.append(tool.name)
        else:
            current.description = tool.description or current.description
            current.input_schema = tool.input_schema or current.input_schema
            current.output_schema = tool.output_schema or current.output_schema
            current.config_json = {**(current.config_json or {}), "tool": tool.name}
            current.updated_at = utc_now()
            db.add(current)
            touched_tool_ids.append(current.id)
            updated.append(tool.name)

    # 与 create_tool 一致：按当前 agent 范围绑定——员工范围内只对该员工私有可见，
    # 否则落到工具广场（open gallery），所有人可见。已存在的工具也一并补绑定，
    # 避免「先在广场导入，再切到员工同步」时员工侧仍然看不到。
    agent = get_agent(db, row.tenant_id, agent_id)
    creator_metadata = user_creator_metadata(current_user)
    for tool_id in touched_tool_ids:
        if agent and not agent.is_overall:
            ensure_private_resource_binding(
                db,
                row.tenant_id,
                agent.id,
                "tool",
                tool_id,
                "active",
                metadata_json=creator_metadata,
            )
        else:
            ensure_open_gallery_binding(
                db,
                row.tenant_id,
                "tool",
                tool_id,
                "active",
                metadata_json=creator_metadata,
            )

    db.add(row)
    db.commit()
    return MCPSyncResponse(success=True, imported=imported, updated=updated, removed=[])


def _discover_response(connection: MCPServerConnection) -> MCPDiscoverResponse:
    config = _connection_to_client_config(connection)
    try:
        tools = list_mcp_tools(config, timeout_seconds=get_settings().tool_timeout_seconds)
    except MCPClientError as exc:
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MCP_DISCOVER_ERROR", message=str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MCP_DISCOVER_UNEXPECTED", message=str(exc)),
        )
    return MCPDiscoverResponse(
        success=True,
        tools=[
            MCPDiscoveredTool(
                name=item.get("name", ""),
                description=item.get("description", ""),
                input_schema=item.get("input_schema", {}),
                output_schema=item.get("output_schema", {}),
            )
            for item in tools
            if item.get("name")
        ],
    )


def _server_tools_by_leaf_name(db: Session, server_id: str) -> dict[str, Tool]:
    rows = db.exec(select(Tool).where(Tool.mcp_server_id == server_id)).all()
    result: dict[str, Tool] = {}
    for row in rows:
        leaf = str((row.config_json or {}).get("tool") or "").strip()
        if leaf:
            result[leaf] = row
    return result


def _scoped_tool_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}.{tool_name}"


def _get_mcp_server(db: Session, tenant_id: str, server_id: str) -> MCPServer:
    ensure_tenant(db, tenant_id)
    row = db.get(MCPServer, server_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row
