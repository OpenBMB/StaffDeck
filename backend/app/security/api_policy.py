"""Module-owned REST boundaries. PEP is not restricted to chat or the Harness v3 bridge.

These guards are additional to the existing OSS endpoint checks. Authentication and
tenant validation run first; list filtering runs on already-authorized endpoint output.
Only server-selected IDs/row projections are sent to the configured PEP.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute, request_response
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.db import models
from app.security.auth import get_current_user
from app.security.module_policy import require_resource
from staffdeck_harness.contracts.security import ResourceRef


# Explicit module routing declarations, not permissions inferred from arbitrary URLs.
RESOURCE_MODELS = {
    "agent": (models.AgentProfile, "id", ("agent_id",)),
    "sop": (models.Skill, "skill_id", ("skill_id",)),
    "general_skill": (models.GeneralSkill, "slug", ("slug",)),
    "tool": (models.Tool, "id", ("tool_id",)),
    "mcp_server": (models.MCPServer, "id", ("server_id",)),
    "knowledge_base": (None, "id", ("knowledge_base_id", "base_id")),
    "team": (models.Team, "id", ("team_id",)),
    "channel": (models.ChannelBinding, "id", ("binding_id",)),
    "model_config": (models.ModelConfig, "id", ("model_id", "config_id")),
    "session": (models.ChatSession, "id", ("session_id",)),
}


def resource_ref(db: Session, tenant_id: str, kind: str, identifier: str, agent_id=None) -> ResourceRef | None:
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter, enabled
    if kind == "knowledge_base":
        try:
            row = StaffDeckKnowledgeAdapter(db, tenant_id, agent_id).call(
                "GET", f"/knowledge-bases/{identifier}",
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise
        return ResourceRef(type=kind, id=row["id"], tenant_id=tenant_id, attributes={
            "owner_user_id": row.get("metadata", {}).get("owner_id"),
            "status": row["status"], "enabled": row["status"] == "active",
        })
    model, field, _ = RESOURCE_MODELS[kind]
    row = db.exec(
        select(model).where(model.tenant_id == tenant_id, getattr(model, field) == identifier)
    ).first()
    if row is None and field != "id":
        row = db.get(model, identifier)
    if row is None or row.tenant_id != tenant_id:
        return None
    meta = dict(getattr(row, "metadata_json", None) or {})
    return ResourceRef(
        type=kind,
        id=row.id,
        tenant_id=tenant_id,
        attributes={
            "authority": "staffdeck.team" if kind == "team" else None,
            "owner_user_id": getattr(row, "owner_user_id", None) or meta.get("owner_user_id"),
            "user_id": getattr(row, "user_id", None),
            "is_overall": getattr(row, "is_overall", False),
            "status": getattr(row, "status", None),
            "enabled": getattr(row, "enabled", True),
            "binding_status": getattr(row, "status", "active") if kind == "channel" else None,
        },
    )


def module_policy(kind: str, *, use_endpoints: tuple[str, ...] = ()):
    async def check(
        request: Request,
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_session),
    ):
        values = dict(request.query_params)
        if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
            try:
                body = await request.json()
                if isinstance(body, dict):
                    values.update(
                        {
                            k: body[k]
                            for k in ("tenant_id", "agent_id", *RESOURCE_MODELS[kind][2])
                            if k in body
                        }
                    )
            except (ValueError, UnicodeError):
                pass  # endpoint owns validation of malformed input
        values.update(request.path_params)
        tenant_id = str(values.get("tenant_id") or user.tenant_id)
        if tenant_id != user.tenant_id:
            raise HTTPException(403, "Tenant mismatch")
        action = (
            "view"
            if request.method in {"GET", "HEAD"}
            else "delete"
            if request.method == "DELETE"
            else "edit"
        )
        if request.scope.get("endpoint").__name__ in use_endpoints:
            action = "use"
        refs = []
        for key in RESOURCE_MODELS[kind][2]:
            if values.get(key):
                ref = await run_in_threadpool(resource_ref, db, tenant_id, kind, str(values[key]), values.get("agent_id"))
                if ref is not None:
                    refs.append(ref)
        if kind == "knowledge_base":
            for key in ("document_id", "bucket_id", "chunk_id"):
                if values.get(key):
                    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter, enabled
                    adapter = StaffDeckKnowledgeAdapter(db, tenant_id, values.get("agent_id"), user)
                    collection = {"document_id": "documents", "bucket_id": "buckets", "chunk_id": "chunks"}[key]
                    row = await run_in_threadpool(adapter.call, "GET", f"/{collection}/{values[key]}")
                    ref = await run_in_threadpool(resource_ref, db, tenant_id, kind, row["knowledge_base_id"], values.get("agent_id"))
                    if ref:
                        refs.append(ref)
        for ref in refs:
            await run_in_threadpool(
                require_resource, user, ref, action, module=kind, local_checked=True
            )
        request.state.module_policy = (kind, tenant_id, user, db)

    return check


def install_response_filters(app: Any) -> None:
    """Filter module catalogs after OSS visibility checks, before bytes leave the API."""
    def iter_routes(routes):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from iter_routes(included.routes)
            else:
                yield route

    for route in iter_routes(app.routes):
        if not isinstance(route, APIRoute):
            continue
        original_get_route_handler = route.get_route_handler

        async def filter_response(request: Request, endpoint):
            response = await endpoint(request)
            state = getattr(request.state, "module_policy", None)
            if not state or request.method != "GET" or response.status_code != 200:
                return response
            body = getattr(response, "body", None)
            if not body or "application/json" not in response.headers.get("content-type", ""):
                return response
            payload = json.loads(body)
            if not isinstance(payload, list):
                return response
            kind, tenant_id, user, db = state
            _, field, _ = RESOURCE_MODELS[kind]
            def filter_catalog():
                from app.security.module_policy import visible_resources
                indexed = []
                for index, item in enumerate(payload):
                    if isinstance(item, dict) and (item.get(field) or item.get('id')):
                        ref = resource_ref(db, tenant_id, kind, str(item.get(field) or item['id']), request.query_params.get("agent_id"))
                        if ref is not None: indexed.append((index, ref))
                allowed = visible_resources(user, [ref for _, ref in indexed], module=kind) if indexed else []
                denied = {index for (index, _), keep in zip(indexed, allowed) if not keep}
                return [item for index, item in enumerate(payload) if index not in denied]
            filtered = await run_in_threadpool(filter_catalog)
            response.body = json.dumps(filtered, ensure_ascii=False, separators=(",", ":")).encode()
            response.headers["content-length"] = str(len(response.body))
            return response

        def wrapped_get_route_handler(original=original_get_route_handler):
            endpoint = original()

            async def handle(request: Request):
                return await filter_response(request, endpoint)

            return handle

        route.get_route_handler = wrapped_get_route_handler
        route.app = request_response(wrapped_get_route_handler())
