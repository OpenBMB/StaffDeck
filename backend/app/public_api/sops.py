from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api import skills as internal_skills
from app.agents.branching import visible_published_skills, visible_skill_rows, model_for_agent
from app.db import get_session
from app.db.models import (
    APIJob,
    APISOPDraft,
    AgentResourceBinding,
    GeneralSkill,
    Skill,
    ChatSession,
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
    SOPRouteRequest,
)
from app.public_api.sessions import ensure_public_agent
from app.security.permissions import ensure_agent_scope_manager, require_agent_scope_viewer
from app.public_api.utils import etag_for
from app.skills import SkillDistiller, SkillEditor
from app.skills.sop_management import normalize_sop_write, legacy_sop_writes_disabled, SopManagementService
from app.skills.skill_schema import (
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillRewriteRequest,
    SkillUpdateRequest,
    skill_card_from_persisted,
)
from app.skills.nesting import SopNestingError, discoverable_sops, expand_visible_sops, validate_sop_nesting
from app.core.turn_planner import TurnPlanner


router = APIRouter(tags=["sops"])


def _draft_model():
    from app.db.models import SopDraftRecord
    return SopDraftRecord if legacy_sop_writes_disabled() else APISOPDraft


def _normalize_draft(content):
    return SopManagementService.normalize(content) if legacy_sop_writes_disabled() else normalize_sop_write(content)


def _draft_etag(content: dict[str, Any]) -> str:
    return etag_for(content)


def _routing_skill_snapshot(skill: Skill) -> Skill:
    """Detach visible DB rows before nesting expansion mutates a routing copy."""
    from app.db.models import SopDefinitionRecord
    if isinstance(skill, SopDefinitionRecord):
        from types import SimpleNamespace
        return SimpleNamespace(**deepcopy(skill.model_dump()))
    return Skill(
        id=skill.id,
        tenant_id=skill.tenant_id,
        skill_id=skill.skill_id,
        version=skill.version,
        name=skill.name,
        business_domain=skill.business_domain,
        description=skill.description,
        content_json=deepcopy(skill.content_json or {}),
        status=skill.status,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


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


def _runtime_skill(db: Session, tenant_id: str, skill_id: str, agent_id: str | None = None) -> Skill | None:
    if legacy_sop_writes_disabled():
        return next((row for row in SopManagementService.rows(db, tenant_id, agent_id)
            if row.skill_id == skill_id), None)
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
    normalized = _normalize_draft(card.model_dump(mode="json"))
    runtime = next((row for row in visible_published_skills(db, tenant_id, agent_id)
                    if row.skill_id == card.skill_id), None)
    base = base_version if base_version is not None else (runtime.version if runtime else None)
    normalized["version"] = _next_version(base) if base else normalized.get("version", "1.0.0")
    row = _draft_model()(
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
    model = _draft_model()
    statement = select(model).where(
        model.tenant_id == principal.tenant_id,
        model.agent_id == agent_id,
        model.skill_id == sop_id,
        model.status == "draft",
    )
    if draft_id:
        statement = statement.where(model.id == draft_id)
    row = db.exec(statement.order_by(model.updated_at.desc())).first()
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
        "knowledge_base": None,
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
            if resource_type == "knowledge_base":
                from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
                from fastapi import HTTPException
                try:
                    resource = StaffDeckKnowledgeAdapter(db, row.tenant_id, row.agent_id).call("GET", f"/knowledge-bases/{resource_id}")
                    unavailable = resource["status"] != "active" or resource["branch"]["status"] != "active"
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    unavailable = True
                if unavailable:
                    errors.append({"path": "capability_refs.knowledge_base", "code": "CAPABILITY_UNAVAILABLE", "detail": f"knowledge_base {resource_id} is not active and bound to this agent."})
                continue
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
        card = skill_card_from_persisted(row.content_json)
        field_errors: list[dict[str, str]] = _validate_capability_refs(db, row, card)
        try:
            validate_sop_nesting(
                card.skill_id,
                card.model_dump(mode="json"),
                visible_skill_rows(
                    db,
                    row.tenant_id,
                    row.agent_id,
                    include_inactive=True,
                ),
            )
        except SopNestingError as exc:
            field_errors.append(
                {
                    "path": "nodes.sub_sop_id",
                    "code": "INVALID_SOP_NESTING",
                    "detail": str(exc),
                }
            )
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
    require_agent_scope_viewer(principal.tenant_id, agent_id, principal.actor_user, db)
    published = internal_skills.list_skills(principal.tenant_id, db, agent_id)
    model = _draft_model()
    drafts = db.exec(
        select(model).where(
            model.tenant_id == principal.tenant_id,
            model.agent_id == agent_id,
        ).order_by(model.updated_at.desc())
    ).all()
    # Agent-scoped visibility may create or normalize a branch projection. Commit
    # that intentional read-side projection before the audit middleware opens its
    # own database session, otherwise SQLite can hold the write lock until the
    # request dependency is torn down and the response body never flushes.
    db.commit()
    return {
        "data": [item.model_dump(mode="json", exclude={"tenant_id"}) for item in published],
        "drafts": [_draft_payload(row) for row in drafts],
        "next_cursor": None,
    }


@router.post("/agents/{agent_id}/sops:route", response_model=dict)
def route_sop(
    agent_id: str,
    body: SOPRouteRequest,
    principal: PublicPrincipal = Depends(require_scopes("sops:read")),
    db: Session = Depends(get_session),
) -> dict:
    """Select a routable SOP using StaffDeck's native router and visibility rules."""
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    published = [
        _routing_skill_snapshot(skill)
        for skill in visible_published_skills(db, principal.tenant_id, agent_id)
    ]
    routing_skills = discoverable_sops(expand_visible_sops(published))
    db.commit()
    session = ChatSession(
        id=body.session_id or f"route:{agent_id}",
        tenant_id=principal.tenant_id,
        user_id=principal.actor_user.id,
        agent_id=agent_id,
        active_skill_id=body.active_sop_id,
        active_step_id=body.active_step_id,
        slots_json=dict(body.slots),
        pending_tasks_json=list(body.pending_tasks),
        awaiting_input_json=dict(body.awaiting_input) if body.awaiting_input else None,
        status="active",
    )
    if body.model_source == "pilotdeck_host":
        from app.public_api.pilotdeck_domain_host import require_pilotdeck_domain_host
        try:
            host = require_pilotdeck_domain_host()
            plan = host.plan_sop_route(
                tenant_id=principal.tenant_id, actor_user_id=principal.actor_user.id,
                agent_id=agent_id, message=body.message, session=session,
                routing_skills=routing_skills, conversation_context=body.conversation_context,
            )
        except RuntimeError as exc:
            raise PublicAPIError(503, "PUBLIC_HOST_SOP_ROUTE_UNAVAILABLE", str(exc)) from exc
    else:
        model_config = model_for_agent(db, principal.tenant_id, agent_id)
        if model_config is None:
            raise PublicAPIError(409, "MODEL_NOT_CONFIGURED", "No enabled default model is configured.")
        plan = TurnPlanner().plan(
            body.message, session, routing_skills, model_config,
            body.conversation_context, None, [],
        )
    selected_frame = next(
        (
            frame
            for frame in plan.task_frames
            if frame.kind == "sop" and frame.target_skill_id
        ),
        None,
    )
    return {
        "decision": plan.decision,
        "selected_sop_id": selected_frame.target_skill_id if selected_frame else None,
        "target_step_id": selected_frame.target_step_id if selected_frame else None,
        "confidence": plan.confidence,
        "user_intent": plan.user_intent,
        "reason": plan.reason,
        "clarification_question": plan.clarification_question,
        "candidate_sop_ids": [skill.skill_id for skill in routing_skills],
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
    ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    if body.base_version is not None:
        sop_id = str(body.content.get("skill_id") or "")
        selected = _version_payload(db, principal, agent_id, sop_id, body.base_version)
        if selected.get("status") not in {"published", "active"}:
            raise PublicAPIError(404, "SOP_VERSION_NOT_FOUND", "Published SOP version not found.")
    try:
        row = _new_draft(
            db,
            tenant_id=principal.tenant_id,
            agent_id=agent_id,
            credential_id=principal.credential_id,
            content=body.content,
            source="structured",
            base_version=body.base_version,
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
        agent_id=str(job.agent_id),
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
        runtime = _runtime_skill(db, principal.tenant_id, sop_id, agent_id)
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
        agent_id=str(job.agent_id),
        current_skill=SkillCard.model_validate(payload["current_skill"]),
        instruction=str(payload["instruction"]),
        target_paths=list(payload.get("target_paths") or []),
        model_config_id=payload.get("model_config_id"),
    )
    model = internal_skills._get_request_model(db, job.tenant_id, request.model_config_id)
    result = SkillEditor().rewrite(
        internal_skills._with_available_context_for_rewrite(db, request),
        model,
    )
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
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    require_agent_scope_viewer(principal.tenant_id, agent_id, principal.actor_user, db)
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
    enforce_agent_access(principal, agent_id, write=True)
    ensure_public_agent(db, principal, agent_id)
    ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
    row = _owned_draft(db, principal, agent_id, sop_id, draft_id)
    _require_etag(row, if_match)
    try:
        card = SkillCard.model_validate(body.content)
    except ValidationError as exc:
        raise PublicAPIError(422, "INVALID_SOP", "The SOP graph is invalid.", errors=exc.errors()) from exc
    if card.skill_id != sop_id:
        raise PublicAPIError(422, "SOP_ID_IMMUTABLE", "skill_id cannot be changed.")
    row.content_json = _normalize_draft(card.model_dump(mode="json"))
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
    enforce_agent_access(principal, agent_id, write=True)
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
    row.content_json = _normalize_draft(card.model_dump(mode="json"))
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
    principal: PublicPrincipal = Depends(require_scopes("sops:write")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id, write=True)
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
    ensure_public_agent(db, principal, agent_id)
    ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
    row = _owned_draft(db, principal, agent_id, sop_id, body.draft_id)
    validation = validate_draft(db, row)
    if not validation["valid"]:
        raise PublicAPIError(422, "SOP_VALIDATION_FAILED", "The SOP cannot be published.", errors=validation["errors"])
    card = skill_card_from_persisted(row.content_json)
    runtime = _runtime_skill(db, principal.tenant_id, sop_id, agent_id)
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
    ensure_public_agent(db, principal, agent_id)
    ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
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
    ensure_public_agent(db, principal, agent_id)
    require_agent_scope_viewer(principal.tenant_id, agent_id, principal.actor_user, db)
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
    ensure_public_agent(db, principal, agent_id)
    require_agent_scope_viewer(principal.tenant_id, agent_id, principal.actor_user, db)
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
    enforce_agent_access(principal, agent_id)
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
    ensure_public_agent(db, principal, agent_id)
    ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
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
