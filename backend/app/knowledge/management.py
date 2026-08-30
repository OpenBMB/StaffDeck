"""共享知识库的人类管理上下文校验。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.db.models import (
    KnowledgeBase,
    Team,
    TeamKnowledgeBaseBinding,
    User,
)
from app.knowledge.errors import (
    KNOWLEDGE_CONTEXT_MISMATCH,
    KNOWLEDGE_GRANT_REQUIRED,
    KNOWLEDGE_MODE_INVALID,
    knowledge_error,
)
from app.security.permissions import is_admin_user


@dataclass(frozen=True)
class SharedKnowledgeManagementContext:
    """已校验的人类团队、共享知识库及绑定组合。"""

    team: Team
    knowledge_base: KnowledgeBase
    binding: TeamKnowledgeBaseBinding


def require_team_knowledge_manager(
    db: Session,
    *,
    tenant_id: str,
    team_id: str,
    knowledge_base_id: str,
    current_user: User,
) -> SharedKnowledgeManagementContext:
    """要求同租户团队所有者或管理员通过活动绑定管理共享知识。"""
    if current_user.tenant_id != tenant_id:
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    team = db.get(Team, team_id)
    if team is None or team.tenant_id != tenant_id or team.status != "active":
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    if team.owner_user_id != current_user.id and not is_admin_user(current_user):
        raise knowledge_error(
            KNOWLEDGE_GRANT_REQUIRED,
            message="只有团队所有者或管理员可以维护团队共享知识库。",
        )
    base = db.get(KnowledgeBase, knowledge_base_id)
    if base is None or base.tenant_id != tenant_id or base.status != "active":
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    if base.mode != "shared":
        raise knowledge_error(KNOWLEDGE_MODE_INVALID)
    binding = db.exec(
        select(TeamKnowledgeBaseBinding).where(
            TeamKnowledgeBaseBinding.tenant_id == tenant_id,
            TeamKnowledgeBaseBinding.team_id == team_id,
            TeamKnowledgeBaseBinding.knowledge_base_id == knowledge_base_id,
            TeamKnowledgeBaseBinding.status == "active",
        )
    ).first()
    if binding is None:
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    return SharedKnowledgeManagementContext(
        team=team,
        knowledge_base=base,
        binding=binding,
    )


def require_shared_knowledge_history_viewer(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    current_user: User,
) -> KnowledgeBase:
    """允许租户管理员或任一活动绑定团队的所有者查看共享历史。"""
    if current_user.tenant_id != tenant_id:
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    base = db.get(KnowledgeBase, knowledge_base_id)
    if base is None or base.tenant_id != tenant_id or base.status != "active":
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    if base.mode != "shared":
        raise knowledge_error(KNOWLEDGE_MODE_INVALID)
    if is_admin_user(current_user):
        return base

    owned_team_ids = db.exec(
        select(Team.id).where(
            Team.tenant_id == tenant_id,
            Team.owner_user_id == current_user.id,
            Team.status == "active",
        )
    ).all()
    if owned_team_ids:
        binding = db.exec(
            select(TeamKnowledgeBaseBinding.id).where(
                TeamKnowledgeBaseBinding.tenant_id == tenant_id,
                TeamKnowledgeBaseBinding.team_id.in_(owned_team_ids),
                TeamKnowledgeBaseBinding.knowledge_base_id == knowledge_base_id,
                TeamKnowledgeBaseBinding.status == "active",
            )
        ).first()
        if binding:
            return base
    raise knowledge_error(KNOWLEDGE_GRANT_REQUIRED)
