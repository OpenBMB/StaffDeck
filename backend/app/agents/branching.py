from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from app.db.models import AgentProfile, AgentResourceBinding, AgentSkillBranch, AgentSkillBranchVersion, GeneralSkill, MCPServer, ModelConfig, Skill, SkillVersion, Tool, utc_now
from app.llm.model_config_resolver import (
    ResolvedModelConfig,
    resolve_model_config_for_runtime,
)


DEFAULT_AGENT_ROLES = ("default", "router", "step", "response", "general_skill")
OPEN_GALLERY_SCOPE = "open_gallery"
AGENT_PRIVATE_SCOPE = "agent_private"
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
    return isinstance(value, str) and bool(value.strip())


def user_creator_metadata(
    user: object | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    metadata = dict(extra or {})
    if user is None:
        return metadata
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)
    display_name = getattr(user, "display_name", None) or username
    if _valid_creator_value(user_id):
        metadata["owner_user_id"] = str(user_id).strip()
        metadata["created_by_user_id"] = str(user_id).strip()
    if _valid_creator_value(username):
        normalized_username = str(username).strip()
        metadata["owner_username"] = normalized_username
        metadata["created_by_username"] = normalized_username
        metadata["created_by"] = normalized_username
        metadata["creator_name"] = normalized_username
    if _valid_creator_value(display_name):
        normalized_display_name = str(display_name).strip()
        metadata["owner_display_name"] = normalized_display_name
        metadata["created_by_display_name"] = normalized_display_name
    return metadata


def metadata_preserving_creator(
    existing: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Replace editable metadata without changing the original creator."""
    metadata = dict(replacement or {})
    current = dict(existing or {})
    for key in CREATOR_METADATA_KEYS:
        value = current.get(key)
        if _valid_creator_value(value):
            metadata[key] = value
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


def _agent_creator_metadata(agent: AgentProfile | None) -> dict[str, Any]:
    if not agent:
        return {}
    source = dict(agent.metadata_json or {})
    metadata = {
        key: value
        for key, value in source.items()
        if key in CREATOR_METADATA_KEYS and _valid_creator_value(value)
    }
    return metadata


def _agent_private_metadata_for(
    db: Session,
    tenant_id: str,
    agent_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agent_metadata = _agent_creator_metadata(get_agent(db, tenant_id, agent_id))
    return agent_private_metadata(agent_id, {**agent_metadata, **(extra or {})})


def is_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> bool:
    agent = get_agent(db, tenant_id, agent_id)
    return bool(agent and agent.is_overall)


def require_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> None:
    if not agent_id and not get_overall_agent(db, tenant_id):
        return
    if not is_overall_agent(db, tenant_id, agent_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403, detail="Only the overall agent can delete global resources"
        )


def open_gallery_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(extra or {})
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


def mark_resource_open_gallery(
    resource: object, metadata_json: dict[str, Any] | None = None
) -> None:
    if not hasattr(resource, "metadata_json"):
        return
    metadata = open_gallery_metadata(
        {**(getattr(resource, "metadata_json", None) or {}), **(metadata_json or {})}
    )
    setattr(resource, "metadata_json", metadata)


def mark_resource_private_for_agent(
    resource: object,
    agent_id: str,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    if not hasattr(resource, "metadata_json"):
        return
    metadata = agent_private_metadata(
        agent_id, {**(getattr(resource, "metadata_json", None) or {}), **(metadata_json or {})}
    )
    setattr(resource, "metadata_json", metadata)


def ensure_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
    revive: bool = False,
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
            metadata_json=open_gallery_metadata(metadata_json),
            revive=revive,
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
    metadata_json: dict[str, Any] | None = None,
    revive: bool = False,
) -> None:
    _ensure_binding(
        db,
        tenant_id,
        agent_id,
        resource_type,
        resource_id,
        status,
        metadata_json=_agent_private_metadata_for(db, tenant_id, agent_id, metadata_json),
        revive=revive,
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


def is_open_gallery_resource(
    db: Session, tenant_id: str, resource_type: str, resource: object
) -> bool:
    resource_id = getattr(resource, "id", None)
    if not resource_id or getattr(resource, "tenant_id", None) != tenant_id:
        return False
    overall = get_overall_agent(db, tenant_id)
    if not overall:
        return False
    overall_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == overall.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()
    if not overall_binding:
        return False
    return overall_binding.status != "deleted" and not _binding_is_private(overall_binding)


def is_bound_resource_visible_for_agent(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource: object,
    binding: AgentResourceBinding,
    *, gallery_visibility: bool | None = None,
) -> bool:
    if binding.status == "deleted":
        return False
    if getattr(resource, "tenant_id", None) != tenant_id:
        return False
    if _binding_is_private(binding) or _metadata_is_private(_resource_metadata(resource)):
        return True
    if gallery_visibility is not None:
        return gallery_visibility
    return is_open_gallery_resource(db, tenant_id, resource_type, resource)


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
        "metadata": dict(branch.metadata_json or {}),
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


def _datetime_sort_key(value: datetime | None) -> float:
    """Compare persisted naive UTC values with timezone-aware UTC values safely."""
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).timestamp()


def visible_skill_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Skill]:
    from app.skills.sop_management import legacy_sop_writes_disabled, SopManagementService
    if legacy_sop_writes_disabled():
        return SopManagementService.rows(db, tenant_id, agent_id, published_only=not include_inactive)
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        status_clause = (
            Skill.status != "deleted" if include_inactive else Skill.status == "published"
        )
        rows = list(
            db.exec(
                select(Skill)
                .where(Skill.tenant_id == tenant_id, status_clause)
                .order_by(Skill.updated_at.desc())
            ).all()
        )
        metadata_by_id = resource_binding_metadata(
            db, tenant_id, agent.id if agent else None, "skill"
        )
        visible_rows = [
            row for row in rows if is_open_gallery_resource(db, tenant_id, "skill", row)
        ]
        for row in visible_rows:
            object.__setattr__(
                row, "agent_branch_meta", {"metadata": metadata_by_id.get(row.id, {})}
            )
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
        if not is_bound_resource_visible_for_agent(db, tenant_id, "skill", skill, binding):
            continue
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
        if not include_inactive and branch.status != "active":
            continue
        rows.append(project_skill_with_branch(skill, branch, binding.status))
    return sorted(rows, key=lambda item: _datetime_sort_key(item.updated_at), reverse=True)


def visible_published_skills(
    db: Session, tenant_id: str, agent_id: str | None = None
) -> list[Skill]:
    return [
        skill
        for skill in visible_skill_rows(db, tenant_id, agent_id)
        if skill.status == "published"
    ]


def visible_skill(
    db: Session, tenant_id: str, skill_id: str, agent_id: str | None = None
) -> Skill | None:
    from app.skills.sop_management import legacy_sop_writes_disabled, SopManagementService
    if legacy_sop_writes_disabled():
        return next((r for r in SopManagementService.rows(db, tenant_id, agent_id, published_only=True)
            if r.skill_id == skill_id), None)
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
        metadata_by_id = resource_binding_metadata(
            db, tenant_id, agent.id if agent else None, "skill"
        )
        object.__setattr__(
            skill, "agent_branch_meta", {"metadata": metadata_by_id.get(skill.id, {})}
        )
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
    if not is_bound_resource_visible_for_agent(db, tenant_id, "skill", skill, binding):
        return None
    branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    if branch.status != "active":
        return None
    return project_skill_with_branch(skill, branch)


def visible_tool_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Tool]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        return []
    if not agent or agent.is_overall:
        rows = db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id).order_by(Tool.bucket, Tool.name)
        ).all()
        return [
            row
            for row in rows
            if is_open_gallery_resource(db, tenant_id, "tool", row)
            and (include_inactive or _tool_runtime_enabled(db, row))
        ]

    bindings = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.status != "deleted",
        )
        .order_by(AgentResourceBinding.updated_at.desc())
    ).all()
    visible: list[Tool] = []
    for binding in bindings:
        if not include_inactive and binding.status != "active":
            continue
        row = db.get(Tool, binding.resource_id)
        if not row or row.tenant_id != tenant_id:
            continue
        if not is_bound_resource_visible_for_agent(db, tenant_id, "tool", row, binding):
            continue
        if not include_inactive and not _tool_runtime_enabled(db, row):
            continue
        visible.append(row)
    return sorted(visible, key=lambda row: (row.bucket, row.name))


def _tool_runtime_enabled(db: Session, row: Tool) -> bool:
    if not row.enabled:
        return False
    if not row.mcp_server_id:
        return True
    server = db.get(MCPServer, row.mcp_server_id)
    return bool(
        server
        and server.tenant_id == row.tenant_id
        and server.enabled
    )


def ensure_agent_skill_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    skill: Skill,
    metadata_json: dict[str, Any] | None = None,
) -> AgentSkillBranch:
    from app.skills.sop_management import ensure_legacy_sop_writes_enabled
    ensure_legacy_sop_writes_enabled()
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    branch_metadata = dict(getattr(branch, "metadata_json", None) or {})
    if metadata_json:
        branch_metadata.update(metadata_json)
    metadata = _agent_private_metadata_for(db, tenant_id, agent_id, branch_metadata)
    if branch:
        if branch.metadata_json != metadata:
            branch.metadata_json = metadata
            branch.updated_at = utc_now()
            db.add(branch)
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
        metadata_json=metadata,
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
    from app.skills.sop_management import normalize_sop_write
    next_content = normalize_sop_write(next_content)
    branch.content_json = next_content
    branch.head_version = next_version
    branch.status = previous_status
    branch.sync_state = "diverged"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, change_summary)
    return branch


def sync_branch_from_overall(
    db: Session, tenant_id: str, agent_id: str, skill: Skill
) -> AgentSkillBranch:
    if skill.status != "published" or not is_open_gallery_resource(db, tenant_id, "skill", skill):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400, detail="Disabled skill cannot be learned from the open gallery"
        )
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


def rollback_branch(
    db: Session, tenant_id: str, agent_id: str, skill_id: str, version: str
) -> AgentSkillBranch:
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


def branch_versions(
    db: Session, tenant_id: str, agent_id: str, skill_id: str
) -> list[AgentSkillBranchVersion]:
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


def visible_knowledge_base_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    return list(visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive).keys())


def visible_knowledge_base_versions(db: Session, tenant_id: str, agent_id: str | None=None, include_inactive: bool=False):
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
    return StaffDeckKnowledgeAdapter(db, tenant_id, agent_id).visible_versions(include_inactive)


def visible_knowledge_base_version_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    return [
        row.id
        for row in visible_knowledge_base_versions(
            db, tenant_id, agent_id, include_inactive
        ).values()
    ]
















def model_for_agent(
    db: Session, tenant_id: str, agent_id: str | None, role: str = "default"
) -> ResolvedModelConfig | None:
    """Resolve every employee task through the tenant's enabled default model.

    ``agent_id`` and ``role`` remain in the signature for call-site compatibility. Employee-level
    model bindings are intentionally no longer part of runtime model selection.
    """
    _ = agent_id, role
    model = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    return _runtime_model(db, tenant_id, model) if model else None


def _runtime_model(
    db: Session, tenant_id: str, model: ModelConfig
) -> ResolvedModelConfig:
    return resolve_model_config_for_runtime(db, tenant_id, model.id)


def copy_overall_scope_to_agent(db: Session, tenant_id: str, agent: AgentProfile) -> None:
    from app.skills.sop_management import SopManagementService, legacy_sop_writes_disabled
    if legacy_sop_writes_disabled():
        SopManagementService.copy_scope(db, tenant_id, None, agent.id)
    skills = [] if legacy_sop_writes_disabled() else db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
    ).all()
    for skill in skills:
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "skill",
            skill.id,
            _binding_status_from_resource_status(skill.status),
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )
        ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    general_skills = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.status == "published"
        )
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
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )


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
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )


def next_branch_version(version: str, requested_version: str | None = None) -> str:
    requested = (requested_version or "").strip()
    if _is_semver(requested) and requested != version:
        return requested
    base = version.partition("-branch.")[0]
    return next_global_version(base)


def next_unique_branch_version(
    db: Session, branch: AgentSkillBranch, requested_version: str | None = None
) -> str:
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
    revive: bool = False,
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
        if existing.status == "deleted" and status != "deleted" and not revive:
            if metadata_json is not None and not existing.metadata_json:
                existing.metadata_json = metadata_json
                existing.updated_at = utc_now()
                db.add(existing)
            return
        existing.status = status
        if metadata_json is not None:
            merged_metadata = {
                **(existing.metadata_json or {}),
                **metadata_json,
            }
            existing.metadata_json = metadata_preserving_creator(
                existing.metadata_json,
                merged_metadata,
            )
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
















def _remap_source_refs(
    source_refs: list[dict[str, Any]], document_id_map: dict[str, str]
) -> list[dict[str, Any]]:
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
