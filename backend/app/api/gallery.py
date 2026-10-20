"""开放广场聚合计数接口。

广场页需要一个「各模块有多少条可见资源」的轻量接口：判定可见性要走
`is_open_gallery_resource` / overall 绑定过滤，与完整列表接口同口径，但首页角标只需要数字，
不投影业务字段。计数口径必须对齐前端 OpenPlatformPage 的二次过滤：
- agents: 非整体、status == "active"、metadata.published_to_gallery is True；
- knowledge: overall 绑定可见 + status == "active" + 排除空的默认知识库；
- general-skills / skills: overall 绑定可见 + status == "published"；
- tools: overall 绑定可见 + enabled（与列表 include_inactive=True 时一致，不校验运行态）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import load_only
from sqlmodel import Session, select

from app.agents.branching import (
    build_binding_visibility_prefetch,
    is_open_gallery_resource,
)
from app.api.agents import _agent_hidden_from_staffdeck, _agent_visible_to_user
from app.api.knowledge_bases import _knowledge_base_stats
from app.db import get_session
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    Tool,
)
from app.security.auth import get_current_user
from app.security.permissions import is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/gallery", dependencies=[Depends(get_current_user)])


class GalleryCountsRead(BaseModel):
    agents: int = 0
    knowledge: int = 0
    general_skills: int = 0
    skills: int = 0
    tools: int = 0


def _tenant_bindings(db: Session, tenant_id: str) -> list[AgentResourceBinding]:
    return list(
        db.exec(
            select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == tenant_id)
        ).all()
    )


def _is_empty_default_knowledge_base(row: KnowledgeBase, stats: dict[str, int]) -> bool:
    """对齐前端 isEmptyDefaultKnowledgeBase：名为默认知识库且 0 文档/0 目录/0 引用。"""
    return (
        row.name == "默认知识库"
        and stats.get("document_count", 0) == 0
        and stats.get("bucket_count", 0) == 0
        and stats.get("chunk_count", 0) == 0
    )


@router.get("/counts", response_model=GalleryCountsRead)
def gallery_counts(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: Any = Depends(get_current_user),
) -> GalleryCountsRead:
    ensure_tenant(db, tenant_id)
    bindings = _tenant_bindings(db, tenant_id)
    prefetch = build_binding_visibility_prefetch(db, tenant_id, bindings)
    counts = GalleryCountsRead()
    user = current_user

    # agents：与 list_agents 相同的可见集合，再对齐前端 isGalleryEmployee / status 过滤。
    for row in db.exec(
        select(AgentProfile)
        .where(AgentProfile.tenant_id == tenant_id)
        .order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())
    ).all():
        if row.is_overall or _agent_hidden_from_staffdeck(row):
            continue
        if not (is_admin_user(user) or _agent_visible_to_user(row, user)):
            continue
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if row.status == "active" and metadata.get("published_to_gallery") is True:
            counts.agents += 1

    # knowledge：overall 绑定可见 + status == "active" + 排除空的默认知识库。
    kb_stats = _knowledge_base_stats(db, tenant_id)
    counts.knowledge = sum(
        1
        for row in db.exec(
            select(KnowledgeBase)
            .where(KnowledgeBase.tenant_id == tenant_id)
            .options(load_only(KnowledgeBase.id, KnowledgeBase.tenant_id, KnowledgeBase.name, KnowledgeBase.status))
        ).all()
        if row.status == "active"
        and not _is_empty_default_knowledge_base(row, kb_stats.get(row.id, {}))
        and is_open_gallery_resource(db, tenant_id, "knowledge_base", row, prefetch=prefetch)
    )

    # GeneralSkill 全实体单行可带 MB 级 skill_files_json，计数只要状态列，必须投影掉
    # 文件包内容（见 branching._RESOURCE_LOAD_COLUMNS 的注释：全实体 12 行 9.8s vs 按列 0.02s）。
    counts.general_skills = sum(
        1
        for row in db.exec(
            select(GeneralSkill)
            .where(GeneralSkill.tenant_id == tenant_id)
            .options(load_only(GeneralSkill.id, GeneralSkill.tenant_id, GeneralSkill.status))
        ).all()
        if row.status == "published"
        and is_open_gallery_resource(db, tenant_id, "general_skill", row, prefetch=prefetch)
    )
    counts.skills = sum(
        1
        for row in db.exec(
            select(Skill)
            .where(Skill.tenant_id == tenant_id)
            .options(load_only(Skill.id, Skill.tenant_id, Skill.status))
        ).all()
        if row.status == "published"
        and is_open_gallery_resource(db, tenant_id, "skill", row, prefetch=prefetch)
    )
    counts.tools = sum(
        1
        for row in db.exec(
            select(Tool)
            .where(Tool.tenant_id == tenant_id)
            .options(load_only(Tool.id, Tool.tenant_id))
        ).all()
        if is_open_gallery_resource(db, tenant_id, "tool", row, prefetch=prefetch)
    )
    return counts
