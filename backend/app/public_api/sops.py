from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api import skills as internal_skills
from app.db import get_session
from app.db.models import (
    APIJob,
    APISOPDraft,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    Tool,
    utc_now,
)
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.idempotency import replay_idempotent_response, store_idempotent_response
from app.public_api.jobs import create_job, job_read, register_job_handler, update_job
from app.public_api.json_patch import JSONPatchError, apply_json_patch
from app.public_api.schemas import (
    SOPGenerateRequest,
    SOPPublishRequest,
    SOPRewritePublicRequest,
    SOPStructuredCreate,
)
from app.public_api.sessions import ensure_public_agent
from app.public_api.utils import etag_for
from app.skills import SkillDistiller, SkillEditor
from app.skills.skill_schema import (
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillRewriteRequest,
    SkillUpdateRequest,
)


router = APIRouter(tags=["sops"])


def _draft_etag(content: dict[str, Any]) -> str:
    return etag_for(content)


def _draft_payload(row: APISOPDraft) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "sop_id": row.skill_id,
        "base_version": row.base_version,
        "draft_version": row.draft_version,
        "content": dict(row.content_json or {}),
        "status": row.status,
        "source": row.source,
        "warnings": list(row.warnings_json or []),
        "validation": dict(row.validation_json or {}),
        "etag": row.etag,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
        "published_at": row.published_at.isoformat() + "Z" if row.published_at else None,
    }


def _next_version(version: str | None) -> str:
    parts = str(version or "0.0.0").split(".")
    try:
        major, minor, patch = (int(parts[index]) if index < len(parts) else 0 for index in range(3))
    except ValueError:
        return "1.0.0"
    return f"{major}.{minor}.{patch + 1}"


def _runtime_skill(db: Session, tenant_id: str, skill_id: str) -> Skill | None:
    return db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()


def _new_draft(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str,
    credential_id: str | None,
    content: dict[str, Any],
    source: str,
    warnings: list[str] | None = None,
    base_version: str | None = None,
) -> APISOPDraft:
    card = SkillCard.model_validate(content)
    normalized = card.model_dump(mode="json")
    runtime = _runtime_skill(db, tenant_id, card.skill_id)
    base = base_version if base_version is not None else (runtime.version if runtime else None)
    normalized["version"] = _next_version(base) if base else normalized.get("version", "1.0.0")
    row = APISOPDraft(
        tenant_id=tenant_id,
        agent_id=agent_id,
        skill_id=card.skill_id,
        base_version=base,
        draft_version=str(normalized["version"]),
        content_json=normalized,
        source=source,
        warnings_json=warnings or [],
        etag=_draft_etag(normalized),
        created_by_credential_id=credential_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _owned_draft(
    db: Session,
    principal: PublicPrincipal,
    agent_id: str,
    sop_id: str,
    draft_id: str | None = None,
) -> APISOPDraft:
    statement = select(APISOPDraft).where(
        APISOPDraft.tenant_id == principal.tenant_id,
        APISOPDraft.agent_id == agent_id,
        APISOPDraft.skill_id == sop_id,
        APISOPDraft.status == "draft",
    )
    if draft_id:
        statement = statement.where(APISOPDraft.id == draft_id)
    row = db.exec(statement.order_by(APISOPDraft.updated_at.desc())).first()
    if not row:
        raise PublicAPIError(404, "SOP_DRAFT_NOT_FOUND", "SOP draft not found.")
    return row


def _require_etag(row: APISOPDraft, if_match: str | None) -> None:
    if not if_match:
        raise PublicAPIError(428, "IF_MATCH_REQUIRED", "If-Match is required for this update.")
    if if_match != row.etag:
        raise PublicAPIError(412, "ETAG_MISMATCH", "The SOP draft changed since it was read.")


def _validate_capability_refs(db: Session, row: APISOPDraft, card: SkillCard) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    type_models = {
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }
    refs_by_type: dict[str, set[str]] = {key: set() for key in type_models}
    for node in card.nodes:
        refs_by_type["general_skill"].update(node.capability_refs.general_skill_ids)
        refs_by_type["knowledge_base"].update(node.capability_refs.knowledge_base_ids)
        refs_by_type["tool"].update(node.capability_refs.tool_ids)
    for resource_type, ids in refs_by_type.items():
        model = type_models[resource_type]
        for resource_id in ids:
            resource = db.get(model, resource_id)
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == row.tenant_id,
                    AgentResourceBinding.agent_id == row.agent_id,
                    AgentResourceBinding.resource_type == resource_type,
                    AgentResourceBinding.resource_id == resource_id,
                    AgentResourceBinding.status == "active",
                )
            ).first()
            unavailable = not resource or resource.tenant_id != row.tenant_id or not binding
            if resource_type == "tool" and resource and not resource.enabled:
                unavailable = True
            if resource_type == "general_skill" and resource and resource.status != "published":
                unavailable = True
            if resource_type == "knowledge_base" and resource and resource.status != "active":
                unavailable = True
            if unavailable:
                errors.append(
                    {
                        "path": f"capability_refs.{resource_type}",
                        "code": "CAPABILITY_UNAVAILABLE",
                        "detail": f"{resource_type} {resource_id} is not active and bound to this agent.",
                    }
                )
    return errors


def validate_draft(db: Session, row: APISOPDraft) -> dict[str, Any]:
    try:
        card = SkillCard.model_validate(row.content_json)
        field_errors: list[dict[str, str]] = _validate_capability_refs(db, row, card)
    except ValidationError as exc:
        field_errors = [
            {
                "path": "/".join(str(item) for item in error.get("loc") or []),
                "code": str(error.get("type") or "INVALID_SOP"),
                "detail": str(error.get("msg") or "Invalid SOP"),
            }
            for error in exc.errors()
        ]
    result = {"valid": not field_errors, "errors": field_errors, "warnings": row.warnings_json or []}
    row.validation_json = result
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    return result


@router.get("/agents/{agent_id}/sops", response_model=dict)
def list_sops(
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    published = internal_skills.list_skills(principal.tenant_id, db, agent_id)
    drafts = db.exec(
        select(APISOPDraft).where(
            APISOPDraft.tenant_id == principal.tenant_id,
            APISOPDraft.agent_id == agent_id,
        ).order_by(APISOPDraft.updated_at.desc())
    ).all()
    return {
        "data": [item.model_dump(mode="json", exclude={"tenant_id"}) for item in published],
        "drafts": [_draft_payload(row) for row in drafts],
        "next_cursor": None,
    }


@router.post("/agents/{agent_id}/sops", response_model=dict, status_code=201)
def create_structured_sop(
    agent_id: str,
    body: SOPStructuredCreate,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    ensure_public_agent(db, principal, agent_id)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    try:
        row = _new_draft(
            db,
            tenant_id=principal.tenant_id,
            agent_id=agent_id,
            credential_id=principal.credential_id,
            content=body.content,
            source="structured",
        )
    except ValidationError as exc:
        raise PublicAPIError(422, "INVALID_SOP", "The SOP graph is invalid.", errors=exc.errors()) from exc
    payload = _draft_payload(row)
    response.headers["ETag"] = row.etag
    store_idempotent_response(
        db,
        principal,
        request,
        body.model_dump(mode="json"),
        payload,
        status_code=201,
        resource_id=row.id,
    )
    return payload


@router.post("/agents/{agent_id}/sops:generate", response_model=dict, status_code=202)
def generate_sop(
    agent_id: str,
    body: SOPGenerateRequest,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    ensure_public_agent(db, principal, agent_id)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    job = create_job(
        db,
        principal,
        kind="sop.generate",
        request_payload={"agent_id": agent_id, **body.model_dump(mode="json")},
        agent_id=agent_id,
    )
    payload = job_read(job).model_dump(mode="json")
    store_idempotent_response(db, principal, request, body.model_dump(mode="json"), payload, status_code=202, resource_id=job.id)
    return payload


@register_job_handler("sop.generate")
def execute_sop_generate(db: Session, job: APIJob) -> dict[str, Any]:
    payload = dict(job.request_json or {})
    update_job(db, job, stage="learning", progress=0.15, event_type="sop.generate.learning")
    request = SkillDistillRequest(
        tenant_id=job.tenant_id,
        title=str(payload["title"]),
        raw_content=str(payload["raw_content"]),
        business_domain=payload.get("business_domain"),
        model_config_id=payload.get("model_config_id"),
    )
    model = internal_skills._get_request_model(db, job.tenant_id, request.model_config_id)
    result = SkillDistiller().distill(internal_skills._with_available_tools(db, request), model)
    row = _new_draft(
        db,
        tenant_id=job.tenant_id,
        agent_id=str(job.agent_id),
        credential_id=job.credential_id,
        content=result.draft_skill.model_dump(mode="json"),
        source="generated",
        warnings=result.warnings,
    )
    return {"draft": _draft_payload(row), "tool_suggestions": [item.model_dump(mode="json") for item in result.tool_suggestions]}


@router.post("/agents/{agent_id}/sops/{sop_id}:rewrite", response_model=dict, status_code=202)
def rewrite_sop(
    agent_id: str,
    sop_id: str,
    body: SOPRewritePublicRequest,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    ensure_public_agent(db, principal, agent_id)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    if body.draft_id:
        current = _owned_draft(db, principal, agent_id, sop_id, body.draft_id).content_json
    else:
        runtime = _runtime_skill(db, principal.tenant_id, sop_id)
        if not runtime:
            raise PublicAPIError(404, "SOP_NOT_FOUND", "SOP not found.")
        current = internal_skills.get_skill(sop_id, principal.tenant_id, agent_id, db).content.model_dump(mode="json")
    job = create_job(
        db,
        principal,
        kind="sop.rewrite",
        request_payload={
            "agent_id": agent_id,
            "sop_id": sop_id,
            "current_skill": deepcopy(current),
            **body.model_dump(mode="json"),
        },
        agent_id=agent_id,
    )
    payload = job_read(job).model_dump(mode="json")
    store_idempotent_response(db, principal, request, body.model_dump(mode="json"), payload, status_code=202, resource_id=job.id)
    return payload


@register_job_handler("sop.rewrite")
def execute_sop_rewrite(db: Session, job: APIJob) -> dict[str, Any]:
    payload = dict(job.request_json or {})
    update_job(db, job, stage="rewriting", progress=0.15, event_type="sop.rewrite.rewriting")
    request = SkillRewriteRequest(
        tenant_id=job.tenant_id,
        current_skill=SkillCard.model_validate(payload["current_skill"]),
        instruction=str(payload["instruction"]),
        target_paths=list(payload.get("target_paths") or []),
        model_config_id=payload.get("model_config_id"),
    )
    model = internal_skills._get_request_model(db, job.tenant_id, request.model_config_id)
    result = SkillEditor().rewrite(internal_skills._with_available_tools_for_rewrite(db, request), model)
    row = _new_draft(
        db,
        tenant_id=job.tenant_id,
        agent_id=str(job.agent_id),
        credential_id=job.credential_id,
        content=result.draft_skill.model_dump(mode="json"),
        source="rewritten",
        warnings=result.warnings,
        base_version=request.current_skill.version,
    )
    return {
        "draft": _draft_payload(row),
        "changed_paths": result.changed_paths,
        "assistant_message": result.assistant_message,
        "tool_suggestions": [item.model_dump(mode="json") for item in result.tool_suggestions],
    }


@router.get("/agents/{agent_id}/sops/{sop_id}/drafts/{draft_id}", response_model=dict)
def get_sop_draft(
    agent_id: str,
    sop_id: str,
    draft_id: str,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_draft(db, principal, agent_id, sop_id, draft_id)
    response.headers["ETag"] = row.etag
    return _draft_payload(row)


@router.put("/agents/{agent_id}/sops/{sop_id}", response_model=dict)
def replace_sop_draft(
    agent_id: str,
    sop_id: str,
    body: SOPStructuredCreate,
    response: Response,
    draft_id: str | None = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_draft(db, principal, agent_id, sop_id, draft_id)
    _require_etag(row, if_match)
    try:
        card = SkillCard.model_validate(body.content)
    except ValidationError as exc:
        raise PublicAPIError(422, "INVALID_SOP", "The SOP graph is invalid.", errors=exc.errors()) from exc
    if card.skill_id != sop_id:
        raise PublicAPIError(422, "SOP_ID_IMMUTABLE", "skill_id cannot be changed.")
    row.content_json = card.model_dump(mode="json")
    row.draft_version = card.version
    row.etag = _draft_etag(row.content_json)
    row.validation_json = {}
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    response.headers["ETag"] = row.etag
    return _draft_payload(row)


@router.patch(
    "/agents/{agent_id}/sops/{sop_id}",
    response_model=dict,
    openapi_extra={"requestBody": {"content": {"application/json-patch+json": {}}}},
)
def patch_sop_draft(
    agent_id: str,
    sop_id: str,
    operations: list[dict[str, Any]],
    response: Response,
    draft_id: str | None = None,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_draft(db, principal, agent_id, sop_id, draft_id)
    _require_etag(row, if_match)
    try:
        patched = apply_json_patch(row.content_json, operations)
        card = SkillCard.model_validate(patched)
    except (JSONPatchError, ValidationError) as exc:
        errors = exc.errors() if isinstance(exc, ValidationError) else [{"path": "patch", "detail": str(exc)}]
        raise PublicAPIError(422, "INVALID_JSON_PATCH", "The JSON Patch is invalid.", errors=errors) from exc
    if card.skill_id != sop_id:
        raise PublicAPIError(422, "SOP_ID_IMMUTABLE", "skill_id cannot be changed.")
    row.content_json = card.model_dump(mode="json")
    row.draft_version = card.version
    row.etag = _draft_etag(row.content_json)
    row.validation_json = {}
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    response.headers["ETag"] = row.etag
    return _draft_payload(row)


@router.post("/sops/{sop_id}:validate", response_model=dict)
def validate_sop_route(
    sop_id: str,
    agent_id: str,
    draft_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_draft(db, principal, agent_id, sop_id, draft_id)
    return validate_draft(db, row)


@router.post("/sops/{sop_id}:publish", response_model=dict)
def publish_sop(
    sop_id: str,
    body: SOPPublishRequest,
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:publish")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    row = _owned_draft(db, principal, agent_id, sop_id, body.draft_id)
    validation = validate_draft(db, row)
    if not validation["valid"]:
        raise PublicAPIError(422, "SOP_VALIDATION_FAILED", "The SOP cannot be published.", errors=validation["errors"])
    card = SkillCard.model_validate(row.content_json)
    runtime = _runtime_skill(db, principal.tenant_id, sop_id)
    if runtime:
        internal_skills.update_skill(
            sop_id,
            SkillUpdateRequest(tenant_id=principal.tenant_id, content=card),
            agent_id,
            db,
            principal.actor_user,
        )
    else:
        internal_skills.create_skill(
            SkillCreateRequest(tenant_id=principal.tenant_id, content=card, status="draft"),
            agent_id,
            db,
            principal.actor_user,
        )
    published = internal_skills.publish_skill(
        sop_id,
        principal.tenant_id,
        agent_id,
        db,
        principal.actor_user,
    )
    row.status = "published"
    row.published_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    return {
        "sop": published.model_dump(mode="json", exclude={"tenant_id"}),
        "draft": _draft_payload(row),
    }


@router.post("/sops/{sop_id}:archive", response_model=dict)
def archive_sop(
    sop_id: str,
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:publish")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    archived = internal_skills.archive_skill(
        sop_id,
        principal.tenant_id,
        agent_id,
        db,
        principal.actor_user,
    )
    return archived.model_dump(mode="json", exclude={"tenant_id"})


@router.get("/sops/{sop_id}/versions", response_model=dict)
def list_sop_versions(
    sop_id: str,
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    rows = internal_skills.list_skill_versions(sop_id, principal.tenant_id, db, agent_id)
    return {"data": [row.model_dump(mode="json", exclude={"tenant_id"}) for row in rows]}


def _version_payload(
    db: Session,
    principal: PublicPrincipal,
    agent_id: str,
    sop_id: str,
    version: str,
) -> dict[str, Any]:
    rows = internal_skills.list_skill_versions(sop_id, principal.tenant_id, db, agent_id)
    for row in rows:
        if row.version == version:
            return row.model_dump(mode="json", exclude={"tenant_id"})
    raise PublicAPIError(404, "SOP_VERSION_NOT_FOUND", "SOP version not found.")


@router.get("/sops/{sop_id}/versions/{version}", response_model=dict)
def get_sop_version(
    sop_id: str,
    version: str,
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    return _version_payload(db, principal, agent_id, sop_id, version)


def _json_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(before.keys() | after.keys()):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            child_path = f"{path}/{escaped}"
            if key not in before:
                changes.append({"op": "add", "path": child_path, "value": after[key]})
            elif key not in after:
                changes.append({"op": "remove", "path": child_path, "old_value": before[key]})
            else:
                changes.extend(_json_diff(before[key], after[key], child_path))
        return changes
    return [{"op": "replace", "path": path or "", "old_value": before, "value": after}]


@router.get("/sops/{sop_id}/versions/{version}/diff", response_model=dict)
def diff_sop_version(
    sop_id: str,
    version: str,
    agent_id: str,
    compare_to: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    before = _version_payload(db, principal, agent_id, sop_id, compare_to)
    after = _version_payload(db, principal, agent_id, sop_id, version)
    return {
        "sop_id": sop_id,
        "from_version": compare_to,
        "to_version": version,
        "changes": _json_diff(before["content"], after["content"]),
    }


@router.post("/sops/{sop_id}/versions/{version}:rollback", response_model=dict, status_code=201)
def rollback_sop_version(
    sop_id: str,
    version: str,
    agent_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
    selected = _version_payload(db, principal, agent_id, sop_id, version)
    row = _new_draft(
        db,
        tenant_id=principal.tenant_id,
        agent_id=agent_id,
        credential_id=principal.credential_id,
        content=dict(selected["content"]),
        source="rollback",
        base_version=version,
    )
    return _draft_payload(row)
