"""Authenticated host gateway to the independent KM API and shared UI."""

from __future__ import annotations

import json
import re
import os
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.db.models import AgentProfile, User
from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter, enabled
from app.security.auth import get_current_user

router = APIRouter(tags=["knowledge-core-gateway"])


@router.get("/km-ui/{path:path}")
async def ui_asset(path: str):
    endpoint = os.getenv("STAFFDECK_KM_URL", "").rstrip("/")
    if not endpoint:
        raise HTTPException(503, "Independent knowledge service is not configured")
    if not path or any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
        raise HTTPException(400, "Invalid Knowledge UI resource")
    if not re.fullmatch(r"ui/v1/(?:index\.html|version\.json|assets/[A-Za-z0-9_.-]+)", path):
        raise HTTPException(404, "Knowledge UI resource not found")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(endpoint + "/" + path)
        except httpx.HTTPError as error:
            raise HTTPException(503, "Knowledge UI unavailable") from error
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={"Cache-Control": "no-cache"})


@router.get("/api/km/config")
def configuration(user: User = Depends(get_current_user)):
    return {"enabled": enabled()}


@router.get("/api/km/health/ready")
async def readiness(user: User = Depends(get_current_user)):
    endpoint = os.getenv("STAFFDECK_KM_URL", "").rstrip("/")
    if not endpoint:
        raise HTTPException(503, "Independent knowledge service is not configured")
    bearer = os.getenv("STAFFDECK_KM_BEARER", "")
    headers = {"Authorization": "Bearer " + bearer} if bearer else {}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            ready = await client.get(endpoint + "/health/ready")
            config_response = await client.get(endpoint + "/api/km/config", headers=headers)
            config_response.raise_for_status()
            capabilities = config_response.json()
            required = ("spaces", "catalog", "versions", "concepts", "discovery")
            if capabilities.get("api_protocol") != "knowledge-core/v1" or any(not capabilities.get("modules", {}).get(name) for name in required):
                raise HTTPException(503, "StaffDeck knowledge integration requires full KM capabilities")
            access = await client.get(endpoint + "/api/km/v1/identity", headers=headers)
        except httpx.HTTPError as error:
            raise HTTPException(503, "Knowledge service unavailable") from error
    if not ready.is_success or not access.is_success:
        raise HTTPException(503, "Knowledge connection or identity configuration is unavailable")
    return {"status": "ready", "app": "knowledge-core"}


@router.api_route("/api/km/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(
    path: str, request: Request, db: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    body = await request.body()
    return await run_in_threadpool(_proxy, path, request, body, db, user)


def _proxy(path: str, request: Request, body: bytes, db: Session, user: User):
    if not enabled():
        raise HTTPException(503, "Independent knowledge service is not configured")
    value = json.loads(body) if body else None
    if isinstance(value, dict) and isinstance(value.get("metadata"), dict):
        value["metadata"].pop("capability_context", None)
        value["metadata"].pop("_pilotdeck_host", None)
    binding = bool(re.fullmatch(r"knowledge-bases/[^/]+/bindings", path))
    scopes = request.query_params.getlist("source_space_id") + request.query_params.getlist("space_id")
    if not binding and isinstance(value, dict) and "space_id" in value:
        scopes.append(value["space_id"])
    if any(not isinstance(item, str) or not item for item in scopes):
        raise HTTPException(422, "Invalid space_id")
    if len(set(scopes)) > 1:
        raise HTTPException(422, "Conflicting space selectors")
    scope = scopes[0] if scopes else None
    write = request.method != "GET" and path != "query"
    if scope:
        prefix = f"sd:{user.tenant_id}:"
        if not scope.startswith(prefix):
            raise HTTPException(403, "Space is outside this host namespace")
        agent_id = scope.removeprefix(prefix)
        if agent_id == "gallery":
            agent_id = None
    else:
        agent_id = None
    adapter = StaffDeckKnowledgeAdapter(db, user.tenant_id, agent_id, user, write=write)
    body_scoped = request.method == "POST" and (
        path in {"knowledge-bases", "query", "documents", "okf"}
        or re.fullmatch(r"knowledge-bases/[^/]+/(?:documents|okf)", path)
    )
    if isinstance(value, dict) and not binding and ("space_id" in value or body_scoped):
        value["space_id"] = adapter.space_id
    if path == "spaces" and request.method == "GET":
        from sqlmodel import select

        for employee in db.exec(select(AgentProfile).where(AgentProfile.tenant_id == user.tenant_id)):
            try:
                visible = StaffDeckKnowledgeAdapter(db, user.tenant_id, employee.id, user)
                visible.ensure_space()
            except HTTPException as error:
                if error.status_code not in {403, 404}:
                    raise
        rows = adapter.call("GET", "/spaces", scoped=False)
        result = []
        for row in rows:
            prefix = f"sd:{user.tenant_id}:"
            if not row["id"].startswith(prefix):
                continue
            employee_id = row["id"].removeprefix(prefix)
            try:
                StaffDeckKnowledgeAdapter(
                    db, user.tenant_id, None if employee_id == "gallery" else employee_id, user,
                )
                row["can_write"] = False
                try:
                    StaffDeckKnowledgeAdapter(
                        db, user.tenant_id, None if employee_id == "gallery" else employee_id,
                        user, write=True,
                    )
                    row["can_write"] = True
                except HTTPException as error:
                    if error.status_code not in {403, 404}:
                        raise
                result.append(row)
            except HTTPException as error:
                if error.status_code not in {403, 404}:
                    raise
        return result
    if path == "spaces" and request.method == "POST":
        raise HTTPException(409, "Host spaces are managed through the staff directory")
    parts = path.split("/")
    if parts[0] == "knowledge-bases" and len(parts) == 3 and parts[2] == "bindings":
        target = value.get("space_id") if isinstance(value, dict) else None
        prefix = f"sd:{user.tenant_id}:"
        if not isinstance(target, str) or not target.startswith(prefix):
            raise HTTPException(403, "Target space is outside this host namespace")
        target_id = target.removeprefix(prefix)
        target_adapter = StaffDeckKnowledgeAdapter(
            db, user.tenant_id, None if target_id == "gallery" else target_id, user, write=True,
        )
        target_adapter.ensure_space()
    if parts[0] == "knowledge-bases" and len(parts) > 1:
        if len(parts) == 3 and parts[2] == "binding":
            bindings = adapter.call("GET", "/bindings")
            if not any(item["knowledge_base_id"] == parts[1] for item in bindings):
                raise HTTPException(404, "Knowledge binding not found")
        else:
            adapter.require_base(parts[1], "manage" if write else "view")
    elif parts[0] in {"documents", "chunks", "buckets", "concepts", "jobs", "suggestions"} and len(parts) > 1:
        row = adapter.call("GET", "/" + "/".join(parts[:2]))
        adapter.require_base(row["knowledge_base_id"], "manage" if write else "view")
    if path == "query" and isinstance(value, dict):
        base_ids = value.get("knowledge_base_ids") or [
            row["id"] for row in adapter.call("GET", "/knowledge-bases")
        ]
        for base_id in base_ids:
            adapter.require_base(base_id, "use")
        value["knowledge_base_ids"] = base_ids
    # The independent UI uses the generic base resource endpoint. An employee
    # space must edit its branch projection, otherwise a rename would mutate
    # shared metadata and leak into the overall/gallery view.
    employee = db.get(AgentProfile, adapter.agent_id) if adapter.agent_id else None
    def project_branch(row):
        if not employee or employee.is_overall:
            return row
        if "branch" not in row:
            row = adapter.call("GET", f"/knowledge-bases/{row['id']}")
        versions = adapter.call("GET", f"/knowledge-bases/{row['id']}/versions")
        head = next(item for item in versions if item["id"] == row["branch"]["head_version_id"])
        return {**row, **{field: head[field] for field in ("name", "description", "capability_scope", "updated_at")},
                "metadata": {**row.get("metadata", {}), **head.get("metadata", {})},
                "status": "active" if row["branch"]["status"] == "active" else "archived"}

    if request.method == "GET" and re.fullmatch(r"knowledge-bases/[^/]+", path):
        return project_branch(adapter.call("GET", "/" + path))
    if request.method == "GET" and path == "knowledge-bases":
        return [project_branch(row) for row in adapter.call("GET", "/knowledge-bases")]
    branch_edit = (
        request.method == "PUT"
        and re.fullmatch(r"knowledge-bases/[^/]+", path)
        and adapter.agent_id is not None
        and not (employee and employee.is_overall)
    )
    if branch_edit:
        kb_id = path.split("/", 1)[1]
        branch_body = dict(value or {})
        if not branch_body.get("expected_updated_at"):
            raise HTTPException(422, "expected_updated_at is required")
        adapter.call("PUT", f"/knowledge-bases/{kb_id}/branch", branch_body)
        projected = project_branch(adapter.call("GET", f"/knowledge-bases/{kb_id}"))
        return Response(
            json.dumps(jsonable_encoder(projected)),
            status_code=200,
            media_type="application/json",
        )
    suffix = "/" + path
    query = dict(request.query_params)
    query.pop("space_id", None)
    if "source_space_id" in query:
        query["source_space_id"] = adapter.space_id
    if query:
        from urllib.parse import urlencode
        suffix += "?" + urlencode(query)
    response = adapter.call(request.method, suffix, value, scoped=True, raw=True)
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={"Content-Disposition": response.headers["Content-Disposition"]}
                    if "Content-Disposition" in response.headers else {})
