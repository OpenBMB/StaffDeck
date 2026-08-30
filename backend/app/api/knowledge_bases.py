from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    get_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    promote_knowledge_branch_to_overall,
    rollback_knowledge_branch,
    sync_knowledge_branch_from_overall,
    user_creator_metadata,
)
from app.capability_scope import normalize_capability_scope
from app.db import get_session
from app.db.models import (
    AgentKnowledgeBranch,
    AgentResourceBinding,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Team,
    TeamKnowledgeBaseBinding,
    TeamMember,
    User,
    utc_now,
)
from app.knowledge.access import KnowledgeAccessService
from app.knowledge.audit import KnowledgeAuditService
from app.knowledge.conversion import (
    KnowledgeConversionError,
    KnowledgeConversionService,
)
from app.knowledge.errors import KNOWLEDGE_MODE_INVALID, KnowledgeError, knowledge_error
from app.knowledge.management import (
    require_shared_knowledge_history_viewer,
    require_team_knowledge_manager,
)
from app.knowledge.okf import (
    build_okf_for_document,
    export_okf_bundle,
    lint_okf_concepts,
    normalize_concept_id,
    parse_okf_markdown,
    persist_lint_issues,
    upsert_concepts,
)
from app.knowledge.schema import (
    KnowledgeBaseAuditEventRead,
    KnowledgeBaseAuditPageRead,
    KnowledgeBaseConversionRead,
    KnowledgeBaseConvertToSharedRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseRead,
    KnowledgeBaseRollbackRequest,
    KnowledgeBaseUpdateRequest,
    KnowledgeBaseVersionRead,
    KnowledgeConceptRead,
    KnowledgeConceptUpdateRequest,
    SharedKnowledgeDraftCreateRequest,
    SharedKnowledgePublishRequest,
    SharedKnowledgeRejectRequest,
    SharedKnowledgeRollbackRequest,
    SharedKnowledgeTeamRead,
)
from app.knowledge.versioning import SharedKnowledgeVersionService
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    is_admin_user,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge-bases",
    tags=["enterprise:knowledge-bases"],
    dependencies=[Depends(get_current_user)],
)


def _knowledge_http_error(exc: KnowledgeError) -> HTTPException:
    """把共享知识领域错误映射为不泄漏内容的稳定 HTTP 载荷。"""
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, **exc.details},
    )


def _shared_version_read(
    version: KnowledgeBaseVersion,
    *,
    published_version_id: str | None,
) -> KnowledgeBaseVersionRead:
    """将共享版本行投影为包含来源和当前正式状态的 API 模型。"""
    return KnowledgeBaseVersionRead(
        id=version.id,
        tenant_id=version.tenant_id,
        knowledge_base_id=version.knowledge_base_id,
        version=version.version,
        name=version.name,
        description=version.description,
        status=version.status,
        publication_state=version.publication_state,
        parent_version_id=version.parent_version_id,
        source_team_id=version.source_team_id,
        created_by_agent_id=version.created_by_agent_id,
        created_by_user_id=version.created_by_user_id,
        change_reason=version.change_reason,
        published_at=version.published_at,
        is_published_head=version.id == published_version_id,
        capability_scope=normalize_capability_scope(version.capability_scope),
        metadata=dict(version.metadata_json or {}),
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _agent_shared_management_versions(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str,
) -> tuple[dict[str, KnowledgeBaseVersion], dict[str, dict[str, object]]]:
    """聚合员工在全部活动团队中获授权的共享正式版，仅供管理页展示。"""
    team_ids = sorted(
        set(
            db.exec(
                select(TeamMember.team_id).where(TeamMember.agent_id == agent_id)
            ).all()
        )
    )
    access = KnowledgeAccessService(db)
    versions: dict[str, KnowledgeBaseVersion] = {}
    team_ids_by_base: dict[str, set[str]] = {}
    permissions_by_base: dict[str, dict[str, str]] = {}
    for team_id in team_ids:
        try:
            projections = access.resolve_projections(
                tenant_id=tenant_id,
                agent_id=agent_id,
                team_id=team_id,
            )
        except KnowledgeError:
            continue
        for projection in projections:
            if projection.mode != "shared":
                continue
            version = db.get(
                KnowledgeBaseVersion,
                projection.knowledge_base_version_id,
            )
            if version is None:
                continue
            versions[projection.knowledge_base_id] = version
            team_ids_by_base.setdefault(projection.knowledge_base_id, set()).add(team_id)
            permissions_by_base.setdefault(projection.knowledge_base_id, {})[
                team_id
            ] = projection.permission
    contexts = {
        knowledge_base_id: {
            "mode": "shared",
            "team_ids": sorted(team_ids_by_base.get(knowledge_base_id, set())),
            "permissions": dict(
                sorted(permissions_by_base.get(knowledge_base_id, {}).items())
            ),
        }
        for knowledge_base_id in versions
    }
    return versions, contexts


@router.get(
    "", response_model=list[KnowledgeBaseRead], dependencies=[Depends(require_agent_scope_viewer)]
)
def list_knowledge_bases(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeBaseRead]:
    ensure_tenant(db, tenant_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch)
            .where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
            .order_by(AgentKnowledgeBranch.updated_at.desc())
        ).all()
        knowledge_base_ids = [branch.knowledge_base_id for branch in branches]
        rows_by_id = {
            row.id: row
            for row in db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.id.in_(knowledge_base_ids),
                )
            ).all()
        }
        dedicated_branches = [
            branch
            for branch in branches
            if rows_by_id.get(branch.knowledge_base_id)
            and rows_by_id[branch.knowledge_base_id].mode == "dedicated"
        ]
        dedicated_versions: dict[str, KnowledgeBaseVersion] = {}
        for branch in dedicated_branches:
            kb = rows_by_id.get(branch.knowledge_base_id)
            if kb:
                dedicated_versions[kb.id] = ensure_knowledge_base_version(
                    db,
                    kb,
                    branch.head_version,
                )
        shared_versions, shared_contexts = _agent_shared_management_versions(
            db,
            tenant_id=tenant_id,
            agent_id=agent.id,
        )
        all_versions = {**dedicated_versions, **shared_versions}
        stats = _knowledge_base_stats(
            db,
            tenant_id,
            [version.id for version in all_versions.values()],
        )
        branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id)
        dedicated_reads = [
            knowledge_base_read(
                rows_by_id[branch.knowledge_base_id],
                stats.get(branch.knowledge_base_id, {}),
                version_row=dedicated_versions.get(branch.knowledge_base_id),
                branch_meta=branch_meta.get(branch.knowledge_base_id),
            )
            for branch in dedicated_branches
            if branch.knowledge_base_id in rows_by_id
        ]
        shared_ids = list(shared_versions)
        shared_rows = db.exec(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.mode == "shared",
                KnowledgeBase.id.in_(shared_ids)
                if shared_ids
                else KnowledgeBase.id == "__none__",
            )
            .order_by(KnowledgeBase.updated_at.desc())
        ).all()
        shared_reads = [
            knowledge_base_read(
                row,
                stats.get(row.id, {}),
                version_row=shared_versions.get(row.id),
                management_context=shared_contexts.get(row.id),
            )
            for row in shared_rows
        ]
        return dedicated_reads + shared_reads
    visible_versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    visible_ids = list(visible_versions.keys())
    rows = db.exec(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.id.in_(visible_ids) if visible_ids else KnowledgeBase.id == "__none__",
        )
        .order_by(KnowledgeBase.updated_at.desc())
    ).all()
    stats = _knowledge_base_stats(
        db, tenant_id, [version.id for version in visible_versions.values()]
    )
    branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id)
    return [
        knowledge_base_read(
            row,
            stats.get(row.id, {}),
            version_row=visible_versions.get(row.id),
            branch_meta=branch_meta.get(row.id),
        )
        for row in rows
    ]


@router.post("", response_model=KnowledgeBaseRead)
def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseRead:
    """按请求模式创建员工专用库或不隐式绑定团队的共享库。"""
    ensure_tenant(db, request.tenant_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
    existing = db.exec(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == request.tenant_id, KnowledgeBase.name == name
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists")
    query_agent_id = agent_id if isinstance(agent_id, str) and agent_id else None
    if request.agent_id and query_agent_id and request.agent_id != query_agent_id:
        raise HTTPException(status_code=400, detail="Conflicting employee knowledge scope")
    resolved_agent_id = request.agent_id or query_agent_id
    if request.mode == "shared" and resolved_agent_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KNOWLEDGE_MODE_INVALID",
                "message": "共享知识库不能归属单个员工。",
            },
        )
    agent = ensure_agent_scope_manager(
        db,
        request.tenant_id,
        resolved_agent_id,
        current_user,
    )
    if not (agent and not agent.is_overall):
        ensure_open_gallery_admin(request.tenant_id, current_user)
    creator_metadata = user_creator_metadata(current_user, request.metadata)
    row = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        mode=request.mode,
        capability_scope=request.capability_scope,
        metadata_json=creator_metadata,
        status="active",
    )
    db.add(row)
    db.flush()
    if request.mode == "shared":
        # 共享库先产生空的首个正式快照；团队绑定和员工授权必须另行配置。
        version = ensure_knowledge_base_version(db, row)
        version.publication_state = "released"
        version.created_by_user_id = current_user.id
        version.change_reason = "创建共享知识库"
        version.published_at = utc_now()
        version.metadata_json = dict(creator_metadata)
        row.published_version_id = version.id
        row.metadata_json = {
            **dict(creator_metadata),
            "current_version": version.version,
        }
        db.add(row)
        db.add(version)
        KnowledgeAuditService(db).append_event(
            tenant_id=request.tenant_id,
            knowledge_base_id=row.id,
            team_id=None,
            knowledge_base_version_id=version.id,
            actor_type="user",
            actor_id=current_user.id,
            action="shared_created",
            reason="创建共享知识库",
            details={"published_version_id": version.id},
        )
        db.commit()
        db.refresh(row)
        return knowledge_base_read(row, {}, version_row=version)
    if agent and not agent.is_overall:
        mark_resource_private_for_agent(row, agent.id, creator_metadata)
        ensure_agent_private_knowledge_branch(
            db,
            request.tenant_id,
            agent.id,
            row,
            metadata_json=creator_metadata,
        )
    else:
        mark_resource_open_gallery(row, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            row.id,
            "active",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    return knowledge_base_read(row, {}, version_row=ensure_knowledge_base_version(db, row))


@router.post(
    "/{knowledge_base_id}/convert-to-shared",
    response_model=KnowledgeBaseConversionRead,
)
def convert_knowledge_base_to_shared(
    knowledge_base_id: str,
    request: KnowledgeBaseConvertToSharedRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseConversionRead:
    """复制并验证一个员工专用分支，再原子公开为新的共享知识库。"""
    ensure_tenant(db, request.tenant_id)
    source = _get_knowledge_base(db, request.tenant_id, knowledge_base_id)
    if source.mode != "dedicated":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KNOWLEDGE_MODE_INVALID",
                "message": "共享知识库不能转换回专用知识库。",
            },
        )
    agent = ensure_agent_scope_manager(
        db,
        request.tenant_id,
        request.agent_id,
        current_user,
    )
    if agent is None or agent.is_overall:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KNOWLEDGE_MODE_INVALID",
                "message": "只能转换具体员工的专用知识分支。",
            },
        )

    try:
        result = KnowledgeConversionService(db).convert_to_shared(
            tenant_id=request.tenant_id,
            source_knowledge_base_id=source.id,
            source_agent_id=agent.id,
            source_version_id=request.source_version_id,
            name=request.name,
            description=request.description,
            change_reason=request.change_reason,
            team_ids=request.team_bindings,
            default_for_team_id=request.default_for_team_id,
            actor_user_id=current_user.id,
        )
        db.commit()
    except KnowledgeConversionError as exc:
        db.rollback()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except Exception:
        db.rollback()
        raise

    shared = db.get(KnowledgeBase, result.shared_knowledge_base_id)
    released = db.get(KnowledgeBaseVersion, result.released_version_id)
    if shared is None or released is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "KNOWLEDGE_CONVERSION_INCOMPLETE",
                "message": "转换结果未能完整回读。",
            },
        )
    stats = _knowledge_base_stats(db, request.tenant_id, [released.id]).get(shared.id, {})
    stats["bound_team_count"] = len(result.team_binding_ids)
    return KnowledgeBaseConversionRead(
        source_knowledge_base_id=result.source_knowledge_base_id,
        source_version_id=result.source_version_id,
        new_knowledge_base=knowledge_base_read(
            shared,
            stats,
            version_row=released,
        ),
        released_version=_shared_version_read(
            released,
            published_version_id=shared.published_version_id,
        ),
        binding_ids=list(result.team_binding_ids),
        default_for_team_id=result.default_for_team_id,
        source_archived=result.source_archival_state == "archived",
        audit_event_id=result.audit_event_id,
    )


@router.get(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    visible_version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    stats = _knowledge_base_stats(
        db,
        tenant_id,
        [visible_version.id],
    )
    branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id).get(row.id)
    return knowledge_base_read(
        row,
        stats.get(row.id, {}),
        version_row=visible_version,
        branch_meta=branch_meta,
    )


@router.put("/{knowledge_base_id}", response_model=KnowledgeBaseRead)
def update_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, request.tenant_id, knowledge_base_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == request.tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(
                db, request.tenant_id, agent.id, knowledge_base_id
            )
        version_fields_changed = any(
            value is not None
            for value in (
                request.name,
                request.description,
                request.capability_scope,
                request.metadata,
            )
        )
        if version_fields_changed:
            version = knowledge_version_for_upload(
                db,
                request.tenant_id,
                knowledge_base_id,
                agent.id,
            )
        else:
            version = ensure_knowledge_base_version(db, row, branch.head_version)
        if request.name is not None:
            name = request.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
            version.name = name
        if request.description is not None:
            version.description = request.description
        if request.capability_scope is not None:
            version.capability_scope = request.capability_scope
        if request.metadata is not None:
            version.metadata_json = metadata_preserving_creator(
                version.metadata_json,
                request.metadata,
            )
        if request.status is not None:
            branch.status = "active" if request.status == "active" else "inactive"
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == request.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "knowledge_base",
                    AgentResourceBinding.resource_id == knowledge_base_id,
                )
            ).first()
            if binding:
                binding.status = branch.status
                binding.updated_at = utc_now()
                db.add(binding)
        if (
            request.name is not None
            or request.description is not None
            or request.capability_scope is not None
            or request.metadata is not None
        ):
            branch.sync_state = "diverged"
        version.updated_at = utc_now()
        branch.updated_at = utc_now()
        db.add(version)
        db.add(branch)
        db.commit()
        db.refresh(row)
        stats = _knowledge_base_stats(db, request.tenant_id, [version.id]).get(row.id, {})
        return knowledge_base_read(
            row,
            stats,
            version_row=version,
            branch_meta={
                "base_version": branch.base_version,
                "head_version": branch.head_version,
                "sync_state": branch.sync_state,
                "status": branch.status,
            },
        )
    ensure_open_gallery_admin(request.tenant_id, current_user)
    version = ensure_knowledge_base_version(db, row)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
        conflict = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == request.tenant_id,
                KnowledgeBase.name == name,
                KnowledgeBase.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Knowledge base name already exists")
        row.name = name
        version.name = name
    if request.description is not None:
        row.description = request.description
        version.description = request.description
    if request.capability_scope is not None:
        row.capability_scope = request.capability_scope
        version.capability_scope = request.capability_scope
    if request.status is not None:
        row.status = request.status
        version.status = request.status
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            request.metadata,
        )
        version.metadata_json = metadata_preserving_creator(
            version.metadata_json,
            request.metadata,
        )
    row.updated_at = utc_now()
    version.updated_at = utc_now()
    db.add(row)
    db.add(version)
    db.flush()
    if request.status is not None:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            row.id,
            "active" if request.status == "active" else "inactive",
        )
    db.commit()
    db.refresh(row)
    return knowledge_base_read(
        row,
        _knowledge_base_stats(db, request.tenant_id).get(row.id, {}),
        version_row=version,
    )


@router.get(
    "/{knowledge_base_id}/teams",
    response_model=list[SharedKnowledgeTeamRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_shared_knowledge_teams(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> list[SharedKnowledgeTeamRead]:
    """一次查询返回当前用户可管理且活动绑定到共享库的团队。"""
    ensure_tenant(db, tenant_id)
    try:
        require_shared_knowledge_history_viewer(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            current_user=current_user,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc

    statement = (
        select(Team)
        .join(
            TeamKnowledgeBaseBinding,
            TeamKnowledgeBaseBinding.team_id == Team.id,
        )
        .where(
            Team.tenant_id == tenant_id,
            Team.status == "active",
            TeamKnowledgeBaseBinding.tenant_id == tenant_id,
            TeamKnowledgeBaseBinding.knowledge_base_id == knowledge_base_id,
            TeamKnowledgeBaseBinding.status == "active",
        )
        .order_by(Team.name, Team.id)
    )
    if not is_admin_user(current_user):
        statement = statement.where(Team.owner_user_id == current_user.id)
    return [
        SharedKnowledgeTeamRead(id=team.id, name=team.name)
        for team in db.exec(statement).all()
    ]


@router.get("/{knowledge_base_id}/versions", dependencies=[Depends(require_agent_scope_viewer)])
def list_knowledge_base_versions(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> list[dict[str, object]]:
    """共享库返回全局生命周期历史，专用库保留原员工分支历史。"""
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if row.mode == "shared":
        try:
            require_shared_knowledge_history_viewer(
                db,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                current_user=current_user,
            )
            versions = SharedKnowledgeVersionService(db).list_versions(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
            )
        except KnowledgeError as exc:
            raise _knowledge_http_error(exc) from exc
        return [
            _shared_version_read(
                version,
                published_version_id=row.published_version_id,
            ).model_dump(mode="json")
            for version in versions
        ]
    _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    branch = None
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
    rows = db.exec(
        select(KnowledgeBaseVersion)
        .where(
            KnowledgeBaseVersion.tenant_id == tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == row.id,
        )
        .order_by(KnowledgeBaseVersion.updated_at.desc())
    ).all()
    if agent and not agent.is_overall and branch:
        rows = [
            version
            for version in rows
            if version.version == branch.base_version
            or (version.metadata_json or {}).get("owner_agent_id") == agent.id
        ]
    else:
        rows = [
            version
            for version in rows
            if (version.metadata_json or {}).get("scope") != "agent_private"
        ]
    return [
        {
            "id": version.id,
            "version": version.version,
            "name": version.name,
            "description": version.description,
            "status": version.status,
            "capability_scope": normalize_capability_scope(version.capability_scope),
            "is_head": bool(branch and branch.head_version == version.version),
            "is_base": bool(branch and branch.base_version == version.version),
            "updated_at": version.updated_at.isoformat(),
            "created_at": version.created_at.isoformat(),
        }
        for version in rows
    ]


@router.post(
    "/{knowledge_base_id}/drafts",
    response_model=KnowledgeBaseVersionRead,
)
def create_shared_knowledge_draft(
    knowledge_base_id: str,
    request: SharedKnowledgeDraftCreateRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseVersionRead:
    """由团队所有者或管理员从当前正式版创建共享草稿。"""
    try:
        require_team_knowledge_manager(
            db,
            tenant_id=request.tenant_id,
            team_id=request.team_id,
            knowledge_base_id=knowledge_base_id,
            current_user=current_user,
        )
        draft = SharedKnowledgeVersionService(db).create_draft(
            tenant_id=request.tenant_id,
            knowledge_base_id=knowledge_base_id,
            source_team_id=request.team_id,
            actor_type="user",
            actor_id=current_user.id,
            change_reason=request.change_reason,
            expected_published_version_id=request.expected_published_version_id,
        )
        db.commit()
        db.refresh(draft)
        base = db.get(KnowledgeBase, knowledge_base_id)
        return _shared_version_read(
            draft,
            published_version_id=base.published_version_id if base else None,
        )
    except KnowledgeError as exc:
        db.rollback()
        raise _knowledge_http_error(exc) from exc


@router.post(
    "/{knowledge_base_id}/versions/{version_id}/publish",
    response_model=KnowledgeBaseVersionRead,
)
def publish_shared_knowledge_version(
    knowledge_base_id: str,
    version_id: str,
    request: SharedKnowledgePublishRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseVersionRead:
    """在活动团队绑定下发布草稿并更新唯一全局正式指针。"""
    try:
        require_team_knowledge_manager(
            db,
            tenant_id=request.tenant_id,
            team_id=request.team_id,
            knowledge_base_id=knowledge_base_id,
            current_user=current_user,
        )
        version = SharedKnowledgeVersionService(db).publish_draft(
            tenant_id=request.tenant_id,
            knowledge_base_id=knowledge_base_id,
            draft_version_id=version_id,
            expected_published_version_id=request.expected_published_version_id,
            actor_type="user",
            actor_id=current_user.id,
            source_team_id=request.team_id,
            change_reason=request.change_reason,
        )
        db.commit()
        db.refresh(version)
        return _shared_version_read(
            version,
            published_version_id=version.id,
        )
    except KnowledgeError as exc:
        db.rollback()
        raise _knowledge_http_error(exc) from exc


@router.post(
    "/{knowledge_base_id}/versions/{version_id}/reject",
    response_model=KnowledgeBaseVersionRead,
)
def reject_shared_knowledge_version(
    knowledge_base_id: str,
    version_id: str,
    request: SharedKnowledgeRejectRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseVersionRead:
    """驳回共享草稿但保留快照和审计历史。"""
    try:
        require_team_knowledge_manager(
            db,
            tenant_id=request.tenant_id,
            team_id=request.team_id,
            knowledge_base_id=knowledge_base_id,
            current_user=current_user,
        )
        version = SharedKnowledgeVersionService(db).reject_draft(
            tenant_id=request.tenant_id,
            knowledge_base_id=knowledge_base_id,
            draft_version_id=version_id,
            actor_type="user",
            actor_id=current_user.id,
            source_team_id=request.team_id,
            change_reason=request.change_reason,
        )
        db.commit()
        db.refresh(version)
        base = db.get(KnowledgeBase, knowledge_base_id)
        return _shared_version_read(
            version,
            published_version_id=base.published_version_id if base else None,
        )
    except KnowledgeError as exc:
        db.rollback()
        raise _knowledge_http_error(exc) from exc


@router.get(
    "/{knowledge_base_id}/audit-events",
    response_model=KnowledgeBaseAuditPageRead,
)
def list_shared_knowledge_audit_events(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    team_id: str | None = Query(None),
    action: str | None = Query(None),
    actor_type: str | None = Query(None),
    actor_id: str | None = Query(None),
    version_id: str | None = Query(None),
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> KnowledgeBaseAuditPageRead:
    """返回共享知识库的可筛选审计页，并补齐操作者、团队和版本来源。"""
    try:
        require_shared_knowledge_history_viewer(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            current_user=current_user,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    resolved_offset = offset if isinstance(offset, int) else 0
    resolved_limit = limit if isinstance(limit, int) else 100
    page = KnowledgeAuditService(db).query_events(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        offset=resolved_offset,
        limit=resolved_limit,
        team_id=team_id if isinstance(team_id, str) else None,
        action=action if isinstance(action, str) else None,
        actor_type=actor_type if isinstance(actor_type, str) else None,
        actor_id=actor_id if isinstance(actor_id, str) else None,
        knowledge_base_version_id=version_id if isinstance(version_id, str) else None,
    )
    return KnowledgeBaseAuditPageRead(
        items=[
            KnowledgeBaseAuditEventRead.model_validate(item, from_attributes=True)
            for item in page.items
        ],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        has_more=page.has_more,
    )


@router.get(
    "/{knowledge_base_id}/okf/concepts",
    response_model=list[KnowledgeConceptRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_okf_concepts(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    concept_type: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeConceptRead]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    stmt = select(KnowledgeConcept).where(
        KnowledgeConcept.tenant_id == tenant_id,
        KnowledgeConcept.knowledge_base_id == knowledge_base_id,
        KnowledgeConcept.knowledge_base_version_id == version.id,
        KnowledgeConcept.status != "deleted",
    )
    if concept_type:
        stmt = stmt.where(KnowledgeConcept.concept_type == concept_type)
    rows = db.exec(stmt.order_by(KnowledgeConcept.concept_type, KnowledgeConcept.concept_id)).all()
    return [concept_read(row) for row in rows]


@router.get(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}",
    response_model=KnowledgeConceptRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeConceptRead:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    row = _get_concept(db, tenant_id, knowledge_base_id, version.id, concept_id)
    return concept_read(row)


@router.put(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}", response_model=KnowledgeConceptRead
)
def upsert_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    request: KnowledgeConceptUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeConceptRead:
    version = _writable_knowledge_version(
        db, request.tenant_id, knowledge_base_id, agent_id, current_user
    )
    document_id = _document_id_for_version(
        db, request.tenant_id, knowledge_base_id, version.id, request.document_id
    )
    parsed = parse_okf_markdown(concept_id, request.content_md)
    rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base_id,
        version.id,
        [
            {
                "concept_id": parsed.concept_id,
                "content_md": parsed.content_md,
                "document_id": document_id,
                "status": request.status,
            }
        ],
    )
    return concept_read(rows[0])


@router.get("/{knowledge_base_id}/okf/export", dependencies=[Depends(require_agent_scope_viewer)])
def export_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> Response:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    rows = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == version.id,
            KnowledgeConcept.status == "active",
        )
        .order_by(KnowledgeConcept.concept_id)
    ).all()
    archive = export_okf_bundle(kb, version.id, rows)
    filename = f"{kb.name or knowledge_base_id}-okf-{version.version}.zip"
    fallback_filename = f"{knowledge_base_id}-okf-{version.version}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback_filename}\"; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.post("/{knowledge_base_id}/okf/lint", dependencies=[Depends(require_agent_scope_viewer)])
def lint_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    issues = lint_okf_concepts(db, tenant_id, knowledge_base_id, version.id)
    persist_lint_issues(db, tenant_id, knowledge_base_id, version.id, issues)
    return {"status": "ok", "issue_count": len(issues), "issues": issues}


@router.delete("/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(db, tenant_id, agent.id, knowledge_base_id)
        branch.status = "deleted"
        branch.updated_at = utc_now()
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == row.id,
            )
        ).first()
        if not binding:
            binding = AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=agent.id,
                resource_type="knowledge_base",
                resource_id=row.id,
                status="deleted",
            )
        else:
            binding.status = "deleted"
            binding.updated_at = utc_now()
        db.add(branch)
        db.add(binding)
        db.commit()
        return {"status": "hidden"}
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "knowledge_base", row):
            raise HTTPException(
                status_code=404, detail="Knowledge base not visible in open gallery"
            )
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "knowledge_base", row.id)
        db.commit()
        return {"status": "hidden"}
    ensure_open_gallery_admin(tenant_id, current_user)
    for model in (
        KnowledgeDiscoverySuggestion,
        KnowledgeIngestJob,
        KnowledgeConcept,
        KnowledgeChunk,
        KnowledgeBucket,
        KnowledgeDocument,
        KnowledgeBaseVersion,
        AgentKnowledgeBranch,
    ):
        children = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == row.id,
            )
        ).all()
        for child in children:
            db.delete(child)
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.resource_type == "knowledge_base",
            AgentResourceBinding.resource_id == row.id,
        )
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{knowledge_base_id}/sync-from-overall")
def sync_knowledge_base_from_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    branch = sync_knowledge_branch_from_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {
        "status": "synced",
        "knowledge_base_id": knowledge_base_id,
        "head_version": branch.head_version,
    }


@router.post("/{knowledge_base_id}/promote-to-overall")
def promote_knowledge_base_to_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Overall agent does not have a branch to promote"
        )
    ensure_open_gallery_admin(tenant_id, current_user)
    version = promote_knowledge_branch_to_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {
        "status": "promoted",
        "knowledge_base_id": knowledge_base_id,
        "version": version.version,
    }


@router.post("/{knowledge_base_id}/rollback")
def rollback_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseRollbackRequest | SharedKnowledgeRollbackRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, object]:
    """共享库移动全局正式指针，专用库继续回滚员工分支头。"""
    if isinstance(request, SharedKnowledgeRollbackRequest):
        try:
            require_team_knowledge_manager(
                db,
                tenant_id=request.tenant_id,
                team_id=request.team_id,
                knowledge_base_id=knowledge_base_id,
                current_user=current_user,
            )
            target = SharedKnowledgeVersionService(db).rollback(
                tenant_id=request.tenant_id,
                knowledge_base_id=knowledge_base_id,
                target_version_id=request.target_version_id,
                expected_published_version_id=request.expected_published_version_id,
                actor_type="user",
                actor_id=current_user.id,
                source_team_id=request.team_id,
                change_reason=request.change_reason,
            )
            db.commit()
            return {
                "status": "rolled_back",
                "knowledge_base_id": knowledge_base_id,
                "previous_published_version_id": request.expected_published_version_id,
                "target_version_id": target.id,
            }
        except KnowledgeError as exc:
            db.rollback()
            raise _knowledge_http_error(exc) from exc
    agent = ensure_agent_scope_manager(db, request.tenant_id, request.agent_id, current_user)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Use overall version management for trunk knowledge base"
        )
    branch = rollback_knowledge_branch(
        db, request.tenant_id, request.agent_id, knowledge_base_id, request.version
    )
    db.commit()
    return {
        "status": "rolled_back",
        "knowledge_base_id": knowledge_base_id,
        "head_version": branch.head_version,
    }


def knowledge_base_read(
    row: KnowledgeBase,
    stats: dict[str, int],
    version_row: KnowledgeBaseVersion | None = None,
    branch_meta: dict[str, str] | None = None,
    management_context: dict[str, object] | None = None,
) -> KnowledgeBaseRead:
    """统一投影专用分支和共享正式指针，同时保持旧字段兼容。"""
    branch_status = (branch_meta or {}).get("status")
    if branch_status == "inactive":
        effective_status = "archived"
    elif branch_status == "active":
        effective_status = "active"
    elif branch_status:
        effective_status = branch_status
    else:
        effective_status = row.status
    return KnowledgeBaseRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=version_row.name if version_row else row.name,
        description=version_row.description if version_row else row.description,
        status=effective_status,
        mode=row.mode,
        published_version_id=row.published_version_id,
        published_version=(
            version_row.version
            if row.mode == "shared"
            and version_row
            and version_row.id == row.published_version_id
            else None
        ),
        bound_team_count=int(stats.get("bound_team_count", 0)),
        management_context={"mode": row.mode, **(management_context or {})},
        capability_scope=normalize_capability_scope(
            version_row.capability_scope if version_row else row.capability_scope
        ),
        version=version_row.version if version_row else None,
        branch_sync_state=(branch_meta or {}).get("sync_state"),
        branch_base_version=(branch_meta or {}).get("base_version"),
        branch_head_version=(branch_meta or {}).get("head_version"),
        metadata=dict((version_row.metadata_json if version_row else row.metadata_json) or {}),
        document_count=int(stats.get("document_count", 0)),
        bucket_count=int(stats.get("bucket_count", 0)),
        chunk_count=int(stats.get("chunk_count", 0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def concept_read(row: KnowledgeConcept) -> KnowledgeConceptRead:
    return KnowledgeConceptRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        document_id=row.document_id,
        concept_id=row.concept_id,
        concept_type=row.concept_type,
        title=row.title,
        description=row.description,
        content_md=row.content_md,
        frontmatter=row.frontmatter_json or {},
        links=row.links_json or [],
        citations=row.citations_json or [],
        source_refs=row.source_refs_json or [],
        status=row.status,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _visible_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
) -> KnowledgeBaseVersion:
    _get_knowledge_base(db, tenant_id, knowledge_base_id)
    versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    version = versions.get(knowledge_base_id)
    if not version:
        raise HTTPException(status_code=404, detail="Knowledge base version not visible")
    return version


def _writable_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    current_user: User,
) -> KnowledgeBaseVersion:
    """保留专用分支写入；共享库必须通过显式草稿入口。"""
    base = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if base.mode == "shared":
        raise _knowledge_http_error(
            knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="共享知识库必须先创建草稿，不能直接修改正式版本。",
            )
        )
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        version = knowledge_version_for_upload(
            db,
            tenant_id,
            knowledge_base_id,
            agent.id,
            metadata_json=user_creator_metadata(current_user),
        )
        db.commit()
        return version
    ensure_open_gallery_admin(tenant_id, current_user)
    return _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)


def _ensure_okf_concepts_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.status == "ready",
        )
    ).all()
    for document in documents:
        existing = db.exec(
            select(KnowledgeConcept.id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == version_id,
                KnowledgeConcept.document_id == document.id,
            )
        ).first()
        if existing:
            continue
        metadata = document.metadata_json or {}
        section_nodes = (
            metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
        )
        buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == version_id,
                KnowledgeBucket.document_id == document.id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        if not section_nodes and not buckets:
            continue
        upsert_concepts(
            db,
            tenant_id,
            knowledge_base_id,
            version_id,
            build_okf_for_document(document, section_nodes, buckets),
        )


def _get_concept(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    concept_id: str,
) -> KnowledgeConcept:
    normalized = normalize_concept_id(concept_id)
    row = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeConcept.concept_id == normalized,
            KnowledgeConcept.status != "deleted",
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="OKF concept not found")
    return row


def _document_id_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    document_id: str | None,
) -> str | None:
    if not document_id:
        return None
    current = db.get(KnowledgeDocument, document_id)
    if (
        current
        and current.tenant_id == tenant_id
        and current.knowledge_base_id == knowledge_base_id
        and current.knowledge_base_version_id == knowledge_base_version_id
    ):
        return current.id
    if (
        not current
        or current.tenant_id != tenant_id
        or current.knowledge_base_id != knowledge_base_id
    ):
        return document_id
    cloned = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeDocument.filename == current.filename,
            KnowledgeDocument.file_type == current.file_type,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).first()
    return cloned.id if cloned else document_id


def _management_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
) -> dict[str, KnowledgeBaseVersion]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).all()
        result: dict[str, KnowledgeBaseVersion] = {}
        for branch in branches:
            kb = db.get(KnowledgeBase, branch.knowledge_base_id)
            if not kb or kb.tenant_id != tenant_id:
                continue
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "knowledge_base",
                    AgentResourceBinding.resource_id == kb.id,
                )
            ).first()
            if not binding or not is_bound_resource_visible_for_agent(
                db, tenant_id, "knowledge_base", kb, binding
            ):
                continue
            result[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
        return result
    rows = db.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "knowledge_base", row)]
    return {row.id: ensure_knowledge_base_version(db, row) for row in rows}


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeBase, knowledge_base_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return row


def _knowledge_base_stats(
    db: Session,
    tenant_id: str,
    version_ids: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    doc_stmt = select(KnowledgeDocument.knowledge_base_id, func.count(KnowledgeDocument.id)).where(
        KnowledgeDocument.tenant_id == tenant_id
    )
    bucket_stmt = select(KnowledgeBucket.knowledge_base_id, func.count(KnowledgeBucket.id)).where(
        KnowledgeBucket.tenant_id == tenant_id
    )
    chunk_stmt = select(KnowledgeChunk.knowledge_base_id, func.count(KnowledgeChunk.id)).where(
        KnowledgeChunk.tenant_id == tenant_id
    )
    if version_ids is not None:
        doc_stmt = doc_stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(version_ids))
        bucket_stmt = bucket_stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(version_ids))
        chunk_stmt = chunk_stmt.where(KnowledgeChunk.knowledge_base_version_id.in_(version_ids))
    for knowledge_base_id, count in db.exec(
        doc_stmt.group_by(KnowledgeDocument.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["document_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        bucket_stmt.group_by(KnowledgeBucket.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["bucket_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        chunk_stmt.group_by(KnowledgeChunk.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["chunk_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        select(
            TeamKnowledgeBaseBinding.knowledge_base_id,
            func.count(TeamKnowledgeBaseBinding.id),
        )
        .where(
            TeamKnowledgeBaseBinding.tenant_id == tenant_id,
            TeamKnowledgeBaseBinding.status == "active",
        )
        .group_by(TeamKnowledgeBaseBinding.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["bound_team_count"] = int(count or 0)
    return stats


def _knowledge_branch_meta(
    db: Session, tenant_id: str, agent_id: str | None
) -> dict[str, dict[str, str]]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        return {}
    rows = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent.id,
            AgentKnowledgeBranch.status != "deleted",
        )
    ).all()
    return {
        row.knowledge_base_id: {
            "base_version": row.base_version,
            "head_version": row.head_version,
            "sync_state": row.sync_state,
            "status": row.status,
        }
        for row in rows
    }
