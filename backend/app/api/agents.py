from __future__ import annotations

from datetime import UTC, datetime
from time import sleep
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.agents.schema import (
    AgentModelsUpdateRequest,
    AgentAPICredentialCreateRequest,
    AgentAPICredentialCreated,
    AgentAPICredentialRead,
    AgentProfileCreateRequest,
    AgentProfileRead,
    AgentProfileUpdateRequest,
    AgentResourceBindingInput,
    AgentResourceImportRequest,
    AgentResourceBindingRead,
    AgentResourcesUpdateRequest,
    AgentScopeRead,
    AgentSkillRollbackRequest,
    AgentWorkRecordEventRead,
    AgentWorkRecordRead,
    AgentWorkRecordReplyStatsRead,
)
from app.agents.branching import (
    agent_private_metadata,
    branch_versions,
    copy_overall_scope_to_agent,
    ensure_agent_skill_branch,
    ensure_knowledge_base_version,
    get_overall_agent,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    promote_branch_to_overall,
    promote_knowledge_branch_to_overall,
    rollback_branch,
    sync_branch_from_overall,
    visible_skill_rows,
)
from app.db import get_session
from app.db.models import (
    APIClient,
    APICredential,
    AgentModelBinding,
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    AgentUsage,
    ChatSession,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    ModelConfig,
    ScheduledTask,
    Skill,
    Tool,
    utc_now,
    User,
)
from app.public_api.auth import generate_api_key
from app.public_api.credential_profiles import (
    AGENT_KEY_ALLOWED_SCOPES,
    agent_access_for_scopes,
    scopes_for_agent_access,
)
from app.security.auth import get_current_user
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant

IMPORT_LOCK_RETRY_ATTEMPTS = 2
IMPORT_LOCK_RETRY_DELAY_SECONDS = 0.5

enterprise_router = APIRouter(prefix="/api/enterprise/agents", tags=["enterprise:agents"])
chat_router = APIRouter(prefix="/api/chat/agents", tags=["chat:agents"])
scope_router = APIRouter(prefix="/api/enterprise/agent-scope", tags=["enterprise:agent-scope"])

STAFFDECK_AGENT_API_CLIENT_NAME = "StaffDeck 员工 API 密钥"


@scope_router.get("", response_model=AgentScopeRead)
def get_agent_scope(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentScopeRead:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    return AgentScopeRead(tenant_id=tenant_id, agents=list_agents(tenant_id, db, current_user))


@enterprise_router.get("", response_model=list[AgentProfileRead])
def list_agents(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentProfileRead]:
    ensure_tenant(db, tenant_id)
    user = current_user
    _ensure_request_tenant(tenant_id, user)
    rows = db.exec(
        select(AgentProfile)
        .where(AgentProfile.tenant_id == tenant_id)
        .order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    if not _is_admin_user(user):
        # Non-admin users still need the overall agent as a read-only open-gallery
        # source for copy/use flows. Mutations remain guarded by manage/update
        # endpoints, so this only exposes the source scope.
        rows = [row for row in rows if row.is_overall or _agent_visible_to_user(row, user)]
    bindings = _bindings_by_agent(db, tenant_id)
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, user)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@enterprise_router.post("", response_model=AgentProfileRead)
def create_agent(
    request: AgentProfileCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    ensure_tenant(db, request.tenant_id)
    user = current_user
    _ensure_request_tenant(request.tenant_id, user)
    if request.is_overall and not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can create overall agent")
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name cannot be empty")
    existing = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == request.tenant_id, AgentProfile.name == name
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent name already exists")
    row = AgentProfile(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        persona_prompt=request.persona_prompt,
        is_overall=request.is_overall,
        status="active",
        harness_max_actions=request.harness_max_actions,
        metadata_json=_metadata_with_creator(request.metadata or {}, user),
    )
    db.add(row)
    db.flush()
    if not row.is_overall:
        copy_from_agent_id = request.copy_from_agent_id
        if request.source_mode == "blank":
            pass
        elif copy_from_agent_id:
            source_agent = _get_agent(db, request.tenant_id, copy_from_agent_id)
            _ensure_can_copy_from_agent(source_agent, user)
            if not row.persona_prompt:
                row.persona_prompt = source_agent.persona_prompt
            _copy_agent_scope_from_source(db, request.tenant_id, source_agent, row)
        else:
            overall = get_overall_agent(db, request.tenant_id)
            if overall and not row.persona_prompt:
                row.persona_prompt = overall.persona_prompt
            copy_overall_scope_to_agent(db, request.tenant_id, row)
            if overall:
                _copy_agent_models_from_source(db, request.tenant_id, overall, row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.get("/{agent_id}", response_model=AgentProfileRead)
def get_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(row, current_user)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.get(
    "/{agent_id}/api-credentials",
    response_model=list[AgentAPICredentialRead],
)
def list_agent_api_credentials(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentAPICredentialRead]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    rows = db.exec(
        select(APICredential)
        .where(
            APICredential.tenant_id == tenant_id,
            APICredential.agent_id == agent_id,
        )
        .order_by(APICredential.created_at.desc())
    ).all()
    return [_agent_api_credential_read(row) for row in rows]


@enterprise_router.post(
    "/{agent_id}/api-credentials",
    response_model=AgentAPICredentialCreated,
)
def create_agent_api_credential(
    agent_id: str,
    request: AgentAPICredentialCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialCreated:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Open gallery cannot own an employee API key")
    client = _ensure_staffdeck_agent_api_client(db, request.tenant_id, current_user)
    token, prefix, digest = generate_api_key()
    row = APICredential(
        tenant_id=request.tenant_id,
        client_id=client.id,
        agent_id=agent_id,
        name=request.name.strip(),
        key_prefix=prefix,
        key_digest=digest,
        scopes_json=scopes_for_agent_access(request.access),
        expires_at=request.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AgentAPICredentialCreated(
        **_agent_api_credential_read(row).model_dump(),
        api_key=token,
    )


@enterprise_router.post(
    "/{agent_id}/api-credentials/{credential_id}/rotate",
    response_model=AgentAPICredentialCreated,
)
def rotate_agent_api_credential(
    agent_id: str,
    credential_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialCreated:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    row = _get_agent_api_credential(db, tenant_id, agent_id, credential_id)
    token, prefix, digest = generate_api_key()
    row.key_prefix = prefix
    row.key_digest = digest
    row.status = "active"
    row.revoked_at = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return AgentAPICredentialCreated(
        **_agent_api_credential_read(row).model_dump(),
        api_key=token,
    )


@enterprise_router.post(
    "/{agent_id}/api-credentials/{credential_id}/revoke",
    response_model=AgentAPICredentialRead,
)
def revoke_agent_api_credential(
    agent_id: str,
    credential_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialRead:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    row = _get_agent_api_credential(db, tenant_id, agent_id, credential_id)
    row.status = "revoked"
    row.revoked_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _agent_api_credential_read(row)


@enterprise_router.get("/{agent_id}/work-record", response_model=AgentWorkRecordRead)
def get_agent_work_record(
    agent_id: str,
    tenant_id: str = Query(...),
    timezone: str = Query("Asia/Shanghai"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentWorkRecordRead:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    try:
        local_timezone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    now = utc_now()
    reply_rows = db.exec(
        select(Message)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == "assistant",
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == current_user.id,
        )
        .order_by(Message.created_at.asc())
    ).all()
    by_day: dict[str, int] = {}
    events = [
        AgentWorkRecordEventRead(
            id=f"{message.id}:reply",
            kind="chat",
            phase="reply",
            timestamp=_iso_utc(message.created_at),
            label="对话回复",
        )
        for message in reply_rows
    ]
    for message in reply_rows:
        day = _as_utc(message.created_at).astimezone(local_timezone).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    events.extend(_agent_resource_timeline_events(db, tenant_id, agent_id))
    events.extend(_agent_scheduled_task_timeline_events(db, tenant_id, agent_id, current_user))
    events.sort(key=lambda item: (item.timestamp, item.id))
    today = _as_utc(now).astimezone(local_timezone).date().isoformat()
    return AgentWorkRecordRead(
        agent_id=agent_id,
        timezone=timezone,
        generated_at=_iso_utc(now),
        reply_stats=AgentWorkRecordReplyStatsRead(
            total=len(reply_rows),
            today=by_day.get(today, 0),
            by_day=dict(sorted(by_day.items())),
        ),
        events=events,
    )


@enterprise_router.put("/{agent_id}", response_model=AgentProfileRead)
def update_agent(
    agent_id: str,
    request: AgentProfileUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    row = _get_agent(db, request.tenant_id, agent_id)
    user = current_user
    _ensure_can_manage_agent(row, user)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Agent name cannot be empty")
        conflict = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == request.tenant_id,
                AgentProfile.name == name,
                AgentProfile.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Agent name already exists")
        row.name = name
    if request.description is not None:
        row.description = request.description
    if request.persona_prompt is not None:
        row.persona_prompt = request.persona_prompt
    if request.status is not None:
        row.status = request.status
    if request.harness_max_actions is not None:
        row.harness_max_actions = request.harness_max_actions
    if request.metadata is not None:
        row.metadata_json = _metadata_preserving_creator(
            row.metadata_json or {}, request.metadata, user
        )
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.delete("/{agent_id}")
def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    if row.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent cannot be deleted")
    bindings = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.agent_id == row.id)
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@enterprise_router.get("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def get_agent_resources(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    rows = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id, AgentResourceBinding.agent_id == agent_id
        )
        .order_by(AgentResourceBinding.resource_type, AgentResourceBinding.created_at)
    ).all()
    return [binding_read(row) for row in rows]


@enterprise_router.put("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def update_agent_resources(
    agent_id: str,
    request: AgentResourcesUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent uses the global resource pool")
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == request.tenant_id,
            AgentResourceBinding.agent_id == agent_id,
        )
    ).all()
    by_key = {(row.resource_type, row.resource_id): row for row in existing}
    desired_keys: set[tuple[str, str]] = set()
    for item in request.resources:
        _ensure_resource_exists(db, request.tenant_id, item)
        key = (item.resource_type, item.resource_id)
        desired_keys.add(key)
        row = by_key.get(key)
        if row:
            row.status = item.status
            row.metadata_json = item.metadata
            row.updated_at = utc_now()
        else:
            row = AgentResourceBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                status=item.status,
                metadata_json=item.metadata,
            )
        db.add(row)
    for key, row in by_key.items():
        if key not in desired_keys:
            db.delete(row)
    db.commit()
    return get_agent_resources(agent_id, request.tenant_id, db, current_user)


@enterprise_router.post("/{agent_id}/resources/import")
def import_agent_resources(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    for attempt in range(IMPORT_LOCK_RETRY_ATTEMPTS):
        try:
            return _import_agent_resources_once(agent_id, request, db, current_user)
        except OperationalError as exc:
            db.rollback()
            if not _is_database_locked_error(exc) or attempt >= IMPORT_LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep(IMPORT_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise HTTPException(status_code=503, detail="Resource import is temporarily busy")


def _import_agent_resources_once(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session,
    current_user: User | object,
) -> dict[str, object]:
    target_agent = _get_agent(db, request.tenant_id, agent_id)
    source_agent = _get_agent(db, request.tenant_id, request.source_agent_id)
    user = current_user
    _ensure_can_import_to_agent(target_agent, user)
    _ensure_can_copy_from_agent(source_agent, user)
    if source_agent.id == target_agent.id:
        raise HTTPException(status_code=400, detail="Source and target agent cannot be the same")
    resource_ids = _dedupe_ids(request.resource_ids)
    if not resource_ids:
        raise HTTPException(status_code=400, detail="No resources selected")
    imported: list[dict[str, object]] = []
    missing: list[dict[str, str]] = []
    for identifier in resource_ids:
        resolved = _resolve_resource(db, request.tenant_id, request.resource_type, identifier)
        if not resolved:
            missing.append({"resource_id": identifier, "reason": "resource_not_found"})
            continue
        source_binding = _source_resource_binding(
            db, request.tenant_id, source_agent, request.resource_type, resolved.id
        )
        if not source_agent.is_overall and not source_binding:
            missing.append({"resource_id": identifier, "reason": "not_visible_in_source_agent"})
            continue
        block_reason = _blocked_learning_reason(
            db,
            request.tenant_id,
            source_agent,
            request.resource_type,
            resolved,
            source_binding,
        )
        if block_reason:
            missing.append({"resource_id": identifier, "reason": block_reason})
            continue
        if target_agent.is_overall:
            _import_resource_to_overall(
                db, request.tenant_id, source_agent, request.resource_type, resolved
            )
        else:
            _upsert_imported_resource_binding(
                db,
                request.tenant_id,
                source_agent,
                target_agent,
                request.resource_type,
                resolved,
                source_binding,
            )
        imported.append(
            {
                "resource_type": request.resource_type,
                "resource_id": resolved.id,
                "display_id": _resource_display_id(request.resource_type, resolved),
                "name": getattr(resolved, "name", getattr(resolved, "slug", resolved.id)),
            }
        )
    db.commit()
    return {
        "status": "imported",
        "target_agent_id": target_agent.id,
        "source_agent_id": source_agent.id,
        "imported": imported,
        "missing": missing,
    }


@enterprise_router.get("/{agent_id}/skills")
def get_agent_skills(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        _skill_branch_read(skill)
        for skill in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    ]


@enterprise_router.post("/{agent_id}/skills/{skill_id}/sync-from-overall")
def sync_agent_skill_from_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    skill = _get_global_skill(db, tenant_id, skill_id)
    if skill.status != "published":
        raise HTTPException(
            status_code=400, detail="Disabled SOP cannot be learned from the open gallery"
        )
    branch = sync_branch_from_overall(db, tenant_id, agent_id, skill)
    db.commit()
    return {"status": "synced", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/promote-to-overall")
def promote_agent_skill_to_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _ensure_admin_user(tenant_id, current_user)
    agent = _get_agent(db, tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Overall agent does not have a branch to promote"
        )
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill_id,
        )
    ).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    skill = promote_branch_to_overall(db, tenant_id, branch)
    db.commit()
    return {"status": "promoted", "skill_id": skill_id, "version": skill.version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/rollback")
def rollback_agent_skill(
    agent_id: str,
    skill_id: str,
    request: AgentSkillRollbackRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Use the global skill rollback endpoint for overall agent"
        )
    branch = rollback_branch(db, request.tenant_id, agent_id, skill_id, request.version)
    db.commit()
    return {"status": "rolled_back", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.get("/{agent_id}/skills/{skill_id}/versions")
def list_agent_skill_versions(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "agent_id": row.agent_id,
            "skill_id": row.skill_id,
            "version": row.version,
            "base_version": row.base_version,
            "sync_state": row.sync_state,
            "status": row.status,
            "content": row.content_json,
            "change_summary": row.change_summary,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in branch_versions(db, tenant_id, agent_id, skill_id)
    ]


@enterprise_router.put("/{agent_id}/models")
def update_agent_models(
    agent_id: str,
    request: AgentModelsUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _ensure_can_manage_agent(_get_agent(db, request.tenant_id, agent_id), current_user)
    for item in request.bindings:
        if item.role != "default":
            continue
        existing = db.exec(
            select(AgentModelBinding).where(
                AgentModelBinding.tenant_id == request.tenant_id,
                AgentModelBinding.agent_id == agent_id,
                AgentModelBinding.role == item.role,
            )
        ).first()
        if existing:
            existing.model_config_id = item.model_config_id
            existing.updated_at = utc_now()
            db.add(existing)
            continue
        db.add(
            AgentModelBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                role=item.role,
                model_config_id=item.model_config_id,
            )
        )
    if not any(item.role == "default" for item in request.bindings):
        existing_default = db.exec(
            select(AgentModelBinding).where(
                AgentModelBinding.tenant_id == request.tenant_id,
                AgentModelBinding.agent_id == agent_id,
                AgentModelBinding.role == "default",
            )
        ).first()
        if existing_default:
            db.delete(existing_default)
    db.commit()
    return {"status": "updated", "agent_id": agent_id}


@enterprise_router.get("/{agent_id}/models", response_model=list[dict[str, object]])
def get_agent_models(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    rows = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == agent_id,
        )
    ).all()
    default = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    result = [{"role": row.role, "model_config_id": row.model_config_id, "effective": True} for row in rows]
    if not any(row["role"] == "default" for row in result) and default:
        result.append({"role": "default", "model_config_id": default.id, "effective": False})
    return result


@chat_router.get("", response_model=list[AgentProfileRead])
def list_chat_agents(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AgentProfileRead]:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
        .order_by(AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    rows = [
        row for row in rows if _chat_agent_selectable_to_user(row, current_user, used_agent_ids)
    ]
    bindings = _bindings_by_agent(db, tenant_id)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@chat_router.post("/{agent_id}/use", response_model=AgentProfileRead)
def use_chat_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentProfileRead:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    row = _get_agent(db, tenant_id, agent_id)
    if (
        row.is_overall
        or row.status != "active"
        or not _chat_agent_visible_to_user(row, current_user)
    ):
        raise HTTPException(status_code=403, detail="Cannot access this agent")
    _mark_agent_used(db, tenant_id, current_user, row.id)
    bindings = _bindings_by_agent(db, tenant_id)
    return agent_read(row, bindings.get(row.id, []), True)


def _agent_resource_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
) -> list[AgentWorkRecordEventRead]:
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    ids_by_type = {
        resource_type: {
            binding.resource_id
            for binding in bindings
            if binding.resource_type == resource_type
        }
        for resource_type in ("skill", "general_skill", "knowledge_base", "tool")
    }
    skills = {
        row.id: row
        for row in db.exec(
            select(Skill).where(
                Skill.tenant_id == tenant_id,
                Skill.id.in_(ids_by_type["skill"]),
            )
        ).all()
    } if ids_by_type["skill"] else {}
    general_skills = {
        row.id: row
        for row in db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.id.in_(ids_by_type["general_skill"]),
            )
        ).all()
    } if ids_by_type["general_skill"] else {}
    knowledge_bases = {
        row.id: row
        for row in db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(ids_by_type["knowledge_base"]),
            )
        ).all()
    } if ids_by_type["knowledge_base"] else {}
    tools = {
        row.id: row
        for row in db.exec(
            select(Tool).where(
                Tool.tenant_id == tenant_id,
                Tool.id.in_(ids_by_type["tool"]),
            )
        ).all()
    } if ids_by_type["tool"] else {}
    skill_branches = {
        row.skill_id: row
        for row in db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent_id,
            )
        ).all()
    }
    knowledge_branches = {
        row.knowledge_base_id: row
        for row in db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent_id,
            )
        ).all()
    }

    events: list[AgentWorkRecordEventRead] = []
    for binding in bindings:
        kind: str
        label: str
        if binding.resource_type == "skill":
            resource = skills.get(binding.resource_id)
            branch = skill_branches.get(resource.skill_id) if resource else None
            if not resource or resource.status != "published" or (branch and branch.status != "active"):
                continue
            kind, label = "sop", resource.name
        elif binding.resource_type == "general_skill":
            resource = general_skills.get(binding.resource_id)
            if not resource or resource.status != "published":
                continue
            kind, label = "skill", resource.name
        elif binding.resource_type == "knowledge_base":
            resource = knowledge_bases.get(binding.resource_id)
            branch = knowledge_branches.get(binding.resource_id)
            if not resource or resource.status != "active" or (branch and branch.status != "active"):
                continue
            kind, label = "knowledge", resource.name
        elif binding.resource_type == "tool":
            resource = tools.get(binding.resource_id)
            if not resource or not resource.enabled:
                continue
            kind, label = "tool", resource.display_name or resource.name
        else:
            continue
        events.append(
            AgentWorkRecordEventRead(
                id=f"{binding.id}:assigned",
                kind=kind,  # type: ignore[arg-type]
                phase="assigned",
                timestamp=_iso_utc(binding.created_at),
                label=label,
            )
        )
    return events


def _agent_scheduled_task_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
    current_user: User,
) -> list[AgentWorkRecordEventRead]:
    conditions = [
        ScheduledTask.tenant_id == tenant_id,
        ScheduledTask.agent_id == agent_id,
        ScheduledTask.status != "archived",
    ]
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTask.created_by_user_id == current_user.id)
    tasks = db.exec(select(ScheduledTask).where(*conditions)).all()
    events: list[AgentWorkRecordEventRead] = []
    for task in tasks:
        for phase, timestamp in (("last_run", task.last_run_at), ("next_run", task.next_run_at)):
            if not timestamp:
                continue
            events.append(
                AgentWorkRecordEventRead(
                    id=f"{task.id}:{phase}",
                    kind="task",
                    phase=phase,  # type: ignore[arg-type]
                    timestamp=_iso_utc(timestamp),
                    label=task.title,
                )
            )
    return events


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def agent_read(
    row: AgentProfile,
    bindings: list[AgentResourceBinding],
    used_by_current_user: bool | None = None,
) -> AgentProfileRead:
    metadata = dict(row.metadata_json or {})
    if used_by_current_user is not None:
        metadata["used_by_current_user"] = used_by_current_user
        metadata["chat_used_by_current_user"] = used_by_current_user
    return AgentProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        persona_prompt=row.persona_prompt,
        is_overall=row.is_overall,
        status=row.status,
        harness_max_actions=max(1, min(int(row.harness_max_actions or 32), 100)),
        metadata=metadata,
        resources=[binding_read(binding) for binding in bindings],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _is_database_locked_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _ensure_request_tenant(tenant_id: str, user: User) -> None:
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _ensure_staffdeck_agent_api_client(
    db: Session,
    tenant_id: str,
    current_user: User,
) -> APIClient:
    row = db.exec(
        select(APIClient).where(
            APIClient.tenant_id == tenant_id,
            APIClient.name == STAFFDECK_AGENT_API_CLIENT_NAME,
        )
    ).first()
    required_scopes = sorted({"credentials:write", *AGENT_KEY_ALLOWED_SCOPES})
    if row:
        if row.status != "active" or not set(required_scopes).issubset(row.scopes_json or []):
            row.status = "active"
            row.scopes_json = sorted(set(row.scopes_json or []) | set(required_scopes))
            row.updated_at = utc_now()
            db.add(row)
            db.flush()
        return row
    row = APIClient(
        tenant_id=tenant_id,
        name=STAFFDECK_AGENT_API_CLIENT_NAME,
        description="由数字员工设置页管理的单员工运行密钥。",
        scopes_json=required_scopes,
        created_by_user_id=current_user.id,
        metadata_json={"managed_by": "agent_settings"},
    )
    db.add(row)
    db.flush()
    return row


def _get_agent_api_credential(
    db: Session,
    tenant_id: str,
    agent_id: str,
    credential_id: str,
) -> APICredential:
    row = db.get(APICredential, credential_id)
    if not row or row.tenant_id != tenant_id or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Employee API credential not found")
    return row


def _agent_api_credential_read(row: APICredential) -> AgentAPICredentialRead:
    return AgentAPICredentialRead(
        id=row.id,
        agent_id=str(row.agent_id or ""),
        name=row.name,
        access=agent_access_for_scopes(list(row.scopes_json or [])),
        key_prefix=f"{row.key_prefix}…",
        scopes=list(row.scopes_json or []),
        status=row.status,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    if _agent_hidden_from_staffdeck(row):
        return False
    if _is_admin_user(user):
        return True
    if row.is_overall:
        return True
    metadata = row.metadata_json or {}
    return _agent_owned_by_user(row, user) or metadata.get("published_to_gallery") is True


def _agent_hidden_from_staffdeck(row: AgentProfile) -> bool:
    return (row.metadata_json or {}).get("hidden_from_staffdeck") is True


def _agent_published_to_gallery(row: AgentProfile) -> bool:
    return (row.metadata_json or {}).get("published_to_gallery") is True


def _used_agent_ids_for_user(db: Session, tenant_id: str, user: User) -> set[str]:
    usage_rows = db.exec(
        select(AgentUsage.agent_id).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id != None,  # noqa: E711
        )
    ).all()
    session_rows = db.exec(
        select(ChatSession.agent_id).where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.agent_id != None,  # noqa: E711
        )
    ).all()
    return {str(agent_id) for agent_id in [*usage_rows, *session_rows] if agent_id}


def _mark_agent_used(db: Session, tenant_id: str, user: User, agent_id: str) -> AgentUsage:
    row = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id == agent_id,
        )
    ).first()
    if row:
        row.updated_at = utc_now()
    else:
        row = AgentUsage(tenant_id=tenant_id, user_id=user.id, agent_id=agent_id)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.exec(
            select(AgentUsage).where(
                AgentUsage.tenant_id == tenant_id,
                AgentUsage.user_id == user.id,
                AgentUsage.agent_id == agent_id,
            )
        ).first()
        if not row:
            raise
    db.refresh(row)
    return row


def _chat_agent_selectable_to_user(row: AgentProfile, user: User, used_agent_ids: set[str]) -> bool:
    if row.is_overall:
        return False
    if _agent_owned_by_user(row, user):
        return True
    if _agent_published_to_gallery(row):
        return row.id in used_agent_ids
    return _is_admin_user(user)


def _ensure_can_access_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if not _agent_visible_to_user(row, user):
        raise HTTPException(status_code=403, detail="Cannot access this agent")


def _ensure_can_copy_from_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if row.is_overall or _agent_visible_to_user(row, user):
        return
    raise HTTPException(status_code=403, detail="Cannot copy resources from this agent")


def _ensure_can_manage_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if _is_admin_user(user):
        return
    if row.is_overall:
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if _agent_owned_by_user(row, user):
        return
    raise HTTPException(
        status_code=403, detail="Only the creator or administrator can manage this staff"
    )


def _ensure_can_import_to_agent(row: AgentProfile, user: User) -> None:
    if row.is_overall:
        _ensure_admin_user(row.tenant_id, user)
        return
    _ensure_can_manage_agent(row, user)


def _ensure_admin_user(tenant_id: str, user: User) -> None:
    _ensure_request_tenant(tenant_id, user)
    if not _is_admin_user(user):
        raise HTTPException(
            status_code=403, detail="Only administrator can update the open gallery"
        )


def _metadata_with_creator(metadata: dict[str, object], user: User) -> dict[str, object]:
    normalized = dict(metadata or {})
    display_name = user.display_name or user.username
    normalized["owner_user_id"] = user.id
    normalized["owner_username"] = user.username
    normalized["owner_display_name"] = display_name
    normalized["created_by_user_id"] = user.id
    normalized["created_by_username"] = user.username
    normalized["created_by"] = user.username
    normalized["created_by_display_name"] = display_name
    normalized["creator_name"] = user.username
    return normalized


def _metadata_preserving_creator(
    existing_metadata: dict[str, object],
    next_metadata: dict[str, object],
    user: User,
) -> dict[str, object]:
    normalized = dict(next_metadata or {})
    for key in (
        "owner_user_id",
        "owner_username",
        "owner_display_name",
        "created_by_user_id",
        "created_by_username",
        "created_by",
        "created_by_display_name",
        "creator_name",
    ):
        existing_value = existing_metadata.get(key)
        if isinstance(existing_value, str) and existing_value.strip():
            normalized[key] = existing_value
    return normalized


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    return _agent_visible_to_user(row, user)


def binding_read(row: AgentResourceBinding) -> AgentResourceBindingRead:
    return AgentResourceBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        status=row.status,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _copy_agent_scope_from_source(
    db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile
) -> None:
    if source.is_overall:
        copy_overall_scope_to_agent(db, tenant_id, target)
    else:
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == source.id,
            )
        ).all()
        for binding in bindings:
            _copy_resource_binding(db, tenant_id, source.id, target.id, binding)
    _copy_agent_models_from_source(db, tenant_id, source, target)


def _copy_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    binding: AgentResourceBinding,
) -> None:
    if binding.status != "active":
        return
    copied_binding = AgentResourceBinding(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        resource_type=binding.resource_type,
        resource_id=binding.resource_id,
        status=binding.status,
        metadata_json=dict(binding.metadata_json or {}),
    )
    db.add(copied_binding)
    if binding.resource_type == "skill":
        skill = db.get(Skill, binding.resource_id)
        if skill and skill.tenant_id == tenant_id:
            _copy_skill_branch(db, tenant_id, source_agent_id, target_agent_id, skill)
    elif binding.resource_type == "knowledge_base":
        kb = db.get(KnowledgeBase, binding.resource_id)
        if kb and kb.tenant_id == tenant_id:
            _copy_knowledge_branch(db, tenant_id, source_agent_id, target_agent_id, kb)


def _dedupe_ids(resource_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_id in resource_ids:
        resource_id = str(raw_id or "").strip()
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        deduped.append(resource_id)
    return deduped


def _source_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resource_id: str,
) -> AgentResourceBinding | None:
    if source_agent.is_overall:
        return None
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == source_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()


AgentResource = Skill | GeneralSkill | KnowledgeBase | Tool


def _blocked_learning_reason(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> str | None:
    if source_agent.is_overall:
        return (
            None
            if _open_gallery_resource_enabled(db, tenant_id, resource_type, resolved)
            else "disabled_in_open_gallery"
        )
    if not source_binding or source_binding.status != "active":
        return "inactive_in_source_agent"
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resolved.id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    return None


def _open_gallery_resource_enabled(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resolved: AgentResource,
) -> bool:
    if not is_open_gallery_resource(db, tenant_id, resource_type, resolved):
        return False
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.status == "published"
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.status == "published"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        return resolved.status == "active"
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.enabled
    return False


def _import_resource_to_overall(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
) -> None:
    if source_agent.is_overall:
        return
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch:
            promote_branch_to_overall(db, tenant_id, branch)
        return
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        promote_knowledge_branch_to_overall(db, tenant_id, source_agent.id, resolved.id)


def _upsert_imported_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    target_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> None:
    status = source_binding.status if source_binding else "active"
    metadata = agent_private_metadata(
        target_agent.id,
        dict(source_binding.metadata_json or {}) if source_binding else {},
    )
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == target_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resolved.id,
        )
    ).first()
    if existing:
        existing.status = status
        existing.metadata_json = metadata
        existing.updated_at = utc_now()
        db.add(existing)
    else:
        db.add(
            AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=target_agent.id,
                resource_type=resource_type,
                resource_id=resolved.id,
                status=status,
                metadata_json=metadata,
            )
        )
    if resource_type == "skill" and isinstance(resolved, Skill):
        _copy_or_update_skill_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)
    elif resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        _copy_or_update_knowledge_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)


def _copy_or_update_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not source_branch:
        branch = sync_branch_from_overall(db, tenant_id, target_agent_id, skill)
        _ensure_copied_skill_branch_version(db, branch, "导入自整体智能体")
        return
    with db.no_autoflush:
        target_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == target_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not target_branch:
        target_branch = AgentSkillBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=source_branch.skill_id,
            source_skill_id=source_branch.source_skill_id,
        )
    target_branch.base_version = source_branch.base_version
    target_branch.head_version = source_branch.head_version
    target_branch.content_json = dict(source_branch.content_json or {})
    target_branch.status = source_branch.status
    target_branch.sync_state = source_branch.sync_state
    target_branch.metadata_json = dict(source_branch.metadata_json or {})
    target_branch.updated_at = utc_now()
    db.add(target_branch)
    db.flush()
    _ensure_copied_skill_branch_version(db, target_branch, f"导入自 {source_agent_id}")


def _ensure_copied_skill_branch_version(
    db: Session, branch: AgentSkillBranch, change_summary: str
) -> None:
    existing = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == branch.head_version,
        )
    ).first()
    if existing:
        existing.content_json = dict(branch.content_json or {})
        existing.status = branch.status
        existing.sync_state = branch.sync_state
        existing.updated_at = utc_now()
        db.add(existing)
        return
    db.add(
        AgentSkillBranchVersion(
            tenant_id=branch.tenant_id,
            agent_id=branch.agent_id,
            skill_id=branch.skill_id,
            source_skill_id=branch.source_skill_id,
            version=branch.head_version,
            base_version=branch.base_version,
            content_json=dict(branch.content_json or {}),
            status=branch.status,
            sync_state=branch.sync_state,
            change_summary=change_summary,
        )
    )


def _copy_or_update_knowledge_branch(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    kb: KnowledgeBase,
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
        target_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == target_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
    if source_branch:
        base_version = source_branch.base_version
        head_version = source_branch.head_version
        status = source_branch.status
        sync_state = source_branch.sync_state
        metadata = dict(source_branch.metadata_json or {})
    else:
        version = ensure_knowledge_base_version(db, kb).version
        base_version = version
        head_version = version
        status = "active"
        sync_state = "synced"
        metadata = {}
    if not target_branch:
        target_branch = AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=kb.id,
        )
    target_branch.base_version = base_version
    target_branch.head_version = head_version
    target_branch.status = status
    target_branch.sync_state = sync_state
    target_branch.metadata_json = metadata
    target_branch.updated_at = utc_now()
    db.add(target_branch)


def _resource_display_id(resource_type: str, resolved: AgentResource) -> str:
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.skill_id
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.slug
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.name
    return resolved.id


def _copy_agent_models_from_source(
    db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile
) -> None:
    bindings = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == source.id,
        )
    ).all()
    for binding in bindings:
        db.add(
            AgentModelBinding(
                tenant_id=tenant_id,
                agent_id=target.id,
                role=binding.role,
                model_config_id=binding.model_config_id,
            )
        )


def _copy_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    source_branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == source_agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    if not source_branch:
        ensure_agent_skill_branch(db, tenant_id, target_agent_id, skill)
        return
    target_branch = AgentSkillBranch(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        skill_id=source_branch.skill_id,
        source_skill_id=source_branch.source_skill_id,
        base_version=source_branch.base_version,
        head_version=source_branch.head_version,
        content_json=dict(source_branch.content_json or {}),
        status=source_branch.status,
        sync_state=source_branch.sync_state,
        metadata_json=dict(source_branch.metadata_json or {}),
    )
    db.add(target_branch)
    db.flush()
    db.add(
        AgentSkillBranchVersion(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=target_branch.skill_id,
            source_skill_id=target_branch.source_skill_id,
            version=target_branch.head_version,
            base_version=target_branch.base_version,
            content_json=dict(target_branch.content_json or {}),
            status=target_branch.status,
            sync_state=target_branch.sync_state,
            change_summary=f"复制自 {source_agent_id}",
        )
    )


def _copy_knowledge_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, kb: KnowledgeBase
) -> None:
    source_branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == source_agent_id,
            AgentKnowledgeBranch.knowledge_base_id == kb.id,
        )
    ).first()
    if not source_branch:
        return
    db.add(
        AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=source_branch.knowledge_base_id,
            base_version=source_branch.base_version,
            head_version=source_branch.head_version,
            status=source_branch.status,
            sync_state=source_branch.sync_state,
            metadata_json=dict(source_branch.metadata_json or {}),
        )
    )


def _resolve_resource(
    db: Session, tenant_id: str, resource_type: str, identifier: str
) -> AgentResource | None:
    if resource_type == "skill":
        return (
            db.get(Skill, identifier)
            or db.exec(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == identifier)
            ).first()
        )
    if resource_type == "general_skill":
        return (
            db.get(GeneralSkill, identifier)
            or db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == identifier
                )
            ).first()
        )
    if resource_type == "knowledge_base":
        return (
            db.get(KnowledgeBase, identifier)
            or db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == identifier
                )
            ).first()
        )
    if resource_type == "tool":
        return (
            db.get(Tool, identifier)
            or db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == identifier)
            ).first()
        )
    return None


def _get_agent(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    ensure_tenant(db, tenant_id)
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


def _bindings_by_agent(db: Session, tenant_id: str) -> dict[str, list[AgentResourceBinding]]:
    rows = db.exec(
        select(AgentResourceBinding)
        .where(AgentResourceBinding.tenant_id == tenant_id)
        .order_by(AgentResourceBinding.created_at.asc())
    ).all()
    agent_ids = {row.agent_id for row in rows}
    agents_by_id = {
        row.id: row
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id.in_(agent_ids) if agent_ids else AgentProfile.id == "__none__",
            )
        ).all()
    }
    grouped: dict[str, list[AgentResourceBinding]] = {}
    for row in rows:
        if not _resource_binding_visible_in_agent_summary(
            db, tenant_id, agents_by_id.get(row.agent_id), row
        ):
            continue
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


def _resource_binding_visible_in_agent_summary(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    binding: AgentResourceBinding,
) -> bool:
    if not agent or binding.status == "deleted":
        return False

    model_by_type = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }
    model = model_by_type.get(binding.resource_type)
    if model is None:
        return False
    resource = db.get(model, binding.resource_id)
    if not resource or resource.tenant_id != tenant_id:
        return False
    if isinstance(resource, KnowledgeBase) and _is_empty_default_knowledge_base(
        db, tenant_id, resource
    ):
        return False

    if agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, binding.resource_type, resource):
            return False
    elif not is_bound_resource_visible_for_agent(
        db, tenant_id, binding.resource_type, resource, binding
    ):
        return False

    if isinstance(resource, Skill) and not agent.is_overall:
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == resource.skill_id,
            )
        ).first()
        if branch and branch.status == "deleted":
            return False
        skill_status = branch.status if branch else resource.status
        if binding.status == "active" and skill_status not in {"active", "published"}:
            return False

    if isinstance(resource, KnowledgeBase) and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resource.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).first()
        if not branch:
            return False
        if binding.status == "active" and branch.status != "active":
            return False

    if binding.status != "active":
        return True
    if isinstance(resource, Skill):
        return agent.is_overall is False or resource.status == "published"
    if isinstance(resource, GeneralSkill):
        return resource.status == "published"
    if isinstance(resource, KnowledgeBase):
        return resource.status == "active"
    if isinstance(resource, Tool):
        return resource.enabled
    return False


def _is_empty_default_knowledge_base(db: Session, tenant_id: str, kb: KnowledgeBase) -> bool:
    metadata = kb.metadata_json or {}
    has_runtime_rows = any(
        db.exec(
            select(model.id).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == kb.id,
            )
        ).first()
        for model in (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
    )
    if has_runtime_rows:
        return False
    if metadata.get("created_from_document_upload") and not metadata.get("source_document_id"):
        return True
    return kb.name == "默认知识库"


def _ensure_resource_exists(db: Session, tenant_id: str, item: AgentResourceBindingInput) -> None:
    model = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }[item.resource_type]
    row = db.get(model, item.resource_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(
            status_code=404, detail=f"Resource not found: {item.resource_type}:{item.resource_id}"
        )


def _get_global_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _skill_branch_read(skill: Skill) -> dict[str, object]:
    metadata = getattr(skill, "agent_branch_meta", {}) or {}
    content = skill.content_json or {}
    if not metadata and isinstance(content.get("metadata"), dict):
        metadata = content.get("metadata", {}).get("agent_branch", {}) or {}
    return {
        "id": skill.id,
        "tenant_id": skill.tenant_id,
        "skill_id": skill.skill_id,
        "version": skill.version,
        "name": skill.name,
        "business_domain": skill.business_domain,
        "description": skill.description,
        "content": skill.content_json,
        "status": skill.status,
        "agent_id": metadata.get("agent_id"),
        "branch_status": metadata.get("status"),
        "branch_sync_state": metadata.get("sync_state"),
        "branch_base_version": metadata.get("base_version"),
        "branch_head_version": metadata.get("head_version"),
        "metadata": dict(metadata.get("metadata") or {}),
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }
