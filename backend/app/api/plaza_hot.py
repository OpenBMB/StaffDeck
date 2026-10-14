"""开放广场「全部」tab —— 热门资源聚合接口。

`GET /api/enterprise/plaza-hot` 返回全广场热门资源 Top 10，供「全部」tab 的
热门排行榜 / №1 主推卡使用。

热度口径（真实计数，不做人工加权）：
- 数字员工 agents:  `usage_count`（每新建一个会话 +1，站内与分享访客都计入）；
- 技能 general-skills: `download_count`（广场下载 + 免登录分享下载都 +1）；
- SOP skills:  被复制到员工侧的分支数（agent_skill_branches.source_skill_id，
  排除 overall 宿主自己的发布分支）；
- 知识库 knowledge:  被复制到员工侧的绑定数（agent_resource_bindings，排除
  overall 宿主的广场发布绑定）。

可见性判定与 gallery_counts 完全同口径（is_open_gallery_resource / 发布状态 /
管理员可见性）。Agent 广场条目是前端本地清单、不落库，暂无使用计数，不进榜；
后续如需纳入，需先补使用埋点。

排序：score 降序 → updated_at 降序。score 为 0 的资源也返回（前端显示 0），
保证新租户页面不为空。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import load_only
from sqlmodel import Session, func, select

from app.agents.branching import (
    build_binding_visibility_prefetch,
    is_open_gallery_resource,
)
from app.api.agents import _agent_hidden_from_staffdeck, _agent_visible_to_user
from app.api.gallery import _is_empty_default_knowledge_base
from app.api.knowledge_bases import _knowledge_base_stats
from app.db.database import get_session
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    GeneralSkill,
    KnowledgeBase,
    Skill,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/plaza-hot",
    tags=["enterprise:plaza-hot"],
    dependencies=[Depends(get_current_user)],
)

TOP_LIMIT = 10

# 前端广场模块 key（与 OpenPlatformPage 的 PlatformKind 一致，snake 转 kebab 在出参时做）
METRIC_LABELS = {
    "agents": "被使用",
    "general-skills": "下载",
    "skills": "被复制",
    "knowledge": "被引用",
}


class HotItem(BaseModel):
    kind: str  # agents | general-skills | skills | knowledge
    ref_id: str
    title: str
    description: str = ""
    metric_label: str
    metric_value: int
    score: int
    updated_at: datetime | None = None


class HotPayload(BaseModel):
    generated_at: datetime
    items: list[HotItem]


def _scored_items(db: Session, tenant_id: str, user: Any) -> list[HotItem]:
    overall_id = ""
    overall = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id, AgentProfile.is_overall == True  # noqa: E712
        )
    ).first()
    if overall:
        overall_id = overall.id

    bindings = list(
        db.exec(
            select(AgentResourceBinding).where(AgentResourceBinding.tenant_id == tenant_id)
        ).all()
    )
    prefetch = build_binding_visibility_prefetch(db, tenant_id, bindings)

    items: list[tuple[int, datetime | None, HotItem]] = []

    def _push(kind: str, ref_id: str, title: str, description: str,
              value: int, updated_at: datetime | None) -> None:
        items.append(
            (
                value,
                updated_at,
                HotItem(
                    kind=kind,
                    ref_id=ref_id,
                    title=title,
                    description=description or "",
                    metric_label=METRIC_LABELS[kind],
                    metric_value=value,
                    score=value,
                    updated_at=updated_at,
                ),
            )
        )

    # 数字员工：published_to_gallery + active，与非整体、可见性同口径
    for row in db.exec(
        select(AgentProfile)
        .where(AgentProfile.tenant_id == tenant_id)
        .order_by(AgentProfile.updated_at.desc())  # type: ignore[attr-defined]
    ).all():
        if row.is_overall or _agent_hidden_from_staffdeck(row):
            continue
        if not (is_admin_user(user) or _agent_visible_to_user(row, user)):
            continue
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if row.status == "active" and metadata.get("published_to_gallery") is True:
            _push("agents", row.id, row.name, row.description or "",
                  max(0, int(row.usage_count or 0)), row.updated_at)

    # 技能：published + overall 绑定可见；投影掉 MB 级 skill_files_json（见 gallery_counts 注释）
    for row in db.exec(
        select(GeneralSkill)
        .where(GeneralSkill.tenant_id == tenant_id)
        .options(load_only(
            GeneralSkill.id, GeneralSkill.tenant_id, GeneralSkill.name,
            GeneralSkill.description, GeneralSkill.status,
            GeneralSkill.download_count, GeneralSkill.updated_at,
        ))
    ).all():
        if row.status != "published":
            continue
        if not is_open_gallery_resource(db, tenant_id, "general_skill", row, prefetch=prefetch):
            continue
        _push("general-skills", row.id, row.name, row.description or "",
              max(0, int(row.download_count or 0)), row.updated_at)

    # SOP：published + overall 绑定可见；热度 = 被复制到员工侧的分支数
    copy_counts: dict[str, int] = {}
    branch_query = select(
        AgentSkillBranch.source_skill_id, func.count(AgentSkillBranch.id)
    ).where(AgentSkillBranch.tenant_id == tenant_id)
    if overall_id:
        branch_query = branch_query.where(AgentSkillBranch.agent_id != overall_id)
    for source_id, count in db.exec(branch_query.group_by(AgentSkillBranch.source_skill_id)).all():
        copy_counts[source_id] = int(count or 0)
    for row in db.exec(
        select(Skill)
        .where(Skill.tenant_id == tenant_id)
        .options(load_only(
            Skill.id, Skill.tenant_id, Skill.name, Skill.description,
            Skill.status, Skill.updated_at,
        ))
    ).all():
        if row.status != "published":
            continue
        if not is_open_gallery_resource(db, tenant_id, "skill", row, prefetch=prefetch):
            continue
        _push("skills", row.id, row.name, row.description or "",
              copy_counts.get(row.id, 0), row.updated_at)

    # 知识库：active + 非空默认库 + overall 绑定可见；热度 = 被复制到员工侧的绑定数
    kb_copies: dict[str, int] = {}
    binding_query = select(
        AgentResourceBinding.resource_id, func.count(AgentResourceBinding.id)
    ).where(
        AgentResourceBinding.tenant_id == tenant_id,
        AgentResourceBinding.resource_type == "knowledge_base",
    )
    if overall_id:
        binding_query = binding_query.where(AgentResourceBinding.agent_id != overall_id)
    for resource_id, count in db.exec(binding_query.group_by(AgentResourceBinding.resource_id)).all():
        kb_copies[resource_id] = int(count or 0)
    kb_stats = _knowledge_base_stats(db, tenant_id)
    for row in db.exec(
        select(KnowledgeBase)
        .where(KnowledgeBase.tenant_id == tenant_id)
        .options(load_only(
            KnowledgeBase.id, KnowledgeBase.tenant_id, KnowledgeBase.name,
            KnowledgeBase.description, KnowledgeBase.status, KnowledgeBase.updated_at,
        ))
    ).all():
        if row.status != "active":
            continue
        if _is_empty_default_knowledge_base(row, kb_stats.get(row.id, {})):
            continue
        if not is_open_gallery_resource(db, tenant_id, "knowledge_base", row, prefetch=prefetch):
            continue
        _push("knowledge", row.id, row.name, row.description or "",
              kb_copies.get(row.id, 0), row.updated_at)

    items.sort(key=lambda row: (-row[0], row[1] is None, row[1] or datetime.min), )
    return [entry[2] for entry in items[:TOP_LIMIT]]


@router.get("", response_model=HotPayload, dependencies=[Depends(require_current_tenant)])
def plaza_hot(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: Any = Depends(get_current_user),
) -> HotPayload:
    """「全部」tab 热门资源 Top 10（真实使用/下载/复制计数）。"""
    ensure_tenant(db, tenant_id)
    return HotPayload(generated_at=datetime.now(), items=_scored_items(db, tenant_id, current_user))
