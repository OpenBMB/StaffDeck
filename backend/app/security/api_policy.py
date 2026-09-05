"""Module-owned REST boundaries. PEP is not restricted to chat or the DSH bridge.

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
    "knowledge_base": (models.KnowledgeBase, "id", ("knowledge_base_id", "base_id")),
    "team": (models.Team, "id", ("team_id",)),
    "channel": (models.ChannelBinding, "id", ("binding_id",)),
    "model_config": (models.ModelConfig, "id", ("model_id", "config_id")),
    "session": (models.ChatSession, "id", ("session_id",)),
}


def resource_ref(db: Session, tenant_id: str, kind: str, identifier: str) -> ResourceRef | None:
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
                ref = resource_ref(db, tenant_id, kind, str(values[key]))
                if ref is not None:
                    refs.append(ref)
        if kind == "knowledge_base":
            for key, model in (
                ("document_id", models.KnowledgeDocument),
                ("bucket_id", models.KnowledgeBucket),
                ("chunk_id", models.KnowledgeChunk),
            ):
                if values.get(key):
                    row = db.get(model, str(values[key]))
                    if (
                        row is not None
                        and row.tenant_id == tenant_id
                        and getattr(row, "knowledge_base_id", None)
                    ):
                        ref = resource_ref(db, tenant_id, kind, row.knowledge_base_id)
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
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        original = route.get_route_handler()

        async def handle(request: Request, endpoint=original):
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
            filtered = []
            for item in payload:
                if not isinstance(item, dict) or not (item.get(field) or item.get("id")):
                    filtered.append(item)
                    continue
                ref = resource_ref(db, tenant_id, kind, str(item.get(field) or item["id"]))
                if ref is None:  # a child/list projection, guarded by its parent boundary
                    filtered.append(item)
                    continue
                try:
                    await run_in_threadpool(
                        require_resource, user, ref, "view", module=kind, local_checked=True
                    )
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    continue
                filtered.append(item)
            response.body = json.dumps(filtered, ensure_ascii=False, separators=(",", ":")).encode()
            response.headers["content-length"] = str(len(response.body))
            return response

        route.app = request_response(handle)
