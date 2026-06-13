from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.agents.schema import (
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileRead,
    AgentProfileUpdateRequest,
    AgentResourceBindingInput,
    AgentResourceImportRequest,
    AgentResourceBindingRead,
    AgentResourcesUpdateRequest,
    AgentScopeRead,
    AgentSkillRollbackRequest,
)
from app.agents.branching import (
    branch_versions,
    copy_overall_scope_to_agent,
    ensure_agent_skill_branch,
    ensure_knowledge_base_version,
    get_overall_agent,
    promote_branch_to_overall,
    promote_knowledge_branch_to_overall,
    rollback_branch,
    sync_branch_from_overall,
    visible_skill_rows,
)
from app.db import get_session
from app.db.models import (
    AgentModelBinding,
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    utc_now,
)
from app.security.tenant import ensure_tenant

enterprise_router = APIRouter(prefix="/api/enterprise/agents", tags=["enterprise:agents"])
chat_router = APIRouter(prefix="/api/chat/agents", tags=["chat:agents"])
scope_router = APIRouter(prefix="/api/enterprise/agent-scope", tags=["enterprise:agent-scope"])


@scope_router.get("", response_model=AgentScopeRead)
def get_agent_scope(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> AgentScopeRead:
    ensure_tenant(db, tenant_id)
    return AgentScopeRead(tenant_id=tenant_id, agents=list_agents(tenant_id, db))


@enterprise_router.get("", response_model=list[AgentProfileRead])
def list_agents(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> list[AgentProfileRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile).where(AgentProfile.tenant_id == tenant_id).order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())
    ).all()
    bindings = _bindings_by_agent(db, tenant_id)
    return [agent_read(row, bindings.get(row.id, [])) for row in rows]


@enterprise_router.post("", response_model=AgentProfileRead)
def create_agent(request: AgentProfileCreateRequest, db: Session = Depends(get_session)) -> AgentProfileRead:
    ensure_tenant(db, request.tenant_id)
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name cannot be empty")
    existing = db.exec(
        select(AgentProfile).where(AgentProfile.tenant_id == request.tenant_id, AgentProfile.name == name)
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
        metadata_json=request.metadata or {},
    )
    db.add(row)
    db.flush()
    if not row.is_overall:
        copy_from_agent_id = request.copy_from_agent_id
        if request.source_mode == "blank":
            pass
        elif copy_from_agent_id:
            source_agent = _get_agent(db, request.tenant_id, copy_from_agent_id)
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
def get_agent(agent_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> AgentProfileRead:
    row = _get_agent(db, tenant_id, agent_id)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.put("/{agent_id}", response_model=AgentProfileRead)
def update_agent(agent_id: str, request: AgentProfileUpdateRequest, db: Session = Depends(get_session)) -> AgentProfileRead:
    row = _get_agent(db, request.tenant_id, agent_id)
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
    if request.metadata is not None:
        row.metadata_json = request.metadata
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.delete("/{agent_id}")
def delete_agent(agent_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> dict[str, str]:
    row = _get_agent(db, tenant_id, agent_id)
    if row.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent cannot be deleted")
    bindings = db.exec(select(AgentResourceBinding).where(AgentResourceBinding.agent_id == row.id)).all()
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
) -> list[AgentResourceBindingRead]:
    _get_agent(db, tenant_id, agent_id)
    rows = db.exec(
        select(AgentResourceBinding)
        .where(AgentResourceBinding.tenant_id == tenant_id, AgentResourceBinding.agent_id == agent_id)
        .order_by(AgentResourceBinding.resource_type, AgentResourceBinding.created_at)
    ).all()
    return [binding_read(row) for row in rows]


@enterprise_router.put("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def update_agent_resources(
    agent_id: str,
    request: AgentResourcesUpdateRequest,
    db: Session = Depends(get_session),
) -> list[AgentResourceBindingRead]:
    agent = _get_agent(db, request.tenant_id, agent_id)
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
    return get_agent_resources(agent_id, request.tenant_id, db)


@enterprise_router.post("/{agent_id}/resources/import")
def import_agent_resources(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session = Depends(get_session),
) -> dict[str, object]:
    target_agent = _get_agent(db, request.tenant_id, agent_id)
    source_agent = _get_agent(db, request.tenant_id, request.source_agent_id)
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
        source_binding = _source_resource_binding(db, request.tenant_id, source_agent, request.resource_type, resolved.id)
        if not source_agent.is_overall and not source_binding:
            missing.append({"resource_id": identifier, "reason": "not_visible_in_source_agent"})
            continue
        if target_agent.is_overall:
            _import_resource_to_overall(db, request.tenant_id, source_agent, request.resource_type, resolved)
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
) -> list[dict[str, object]]:
    _get_agent(db, tenant_id, agent_id)
    return [_skill_branch_read(skill) for skill in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)]


@enterprise_router.post("/{agent_id}/skills/{skill_id}/sync-from-overall")
def sync_agent_skill_from_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    agent = _get_agent(db, tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    skill = _get_global_skill(db, tenant_id, skill_id)
    branch = sync_branch_from_overall(db, tenant_id, agent_id, skill)
    db.commit()
    return {"status": "synced", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/promote-to-overall")
def promote_agent_skill_to_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    agent = _get_agent(db, tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent does not have a branch to promote")
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
) -> dict[str, object]:
    agent = _get_agent(db, request.tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Use the global skill rollback endpoint for overall agent")
    branch = rollback_branch(db, request.tenant_id, agent_id, skill_id, request.version)
    db.commit()
    return {"status": "rolled_back", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.get("/{agent_id}/skills/{skill_id}/versions")
def list_agent_skill_versions(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> list[dict[str, object]]:
    _get_agent(db, tenant_id, agent_id)
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
) -> dict[str, object]:
    _get_agent(db, request.tenant_id, agent_id)
    for item in request.bindings:
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
    db.commit()
    return {"status": "updated", "agent_id": agent_id}


@chat_router.get("", response_model=list[AgentProfileRead])
def list_chat_agents(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> list[AgentProfileRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        ).order_by(AgentProfile.updated_at.desc())
    ).all()
    bindings = _bindings_by_agent(db, tenant_id)
    return [agent_read(row, bindings.get(row.id, [])) for row in rows]


def agent_read(row: AgentProfile, bindings: list[AgentResourceBinding]) -> AgentProfileRead:
    return AgentProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        persona_prompt=row.persona_prompt,
        is_overall=row.is_overall,
        status=row.status,
        metadata=row.metadata_json or {},
        resources=[binding_read(binding) for binding in bindings],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def binding_read(row: AgentResourceBinding) -> AgentResourceBindingRead:
    return AgentResourceBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        status=row.status,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _copy_agent_scope_from_source(db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile) -> None:
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


def _import_resource_to_overall(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: Skill | GeneralSkill | KnowledgeBase,
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
    resolved: Skill | GeneralSkill | KnowledgeBase,
    source_binding: AgentResourceBinding | None,
) -> None:
    status = source_binding.status if source_binding else "active"
    metadata = dict(source_binding.metadata_json or {}) if source_binding else {}
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


def _copy_or_update_skill_branch(db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill) -> None:
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


def _ensure_copied_skill_branch_version(db: Session, branch: AgentSkillBranch, change_summary: str) -> None:
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


def _resource_display_id(resource_type: str, resolved: Skill | GeneralSkill | KnowledgeBase) -> str:
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.skill_id
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.slug
    return resolved.id


def _copy_agent_models_from_source(db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile) -> None:
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


def _copy_skill_branch(db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill) -> None:
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


def _copy_knowledge_branch(db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, kb: KnowledgeBase) -> None:
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


def _resolve_resource(db: Session, tenant_id: str, resource_type: str, identifier: str) -> Skill | GeneralSkill | KnowledgeBase | None:
    if resource_type == "skill":
        return db.get(Skill, identifier) or db.exec(select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == identifier)).first()
    if resource_type == "general_skill":
        return db.get(GeneralSkill, identifier) or db.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == identifier)
        ).first()
    if resource_type == "knowledge_base":
        return db.get(KnowledgeBase, identifier) or db.exec(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == identifier)
        ).first()
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
    grouped: dict[str, list[AgentResourceBinding]] = {}
    for row in rows:
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


def _ensure_resource_exists(db: Session, tenant_id: str, item: AgentResourceBindingInput) -> None:
    model = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
    }[item.resource_type]
    row = db.get(model, item.resource_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=f"Resource not found: {item.resource_type}:{item.resource_id}")


def _get_global_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    row = db.exec(select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)).first()
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
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }
