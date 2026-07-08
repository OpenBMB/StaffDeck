from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from sqlmodel import Session, select

from app.db.models import (
    AgentKnowledgeBranch,
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    ModelConfig,
    Skill,
    SkillVersion,
    Tool,
    utc_now,
)


DEFAULT_AGENT_ROLES = ("default", "router", "step", "response", "general_skill")
OPEN_GALLERY_SCOPE = "open_gallery"
AGENT_PRIVATE_SCOPE = "agent_private"
SYSTEM_CREATOR_NAME = "admin"
STANDARD_CREATOR_METADATA_KEYS = (
    "creator_name",
    "created_by",
    "created_by_display_name",
    "created_by_username",
)
CREATOR_SOURCE_METADATA_KEYS = (
    "gallery_published_by",
    "owner_display_name",
    "owner_username",
    "created_by_user_id",
    "owner_user_id",
)
CREATOR_METADATA_KEYS = STANDARD_CREATOR_METADATA_KEYS + CREATOR_SOURCE_METADATA_KEYS


def _valid_creator_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return bool(normalized) and normalized != "系统" and normalized.lower() != "system"


def _metadata_creator_value(metadata: dict[str, Any]) -> str:
    owner_username = metadata.get("owner_username")
    if _valid_creator_value(owner_username):
        return owner_username.strip()
    for key in (
        "created_by_username",
        "created_by",
        "creator_name",
        "gallery_published_by",
    ):
        value = metadata.get(key)
        if _valid_creator_value(value) and value.strip() != SYSTEM_CREATOR_NAME:
            return value.strip()
    for key in (
        "created_by_username",
        "created_by",
        "owner_username",
        "creator_name",
        "gallery_published_by",
        "created_by_display_name",
        "owner_display_name",
        "created_by_user_id",
        "owner_user_id",
    ):
        value = metadata.get(key)
        if _valid_creator_value(value):
            return value.strip()
    return SYSTEM_CREATOR_NAME


def system_creator_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(extra or {})
    owner_username = metadata.get("owner_username")
    owner_creator = owner_username.strip() if _valid_creator_value(owner_username) else ""
    creator = owner_creator or _metadata_creator_value(metadata)
    should_replace_default = creator != SYSTEM_CREATOR_NAME
    should_replace_owner = bool(owner_creator)
    if not _valid_creator_value(metadata.get("creator_name")) or (
        should_replace_default and metadata.get("creator_name") == SYSTEM_CREATOR_NAME
    ) or (should_replace_owner and metadata.get("creator_name") != creator):
        metadata["creator_name"] = creator
    if not _valid_creator_value(metadata.get("created_by")) or (
        should_replace_default and metadata.get("created_by") == SYSTEM_CREATOR_NAME
    ) or (should_replace_owner and metadata.get("created_by") != creator):
        metadata["created_by"] = creator
    if not _valid_creator_value(metadata.get("created_by_username")) or (
        should_replace_default and metadata.get("created_by_username") == SYSTEM_CREATOR_NAME
    ) or (should_replace_owner and metadata.get("created_by_username") != creator):
        metadata["created_by_username"] = creator
    if not _valid_creator_value(metadata.get("created_by_user_id")):
        metadata["created_by_user_id"] = (
            metadata.get("owner_user_id")
            if _valid_creator_value(metadata.get("owner_user_id"))
            else creator
        )
    return metadata


def get_overall_agent(db: Session, tenant_id: str) -> AgentProfile | None:
    return db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.is_overall == True,  # noqa: E712
            AgentProfile.status != "archived",
        )
    ).first()


def get_agent(db: Session, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
    if not agent_id:
        return None
    return db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == agent_id,
            AgentProfile.status != "archived",
        )
    ).first()


def is_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> bool:
    agent = get_agent(db, tenant_id, agent_id)
    return bool(agent and agent.is_overall)


def require_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> None:
    if not agent_id and not get_overall_agent(db, tenant_id):
        return
    if not is_overall_agent(db, tenant_id, agent_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Only the overall agent can delete global resources")


def open_gallery_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = system_creator_metadata(extra)
    metadata["scope"] = OPEN_GALLERY_SCOPE
    metadata["visibility"] = OPEN_GALLERY_SCOPE
    metadata.pop("owner_agent_id", None)
    metadata.pop("created_from_agent", None)
    metadata.pop("created_from_upload", None)
    return metadata


def agent_private_metadata(agent_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(extra or {})
    metadata["scope"] = AGENT_PRIVATE_SCOPE
    metadata["visibility"] = AGENT_PRIVATE_SCOPE
    metadata["owner_agent_id"] = agent_id
    metadata["created_from_agent"] = True
    return metadata


def mark_resource_open_gallery(resource: object) -> None:
    if not hasattr(resource, "metadata_json"):
        return
    metadata = open_gallery_metadata(getattr(resource, "metadata_json", None) or {})
    setattr(resource, "metadata_json", metadata)


def mark_resource_private_for_agent(resource: object, agent_id: str) -> None:
    if not hasattr(resource, "metadata_json"):
        return
    metadata = agent_private_metadata(agent_id, getattr(resource, "metadata_json", None) or {})
    setattr(resource, "metadata_json", metadata)


def ensure_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
) -> None:
    overall = get_overall_agent(db, tenant_id)
    if overall:
        _ensure_binding(
            db,
            tenant_id,
            overall.id,
            resource_type,
            resource_id,
            status,
            metadata_json=open_gallery_metadata(),
        )


def hide_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    overall = get_overall_agent(db, tenant_id)
    if not overall:
        return False
    _ensure_binding(
        db,
        tenant_id,
        overall.id,
        resource_type,
        resource_id,
        "deleted",
        metadata_json=open_gallery_metadata(),
    )
    return True


def ensure_private_resource_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
) -> None:
    _ensure_binding(
        db,
        tenant_id,
        agent_id,
        resource_type,
        resource_id,
        status,
        metadata_json=agent_private_metadata(agent_id),
    )


def resource_binding_metadata(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    resource_type: str,
) -> dict[str, dict[str, Any]]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        agent = get_overall_agent(db, tenant_id)
    if not agent:
        return {}
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.status != "deleted",
        )
    ).all()
    return {binding.resource_id: dict(binding.metadata_json or {}) for binding in bindings}


def is_open_gallery_resource(db: Session, tenant_id: str, resource_type: str, resource: object) -> bool:
    resource_id = getattr(resource, "id", None)
    if not resource_id or getattr(resource, "tenant_id", None) != tenant_id:
        return False
    overall = get_overall_agent(db, tenant_id)
    if overall:
        overall_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == overall.id,
                AgentResourceBinding.resource_type == resource_type,
                AgentResourceBinding.resource_id == resource_id,
            )
        ).first()
        if overall_binding:
            return overall_binding.status != "deleted" and not _binding_is_private(overall_binding)
    metadata = _resource_metadata(resource)
    if _metadata_is_private(metadata):
        return False
    private_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).all()
    if any(_binding_is_private(binding) for binding in private_binding):
        return False
    return True


def project_skill_with_branch(
    skill: Skill,
    branch: AgentSkillBranch | None,
    binding_status: str | None = None,
) -> Skill:
    if not branch:
        return skill
    content = dict(branch.content_json or {})
    content["version"] = branch.head_version
    is_visible = branch.status == "active" and (binding_status in {None, "active"})
    metadata = {
        "agent_id": branch.agent_id,
        "base_version": branch.base_version,
        "head_version": branch.head_version,
        "sync_state": branch.sync_state,
        "status": branch.status,
        "binding_status": binding_status,
    }
    projected = Skill(
        id=skill.id,
        tenant_id=skill.tenant_id,
        skill_id=skill.skill_id,
        version=branch.head_version,
        name=str(branch.content_json.get("name") or skill.name),
        business_domain=branch.content_json.get("business_domain") or skill.business_domain,
        description=branch.content_json.get("description") or skill.description,
        content_json=content,
        status="published" if is_visible else "archived",
        created_at=skill.created_at,
        updated_at=branch.updated_at,
    )
    object.__setattr__(projected, "agent_branch_meta", metadata)
    return projected


def visible_skill_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Skill]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        status_clause = Skill.status != "deleted" if include_inactive else Skill.status == "published"
        rows = list(
            db.exec(
                select(Skill)
                .where(Skill.tenant_id == tenant_id, status_clause)
                .order_by(Skill.updated_at.desc())
            ).all()
        )
        metadata_by_id = resource_binding_metadata(db, tenant_id, agent.id if agent else None, "skill")
        visible_rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "skill", row)]
        for row in visible_rows:
            object.__setattr__(row, "agent_branch_meta", {"metadata": metadata_by_id.get(row.id, {})})
        return visible_rows
    rows: list[Skill] = []
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "skill",
        )
    ).all()
    for binding in bindings:
        if binding.status == "deleted":
            continue
        if not include_inactive and binding.status != "active":
            continue
        skill = db.get(Skill, binding.resource_id)
        if not skill or skill.tenant_id != tenant_id:
            continue
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
        if not include_inactive and branch.status != "active":
            continue
        rows.append(project_skill_with_branch(skill, branch, binding.status))
    return sorted(rows, key=lambda item: item.updated_at, reverse=True)


def visible_published_skills(db: Session, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
    return [skill for skill in visible_skill_rows(db, tenant_id, agent_id) if skill.status == "published"]


def visible_skill(db: Session, tenant_id: str, skill_id: str, agent_id: str | None = None) -> Skill | None:
    skill = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not skill or skill.status == "deleted":
        return None
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        if skill.status == "archived":
            return None
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            return None
        metadata_by_id = resource_binding_metadata(db, tenant_id, agent.id if agent else None, "skill")
        object.__setattr__(skill, "agent_branch_meta", {"metadata": metadata_by_id.get(skill.id, {})})
        return skill
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == skill.id,
            AgentResourceBinding.status == "active",
        )
    ).first()
    if not binding:
        return None
    branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    if branch.status != "active":
        return None
    return project_skill_with_branch(skill, branch)


def ensure_agent_skill_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    skill: Skill,
) -> AgentSkillBranch:
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    if branch:
        return branch
    branch = AgentSkillBranch(
        tenant_id=tenant_id,
        agent_id=agent_id,
        skill_id=skill.skill_id,
        source_skill_id=skill.id,
        base_version=skill.version,
        head_version=skill.version,
        content_json=dict(skill.content_json),
        status="active" if skill.status == "published" else "inactive",
        sync_state="synced",
    )
    db.add(branch)
    db.flush()
    _ensure_branch_version(db, branch, "初始化分支")
    return branch


def update_branch_skill(
    db: Session,
    tenant_id: str,
    agent_id: str,
    skill: Skill,
    content: dict[str, Any],
    change_summary: str = "分支改写",
) -> AgentSkillBranch:
    branch = ensure_agent_skill_branch(db, tenant_id, agent_id, skill)
    previous_status = branch.status
    next_version = next_unique_branch_version(db, branch, str(content.get("version") or ""))
    next_content = dict(content)
    next_content["version"] = next_version
    branch.content_json = next_content
    branch.head_version = next_version
    branch.status = previous_status
    branch.sync_state = "diverged"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, change_summary)
    return branch


def sync_branch_from_overall(db: Session, tenant_id: str, agent_id: str, skill: Skill) -> AgentSkillBranch:
    if skill.status != "published" or not is_open_gallery_resource(db, tenant_id, "skill", skill):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Disabled skill cannot be learned from the open gallery")
    branch = ensure_agent_skill_branch(db, tenant_id, agent_id, skill)
    branch.base_version = skill.version
    branch.head_version = skill.version
    branch.content_json = dict(skill.content_json)
    branch.status = "active" if skill.status == "published" else "inactive"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, "同步整体版本")
    return branch


def promote_branch_to_overall(db: Session, tenant_id: str, branch: AgentSkillBranch) -> Skill:
    skill = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == branch.skill_id)
    ).first()
    if not skill:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Skill not found")
    next_version = next_global_version(skill.version)
    content = dict(branch.content_json)
    content["version"] = next_version
    skill.version = next_version
    skill.name = str(content.get("name") or skill.name)
    skill.business_domain = content.get("business_domain") or skill.business_domain
    skill.description = content.get("description") or skill.description
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    mark_resource_open_gallery(skill)
    ensure_open_gallery_binding(db, tenant_id, "skill", skill.id, "active")
    db.add(
        SkillVersion(
            tenant_id=tenant_id,
            skill_id=skill.skill_id,
            version=next_version,
            name=skill.name,
            business_domain=skill.business_domain,
            description=skill.description,
            content_json=content,
            status="published",
        )
    )
    branch.base_version = next_version
    branch.head_version = next_version
    branch.content_json = content
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, "推送到整体")
    return skill


def rollback_branch(db: Session, tenant_id: str, agent_id: str, skill_id: str, version: str) -> AgentSkillBranch:
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill_id,
        )
    ).first()
    if not branch:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Branch not found")
    version_row = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == tenant_id,
            AgentSkillBranchVersion.agent_id == agent_id,
            AgentSkillBranchVersion.skill_id == skill_id,
            AgentSkillBranchVersion.version == version,
        )
    ).first()
    if not version_row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Branch version not found")
    branch.content_json = dict(version_row.content_json)
    branch.head_version = version_row.version
    branch.status = version_row.status
    branch.sync_state = "synced" if version_row.version == branch.base_version else "diverged"
    branch.updated_at = utc_now()
    return branch


def branch_versions(db: Session, tenant_id: str, agent_id: str, skill_id: str) -> list[AgentSkillBranchVersion]:
    return list(
        db.exec(
            select(AgentSkillBranchVersion)
            .where(
                AgentSkillBranchVersion.tenant_id == tenant_id,
                AgentSkillBranchVersion.agent_id == agent_id,
                AgentSkillBranchVersion.skill_id == skill_id,
            )
            .order_by(AgentSkillBranchVersion.created_at.desc())
        ).all()
    )


def visible_knowledge_base_ids(db: Session, tenant_id: str, agent_id: str | None = None) -> list[str]:
    return list(visible_knowledge_base_versions(db, tenant_id, agent_id).keys())


def visible_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
) -> dict[str, KnowledgeBaseVersion]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        rows = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.status != "archived",
            )
        ).all()
        rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "knowledge_base", row)]
        return {row.id: ensure_knowledge_base_version(db, row, _current_knowledge_version(row)) for row in rows}
    branches = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent.id,
            AgentKnowledgeBranch.status == "active",
        )
    ).all()
    result: dict[str, KnowledgeBaseVersion] = {}
    for branch in branches:
        kb = db.get(KnowledgeBase, branch.knowledge_base_id)
        if not kb or kb.tenant_id != tenant_id or kb.status == "deleted":
            continue
        result[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
    return result


def visible_knowledge_base_version_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
) -> list[str]:
    return [row.id for row in visible_knowledge_base_versions(db, tenant_id, agent_id).values()]


def ensure_knowledge_base_version(db: Session, kb: KnowledgeBase, version: str | None = None) -> KnowledgeBaseVersion:
    normalized_version = version or _current_knowledge_version(kb)
    row = db.exec(
        select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.tenant_id == kb.tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == kb.id,
            KnowledgeBaseVersion.version == normalized_version,
        )
    ).first()
    if row:
        return row
    row = KnowledgeBaseVersion(
        id=f"kbver_{kb.id}_{_safe_version_id(normalized_version)}",
        tenant_id=kb.tenant_id,
        knowledge_base_id=kb.id,
        version=normalized_version,
        name=kb.name,
        description=kb.description,
        status=kb.status,
        metadata_json=dict(kb.metadata_json or {}),
    )
    db.add(row)
    db.flush()
    return row


def knowledge_version_for_upload(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
) -> KnowledgeBaseVersion:
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        return ensure_knowledge_base_version(db, kb, _current_knowledge_version(kb))
    branch = _ensure_knowledge_branch(db, tenant_id, agent.id, kb)
    source_version = ensure_knowledge_base_version(db, kb, branch.head_version)
    next_version = _next_knowledge_branch_version(branch)
    target_version = ensure_knowledge_base_version(db, kb, next_version)
    clone_knowledge_version_assets(db, tenant_id, knowledge_base_id, source_version.id, target_version.id)
    branch.head_version = next_version
    branch.sync_state = "diverged"
    branch.status = "active"
    branch.updated_at = utc_now()
    return target_version


def ensure_agent_private_knowledge_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base: KnowledgeBase,
) -> AgentKnowledgeBranch:
    mark_resource_private_for_agent(knowledge_base, agent_id)
    ensure_private_resource_binding(db, tenant_id, agent_id, "knowledge_base", knowledge_base.id, "active")
    current_version = _current_knowledge_version(knowledge_base)
    ensure_knowledge_base_version(db, knowledge_base, current_version)
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, knowledge_base)
    branch.base_version = current_version
    branch.head_version = current_version
    branch.status = "active"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return branch


def sync_knowledge_branch_from_overall(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
) -> AgentKnowledgeBranch:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if kb.status != "active" or not is_open_gallery_resource(db, tenant_id, "knowledge_base", kb):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Disabled knowledge base cannot be learned from the open gallery")
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, kb)
    current_version = _current_knowledge_version(kb)
    ensure_knowledge_base_version(db, kb, current_version)
    branch.base_version = current_version
    branch.head_version = current_version
    branch.status = "active"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return branch


def promote_knowledge_branch_to_overall(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
) -> KnowledgeBaseVersion:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent_id,
            AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
        )
    ).first()
    if not branch:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge branch not found")
    source = ensure_knowledge_base_version(db, kb, branch.head_version)
    next_version = next_global_version(_current_knowledge_version(kb))
    target = ensure_knowledge_base_version(db, kb, next_version)
    target.name = source.name
    target.description = source.description
    target.metadata_json = dict(source.metadata_json or {})
    target.status = "active"
    target.updated_at = utc_now()
    _retag_knowledge_version(db, tenant_id, knowledge_base_id, source.id, target.id)
    kb.name = source.name
    kb.description = source.description
    kb.metadata_json = open_gallery_metadata({**(kb.metadata_json or {}), "current_version": next_version})
    kb.updated_at = utc_now()
    ensure_open_gallery_binding(db, tenant_id, "knowledge_base", kb.id, "active")
    branch.base_version = next_version
    branch.head_version = next_version
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return target


def rollback_knowledge_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
    version: str,
) -> AgentKnowledgeBranch:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    target = ensure_knowledge_base_version(db, kb, version)
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, kb)
    branch.head_version = target.version
    branch.status = "active"
    branch.sync_state = "synced" if target.version == branch.base_version else "diverged"
    branch.updated_at = utc_now()
    return branch


def model_for_agent(db: Session, tenant_id: str, agent_id: str | None, role: str = "default") -> ModelConfig | None:
    agent = get_agent(db, tenant_id, agent_id)
    roles: Iterable[str] = (role, "default") if role != "default" else ("default",)
    if agent:
        for candidate_role in roles:
            binding = db.exec(
                select(AgentModelBinding).where(
                    AgentModelBinding.tenant_id == tenant_id,
                    AgentModelBinding.agent_id == agent.id,
                    AgentModelBinding.role == candidate_role,
                )
            ).first()
            if binding:
                model = db.get(ModelConfig, binding.model_config_id)
                if model and model.enabled:
                    return model
    return db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()


def copy_overall_scope_to_agent(db: Session, tenant_id: str, agent: AgentProfile) -> None:
    skills = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
    ).all()
    for skill in skills:
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            continue
        _ensure_binding(db, tenant_id, agent.id, "skill", skill.id, _binding_status_from_resource_status(skill.status))
        ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    knowledge_bases = db.exec(
        select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.status == "active")
    ).all()
    for kb in knowledge_bases:
        if not is_open_gallery_resource(db, tenant_id, "knowledge_base", kb):
            continue
        _ensure_binding(db, tenant_id, agent.id, "knowledge_base", kb.id, _binding_status_from_resource_status(kb.status))
        branch = _ensure_knowledge_branch(db, tenant_id, agent.id, kb)
        branch.status = _binding_status_from_resource_status(kb.status)
        branch.updated_at = utc_now()
        db.add(branch)
    general_skills = db.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id, GeneralSkill.status == "published")
    ).all()
    for general_skill in general_skills:
        if not is_open_gallery_resource(db, tenant_id, "general_skill", general_skill):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "general_skill",
            general_skill.id,
            _binding_status_from_resource_status(general_skill.status),
        )
    copy_open_gallery_tools_to_agent(db, tenant_id, agent)


def copy_open_gallery_tools_to_agent(db: Session, tenant_id: str, agent: AgentProfile) -> None:
    tools = db.exec(select(Tool).where(Tool.tenant_id == tenant_id)).all()
    for tool in tools:
        if not is_open_gallery_resource(db, tenant_id, "tool", tool):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "tool",
            tool.id,
            "active" if tool.enabled else "inactive",
        )


def next_branch_version(version: str, requested_version: str | None = None) -> str:
    requested = (requested_version or "").strip()
    if _is_semver(requested) and requested != version:
        return requested
    base = version.partition("-branch.")[0]
    return next_global_version(base)


def next_unique_branch_version(db: Session, branch: AgentSkillBranch, requested_version: str | None = None) -> str:
    candidate = next_branch_version(branch.head_version, requested_version)
    while db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == candidate,
        )
    ).first():
        candidate = next_global_version(candidate.partition("-branch.")[0])
    return candidate


def next_global_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{version}.1"


def _is_semver(version: str) -> bool:
    parts = version.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _ensure_branch_version(db: Session, branch: AgentSkillBranch, change_summary: str) -> None:
    existing = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == branch.head_version,
        )
    ).first()
    if existing:
        return
    db.add(
        AgentSkillBranchVersion(
            tenant_id=branch.tenant_id,
            agent_id=branch.agent_id,
            skill_id=branch.skill_id,
            source_skill_id=branch.source_skill_id,
            version=branch.head_version,
            base_version=branch.base_version,
            content_json=dict(branch.content_json),
            status=branch.status,
            sync_state=branch.sync_state,
            change_summary=change_summary,
        )
    )


def _binding_status_from_resource_status(status: str | None) -> str:
    return "active" if status in {"active", "published"} else "inactive"


def _resource_metadata(resource: object) -> dict[str, Any]:
    metadata = getattr(resource, "metadata_json", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _metadata_is_private(metadata: dict[str, Any]) -> bool:
    return (
        metadata.get("scope") == AGENT_PRIVATE_SCOPE
        or metadata.get("visibility") == AGENT_PRIVATE_SCOPE
        or metadata.get("created_from_agent") is True
        or metadata.get("created_from_upload") is True
    )


def _binding_is_private(binding: AgentResourceBinding) -> bool:
    metadata = dict(binding.metadata_json or {})
    return _metadata_is_private(metadata)


def _ensure_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
) -> None:
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()
    if existing:
        existing.status = status
        if metadata_json is not None:
            existing.metadata_json = metadata_json
        existing.updated_at = utc_now()
        db.add(existing)
        return
    db.add(
        AgentResourceBinding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            metadata_json=metadata_json or {},
        )
    )


def _ensure_knowledge_branch(db: Session, tenant_id: str, agent_id: str, kb: KnowledgeBase) -> AgentKnowledgeBranch:
    branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent_id,
            AgentKnowledgeBranch.knowledge_base_id == kb.id,
        )
    ).first()
    if branch:
        return branch
    branch = AgentKnowledgeBranch(
        tenant_id=tenant_id,
        agent_id=agent_id,
        knowledge_base_id=kb.id,
        base_version="1.0.0",
        head_version="1.0.0",
        status=_binding_status_from_resource_status(kb.status),
        sync_state="synced",
    )
    db.add(branch)
    return branch


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


def _current_knowledge_version(kb: KnowledgeBase) -> str:
    metadata = kb.metadata_json or {}
    version = metadata.get("current_version") if isinstance(metadata, dict) else None
    return str(version or "1.0.0")


def _next_knowledge_branch_version(branch: AgentKnowledgeBranch) -> str:
    prefix = f"{branch.base_version}-branch.{_safe_version_id(branch.agent_id)}."
    if branch.head_version.startswith(prefix):
        suffix = branch.head_version.removeprefix(prefix)
        if suffix.isdigit():
            return f"{prefix}{int(suffix) + 1}"
    return f"{prefix}1"


def _retag_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    source_version_id: str,
    target_version_id: str,
) -> None:
    tables = (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk, KnowledgeConcept, KnowledgeDiscoverySuggestion)
    for model in tables:
        rows = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == knowledge_base_id,
                model.knowledge_base_version_id == source_version_id,
            )
        ).all()
        for row in rows:
            row.knowledge_base_version_id = target_version_id
            row.updated_at = utc_now()
            db.add(row)


def clone_knowledge_version_assets(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    source_version_id: str,
    target_version_id: str,
) -> None:
    if source_version_id == target_version_id:
        return

    document_id_map: dict[str, str] = {}
    bucket_id_map: dict[str, str] = {}
    source_documents = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == source_version_id,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).all()
    target_has_documents = db.exec(
        select(KnowledgeDocument.id).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == target_version_id,
        )
    ).first()

    if not target_has_documents:
        for document in source_documents:
            clone = KnowledgeDocument(
                tenant_id=document.tenant_id,
                knowledge_base_id=document.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                filename=document.filename,
                file_type=document.file_type,
                title=document.title,
                status=document.status,
                bucket_count=document.bucket_count,
                chunk_count=document.chunk_count,
                metadata_json=deepcopy(document.metadata_json or {}),
                error=document.error,
                created_at=document.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)
            db.flush()
            document_id_map[document.id] = clone.id

        source_buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        for bucket in source_buckets:
            clone = KnowledgeBucket(
                tenant_id=bucket.tenant_id,
                knowledge_base_id=bucket.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(bucket.document_id, bucket.document_id),
                bucket_key=bucket.bucket_key,
                title=bucket.title,
                summary=bucket.summary,
                token_estimate=bucket.token_estimate,
                metadata_json=deepcopy(bucket.metadata_json or {}),
                created_at=bucket.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)
            db.flush()
            bucket_id_map[bucket.id] = clone.id

        source_chunks = db.exec(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                KnowledgeChunk.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeChunk.bucket_id.asc(), KnowledgeChunk.chunk_index.asc())
        ).all()
        for chunk in source_chunks:
            clone = KnowledgeChunk(
                tenant_id=chunk.tenant_id,
                knowledge_base_id=chunk.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(chunk.document_id, chunk.document_id),
                bucket_id=bucket_id_map.get(chunk.bucket_id, chunk.bucket_id),
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                summary=chunk.summary,
                source_ref=chunk.source_ref,
                metadata_json=deepcopy(chunk.metadata_json or {}),
                created_at=chunk.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)

        source_suggestions = db.exec(
            select(KnowledgeDiscoverySuggestion)
            .where(
                KnowledgeDiscoverySuggestion.tenant_id == tenant_id,
                KnowledgeDiscoverySuggestion.knowledge_base_id == knowledge_base_id,
                KnowledgeDiscoverySuggestion.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeDiscoverySuggestion.created_at.asc())
        ).all()
        for suggestion in source_suggestions:
            clone = KnowledgeDiscoverySuggestion(
                tenant_id=suggestion.tenant_id,
                knowledge_base_id=suggestion.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(suggestion.document_id, suggestion.document_id),
                bucket_id=bucket_id_map.get(suggestion.bucket_id or "", suggestion.bucket_id),
                suggestion_type=suggestion.suggestion_type,
                title=suggestion.title,
                status=suggestion.status,
                payload_json=deepcopy(suggestion.payload_json or {}),
                source_refs_json=_remap_source_refs(suggestion.source_refs_json or [], document_id_map),
                reason=suggestion.reason,
                created_at=suggestion.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)

    else:
        target_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.knowledge_base_version_id == target_version_id,
            )
        ).all()
        for source_document in source_documents:
            match = next(
                (
                    target_document
                    for target_document in target_documents
                    if target_document.filename == source_document.filename
                    and target_document.file_type == source_document.file_type
                    and target_document.title == source_document.title
                ),
                None,
            )
            if match:
                document_id_map[source_document.id] = match.id

    if document_id_map:
        existing_target_concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == target_version_id,
            )
        ).all()
        for concept in existing_target_concepts:
            changed = False
            mapped_document_id = document_id_map.get(concept.document_id or "")
            if mapped_document_id:
                concept.document_id = mapped_document_id
                changed = True
            next_source_refs = _remap_source_refs(concept.source_refs_json or [], document_id_map)
            if next_source_refs != (concept.source_refs_json or []):
                concept.source_refs_json = next_source_refs
                changed = True
            if changed:
                concept.updated_at = utc_now()
                db.add(concept)

    target_concept_ids = {
        concept_id
        for concept_id in db.exec(
            select(KnowledgeConcept.concept_id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == target_version_id,
            )
        ).all()
    }
    source_concepts = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == source_version_id,
            KnowledgeConcept.status != "deleted",
        )
        .order_by(KnowledgeConcept.created_at.asc())
    ).all()
    for concept in source_concepts:
        if concept.concept_id in target_concept_ids:
            continue
        clone = KnowledgeConcept(
            tenant_id=concept.tenant_id,
            knowledge_base_id=concept.knowledge_base_id,
            knowledge_base_version_id=target_version_id,
            document_id=document_id_map.get(concept.document_id or "", concept.document_id),
            concept_id=concept.concept_id,
            concept_type=concept.concept_type,
            title=concept.title,
            description=concept.description,
            content_md=concept.content_md,
            frontmatter_json=deepcopy(concept.frontmatter_json or {}),
            links_json=deepcopy(concept.links_json or []),
            citations_json=deepcopy(concept.citations_json or []),
            source_refs_json=_remap_source_refs(concept.source_refs_json or [], document_id_map),
            status=concept.status,
            created_at=concept.created_at,
            updated_at=utc_now(),
        )
        db.add(clone)


def _remap_source_refs(source_refs: list[dict[str, Any]], document_id_map: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_ref in source_refs:
        if not isinstance(source_ref, dict):
            continue
        next_ref = deepcopy(source_ref)
        document_id = next_ref.get("document_id")
        if isinstance(document_id, str) and document_id in document_id_map:
            next_ref["document_id"] = document_id_map[document_id]
        rows.append(next_ref)
    return rows


def _safe_version_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)
