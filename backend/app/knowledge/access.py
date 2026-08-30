"""知识库访问解析器：按可信会话上下文组合员工专用读取与团队共享授权。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlmodel import Session, select

from app.agents.branching import visible_knowledge_base_versions
from app.db.models import (
    AgentProfile,
    KnowledgeBase,
    KnowledgeBaseVersion,
    Team,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
    TeamMember,
)
from app.knowledge.errors import (
    KNOWLEDGE_CONTEXT_MISMATCH,
    KNOWLEDGE_DEFAULT_NOT_CONFIGURED,
    KNOWLEDGE_GRANT_REQUIRED,
    knowledge_error,
)

KnowledgePermission = Literal["reader", "editor", "publisher"]
_PERMISSION_RANK: dict[str, int] = {
    "reader": 1,
    "editor": 2,
    "publisher": 3,
}


@dataclass(frozen=True)
class KnowledgeAccessProjection:
    """一次解析得到的授权知识库及本轮应使用的具体版本。"""

    knowledge_base_id: str
    knowledge_base_version_id: str
    mode: Literal["dedicated", "shared"]
    permission: KnowledgePermission
    team_id: str | None
    is_default_write: bool


class KnowledgeAccessService:
    """集中校验租户、员工、团队、绑定与授权，并返回可执行的访问投影。"""

    def __init__(self, db: Session) -> None:
        """绑定调用方数据库会话；解析可能复用现有专用分支的版本补齐逻辑。"""
        self.db = db

    def resolve_projections(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        team_id: str | None,
    ) -> list[KnowledgeAccessProjection]:
        """私聊只返回员工专用投影；群聊再合并当前团队授权的共享正式版本。"""
        self._require_active_agent(tenant_id, agent_id)
        if team_id is None:
            return self._private_projections(tenant_id, agent_id)

        # 先验证团队上下文，避免无效团队或非成员请求降级为员工专用读取。
        team = self._require_active_team_context(tenant_id, agent_id, team_id)

        # 再合并当前员工自己的专用投影与当前团队授权共享投影，不读取其他团队或员工。
        projections = [
            *self._private_projections(tenant_id, agent_id),
            *self._team_projections(tenant_id, agent_id, team),
        ]
        return sorted(projections, key=lambda item: item.knowledge_base_id)

    def require_projection(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        team_id: str | None,
        knowledge_base_id: str,
        minimum_permission: KnowledgePermission = "reader",
    ) -> KnowledgeAccessProjection:
        """返回指定知识库投影，并在不可见或权限不足时统一按授权失败处理。"""
        projections = self.resolve_projections(
            tenant_id=tenant_id,
            agent_id=agent_id,
            team_id=team_id,
        )
        projection = next(
            (
                item
                for item in projections
                if item.knowledge_base_id == knowledge_base_id
            ),
            None,
        )
        if projection is None or not self.permission_allows(
            projection.permission,
            minimum_permission,
        ):
            raise knowledge_error(KNOWLEDGE_GRANT_REQUIRED)
        return projection

    def require_shared_projection(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        team_id: str | None,
        knowledge_base_id: str,
        minimum_permission: KnowledgePermission = "reader",
    ) -> KnowledgeAccessProjection:
        """返回满足权限的共享投影；员工专用投影只可读取，不能充当共享维护目标。"""
        projection = self.require_projection(
            tenant_id=tenant_id,
            agent_id=agent_id,
            team_id=team_id,
            knowledge_base_id=knowledge_base_id,
            minimum_permission=minimum_permission,
        )
        if projection.mode != "shared":
            raise knowledge_error(KNOWLEDGE_GRANT_REQUIRED)
        return projection

    def resolve_write_target(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        team_id: str,
        knowledge_base_id: str | None = None,
        minimum_permission: KnowledgePermission = "editor",
    ) -> KnowledgeAccessProjection:
        """只从显式目标或该团队默认目标解析写入库；没有默认时不做任何猜测。"""
        team = self._require_active_team_context(tenant_id, agent_id, team_id)
        target_id = str(knowledge_base_id or team.default_knowledge_base_id or "").strip()
        if not target_id:
            raise knowledge_error(KNOWLEDGE_DEFAULT_NOT_CONFIGURED)
        return self.require_shared_projection(
            tenant_id=tenant_id,
            agent_id=agent_id,
            team_id=team_id,
            knowledge_base_id=target_id,
            minimum_permission=minimum_permission,
        )

    @staticmethod
    def permission_allows(
        actual: str,
        required: KnowledgePermission,
    ) -> bool:
        """按 reader、editor、publisher 的包含关系判断实际权限是否满足要求。"""
        return _PERMISSION_RANK.get(actual, 0) >= _PERMISSION_RANK[required]

    def _require_active_agent(self, tenant_id: str, agent_id: str) -> AgentProfile:
        """确认员工存在、启用且属于当前租户；失败不透露其他租户员工信息。"""
        agent = self.db.get(AgentProfile, agent_id)
        if (
            agent is None
            or agent.tenant_id != tenant_id
            or agent.status != "active"
        ):
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        return agent

    def _require_active_team_context(
        self,
        tenant_id: str,
        agent_id: str,
        team_id: str,
    ) -> Team:
        """确认团队启用、租户一致且员工仍是成员，作为所有团队知识动作的前置门禁。"""
        self._require_active_agent(tenant_id, agent_id)
        team = self.db.get(Team, team_id)
        if team is None or team.tenant_id != tenant_id or team.status != "active":
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        member = self.db.exec(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.agent_id == agent_id,
            )
        ).first()
        if member is None:
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        return team

    def _private_projections(
        self,
        tenant_id: str,
        agent_id: str,
    ) -> list[KnowledgeAccessProjection]:
        """复用现有员工分支解析，并过滤掉任何共享模式根记录。"""
        visible_versions = visible_knowledge_base_versions(
            self.db,
            tenant_id,
            agent_id,
        )
        projections: list[KnowledgeAccessProjection] = []
        for knowledge_base_id, version in visible_versions.items():
            knowledge_base = self.db.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None or knowledge_base.mode != "dedicated":
                continue
            projections.append(
                KnowledgeAccessProjection(
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    mode="dedicated",
                    permission="publisher",
                    team_id=None,
                    is_default_write=False,
                )
            )
        return sorted(projections, key=lambda item: item.knowledge_base_id)

    def _team_projections(
        self,
        tenant_id: str,
        agent_id: str,
        team: Team,
    ) -> list[KnowledgeAccessProjection]:
        """只解析当前团队的有效共享绑定、当前员工授权及全局正式版本。"""
        bindings = self.db.exec(
            select(TeamKnowledgeBaseBinding).where(
                TeamKnowledgeBaseBinding.tenant_id == tenant_id,
                TeamKnowledgeBaseBinding.team_id == team.id,
                TeamKnowledgeBaseBinding.status == "active",
            )
        ).all()
        grants = self.db.exec(
            select(TeamKnowledgeBaseGrant).where(
                TeamKnowledgeBaseGrant.tenant_id == tenant_id,
                TeamKnowledgeBaseGrant.team_id == team.id,
                TeamKnowledgeBaseGrant.agent_id == agent_id,
                TeamKnowledgeBaseGrant.status == "active",
            )
        ).all()
        grant_by_base = {grant.knowledge_base_id: grant for grant in grants}
        projections: list[KnowledgeAccessProjection] = []
        for binding in bindings:
            grant = grant_by_base.get(binding.knowledge_base_id)
            if grant is None or grant.permission not in _PERMISSION_RANK:
                continue
            knowledge_base = self.db.get(KnowledgeBase, binding.knowledge_base_id)
            if (
                knowledge_base is None
                or knowledge_base.tenant_id != tenant_id
                or knowledge_base.status != "active"
                or knowledge_base.mode != "shared"
                or not knowledge_base.published_version_id
            ):
                continue
            version = self.db.get(
                KnowledgeBaseVersion,
                knowledge_base.published_version_id,
            )
            if (
                version is None
                or version.tenant_id != tenant_id
                or version.knowledge_base_id != knowledge_base.id
                or version.publication_state != "released"
            ):
                continue
            projections.append(
                KnowledgeAccessProjection(
                    knowledge_base_id=knowledge_base.id,
                    knowledge_base_version_id=version.id,
                    mode="shared",
                    permission=grant.permission,
                    team_id=team.id,
                    is_default_write=(
                        team.default_knowledge_base_id == knowledge_base.id
                    ),
                )
            )
        return sorted(projections, key=lambda item: item.knowledge_base_id)
