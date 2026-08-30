from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_private_knowledge_branch
from app.api.knowledge_bases import list_knowledge_bases
from app.db.models import (
    AgentProfile,
    KnowledgeBase,
    KnowledgeBaseVersion,
    Team,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
    TeamMember,
    Tenant,
)
from app.knowledge.access import KnowledgeAccessService
from app.knowledge.errors import KnowledgeError


@dataclass(frozen=True)
class _AccessFixture:
    """访问矩阵中的稳定标识，便于测试只关注可见性和权限。"""

    agent_id: str
    private_knowledge_base_id: str
    colleague_agent_id: str
    colleague_private_knowledge_base_id: str
    nonmember_agent_id: str
    team_a_id: str
    team_b_id: str
    shared_a_id: str
    shared_b_id: str


def _session() -> Session:
    """创建独立内存数据库，副作用仅限当前访问矩阵测试。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add_shared_base(
    db: Session,
    *,
    knowledge_base_id: str,
    version_id: str,
    name: str,
) -> KnowledgeBase:
    """创建带一个全局正式版本的共享知识库，并返回根记录。"""
    base = KnowledgeBase(
        id=knowledge_base_id,
        tenant_id="tenant_demo",
        name=name,
        mode="shared",
        published_version_id=version_id,
    )
    version = KnowledgeBaseVersion(
        id=version_id,
        tenant_id="tenant_demo",
        knowledge_base_id=knowledge_base_id,
        version="1.0.0",
        name=name,
        publication_state="released",
    )
    db.add(base)
    db.add(version)
    return base


def _seed_access_matrix(db: Session) -> _AccessFixture:
    """建立跨团队员工、同租户非成员与专用知识库的最小授权矩阵。"""
    agent_id = "agent_writer"
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(AgentProfile(id=agent_id, tenant_id="tenant_demo", name="内容员工"))
    colleague_agent_id = "agent_colleague"
    db.add(
        AgentProfile(
            id=colleague_agent_id,
            tenant_id="tenant_demo",
            name="同团队员工",
        )
    )
    db.add(
        AgentProfile(
            id="agent_other_tenant",
            tenant_id="tenant_other",
            name="其他租户员工",
        )
    )
    nonmember_agent_id = "agent_nonmember"
    db.add(
        AgentProfile(
            id=nonmember_agent_id,
            tenant_id="tenant_demo",
            name="同租户非团队成员",
            status="active",
        )
    )
    private_base = KnowledgeBase(
        id="kb_private",
        tenant_id="tenant_demo",
        name="员工私有资料",
        mode="dedicated",
    )
    db.add(private_base)
    db.flush()
    ensure_agent_private_knowledge_branch(
        db,
        "tenant_demo",
        agent_id,
        private_base,
    )
    colleague_private_base = KnowledgeBase(
        id="kb_private_colleague",
        tenant_id="tenant_demo",
        name="同事专用资料",
        mode="dedicated",
    )
    db.add(colleague_private_base)
    db.flush()
    ensure_agent_private_knowledge_branch(
        db,
        "tenant_demo",
        colleague_agent_id,
        colleague_private_base,
    )

    shared_a = _add_shared_base(
        db,
        knowledge_base_id="kb_shared_a",
        version_id="kbver_shared_a",
        name="项目 A 资料",
    )
    shared_b = _add_shared_base(
        db,
        knowledge_base_id="kb_shared_b",
        version_id="kbver_shared_b",
        name="项目 B 资料",
    )
    team_a = Team(
        id="team_a",
        tenant_id="tenant_demo",
        name="项目 A",
        owner_user_id="user_admin",
    )
    team_b = Team(
        id="team_b",
        tenant_id="tenant_demo",
        name="项目 B",
        owner_user_id="user_admin",
        default_knowledge_base_id=shared_b.id,
    )
    db.add(team_a)
    db.add(team_b)
    db.add(TeamMember(team_id=team_a.id, agent_id=agent_id))
    db.add(TeamMember(team_id=team_a.id, agent_id=colleague_agent_id))
    db.add(TeamMember(team_id=team_b.id, agent_id=agent_id))
    for team, base, permission in (
        (team_a, shared_a, "reader"),
        (team_b, shared_b, "editor"),
    ):
        db.add(
            TeamKnowledgeBaseBinding(
                tenant_id="tenant_demo",
                team_id=team.id,
                knowledge_base_id=base.id,
                created_by_user_id="user_admin",
            )
        )
        db.add(
            TeamKnowledgeBaseGrant(
                tenant_id="tenant_demo",
                team_id=team.id,
                knowledge_base_id=base.id,
                agent_id=agent_id,
                permission=permission,
                created_by_user_id="user_admin",
            )
        )
    db.commit()
    return _AccessFixture(
        agent_id=agent_id,
        private_knowledge_base_id=private_base.id,
        colleague_agent_id=colleague_agent_id,
        colleague_private_knowledge_base_id=colleague_private_base.id,
        nonmember_agent_id=nonmember_agent_id,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        shared_a_id=shared_a.id,
        shared_b_id=shared_b.id,
    )


def test_team_reads_own_dedicated_and_current_team_shared_knowledge() -> None:
    """群聊合并当前员工专用知识与本团队授权共享知识，私聊仍只含专用知识。"""
    with _session() as db:
        fixture = _seed_access_matrix(db)
        service = KnowledgeAccessService(db)

        team_a = service.resolve_projections(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            team_id=fixture.team_a_id,
        )
        team_b = service.resolve_projections(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            team_id=fixture.team_b_id,
        )
        private = service.resolve_projections(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            team_id=None,
        )

    assert {(item.knowledge_base_id, item.permission) for item in team_a} == {
        (fixture.private_knowledge_base_id, "publisher"),
        (fixture.shared_a_id, "reader"),
    }
    assert {(item.knowledge_base_id, item.permission) for item in team_b} == {
        (fixture.private_knowledge_base_id, "publisher"),
        (fixture.shared_b_id, "editor"),
    }
    assert {(item.knowledge_base_id, item.mode) for item in private} == {
        (fixture.private_knowledge_base_id, "dedicated")
    }
    assert all(item.team_id is None for item in private)
    assert fixture.shared_b_id not in {item.knowledge_base_id for item in team_a}
    assert fixture.shared_a_id not in {item.knowledge_base_id for item in team_b}
    assert fixture.colleague_private_knowledge_base_id not in {
        item.knowledge_base_id for item in (*team_a, *team_b, *private)
    }


def test_management_list_unions_dedicated_branches_with_granted_shared_bases() -> None:
    """管理页可同时维护员工专用库和获授权共享库，但不改变私聊运行时投影。"""
    with _session() as db:
        fixture = _seed_access_matrix(db)

        rows = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            db=db,
        )

    rows_by_id = {row.id: row for row in rows}
    assert set(rows_by_id) == {
        fixture.private_knowledge_base_id,
        fixture.shared_a_id,
        fixture.shared_b_id,
    }
    assert rows_by_id[fixture.private_knowledge_base_id].mode == "dedicated"
    assert rows_by_id[fixture.shared_a_id].published_version_id == "kbver_shared_a"
    assert rows_by_id[fixture.shared_b_id].published_version_id == "kbver_shared_b"
    assert rows_by_id[fixture.shared_a_id].bound_team_count == 1
    assert rows_by_id[fixture.shared_b_id].bound_team_count == 1
    assert rows_by_id[fixture.shared_a_id].management_context["team_ids"] == [
        fixture.team_a_id
    ]
    assert rows_by_id[fixture.shared_b_id].management_context["permissions"] == {
        fixture.team_b_id: "editor"
    }


def test_team_context_fails_closed_for_nonmembers_and_cross_tenant_agents() -> None:
    """团队成员或租户不匹配时拒绝整个上下文，不降级到私有或空权限。"""
    with _session() as db:
        fixture = _seed_access_matrix(db)
        service = KnowledgeAccessService(db)

        with pytest.raises(KnowledgeError) as nonmember:
            service.resolve_projections(
                tenant_id="tenant_demo",
                agent_id=fixture.nonmember_agent_id,
                team_id=fixture.team_a_id,
            )
        with pytest.raises(KnowledgeError) as cross_tenant:
            service.resolve_projections(
                tenant_id="tenant_demo",
                agent_id="agent_other_tenant",
                team_id=fixture.team_a_id,
            )

    assert getattr(nonmember.value, "code", None) == "KNOWLEDGE_CONTEXT_MISMATCH"
    assert getattr(cross_tenant.value, "code", None) == "KNOWLEDGE_CONTEXT_MISMATCH"


def test_write_target_uses_only_an_authorized_explicit_or_team_default_base() -> None:
    """写入仅解析显式目标或团队默认目标，并按权限等级快速失败。"""
    with _session() as db:
        fixture = _seed_access_matrix(db)
        service = KnowledgeAccessService(db)

        default_target = service.resolve_write_target(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            team_id=fixture.team_b_id,
        )
        with pytest.raises(KnowledgeError) as missing_default:
            service.resolve_write_target(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_a_id,
            )
        with pytest.raises(KnowledgeError) as insufficient:
            service.resolve_write_target(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_a_id,
                knowledge_base_id=fixture.shared_a_id,
            )
        with pytest.raises(KnowledgeError) as unrelated:
            service.resolve_write_target(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_b_id,
                knowledge_base_id=fixture.shared_a_id,
            )
        with pytest.raises(KnowledgeError) as dedicated_read_projection:
            service.resolve_write_target(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_b_id,
                knowledge_base_id=fixture.private_knowledge_base_id,
            )

    assert default_target.knowledge_base_id == fixture.shared_b_id
    assert default_target.is_default_write is True
    assert getattr(missing_default.value, "code", None) == (
        "KNOWLEDGE_DEFAULT_NOT_CONFIGURED"
    )
    assert getattr(insufficient.value, "code", None) == "KNOWLEDGE_GRANT_REQUIRED"
    assert getattr(unrelated.value, "code", None) == "KNOWLEDGE_GRANT_REQUIRED"
    assert getattr(dedicated_read_projection.value, "code", None) == (
        "KNOWLEDGE_GRANT_REQUIRED"
    )


def test_revoked_grant_is_removed_from_live_access_without_changing_the_frozen_version() -> None:
    """撤销授权后实时解析立即失效，先前保存的版本标识本身不被修改。"""
    with _session() as db:
        fixture = _seed_access_matrix(db)
        service = KnowledgeAccessService(db)
        before = next(
            projection
            for projection in service.resolve_projections(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_b_id,
            )
            if projection.knowledge_base_id == fixture.shared_b_id
        )
        grant = db.exec(
            select(TeamKnowledgeBaseGrant).where(
                TeamKnowledgeBaseGrant.team_id == fixture.team_b_id,
                TeamKnowledgeBaseGrant.agent_id == fixture.agent_id,
            )
        ).one()
        grant.status = "revoked"
        db.add(grant)
        db.commit()

        after = service.resolve_projections(
            tenant_id="tenant_demo",
            agent_id=fixture.agent_id,
            team_id=fixture.team_b_id,
        )
        with pytest.raises(KnowledgeError) as revoked:
            service.require_projection(
                tenant_id="tenant_demo",
                agent_id=fixture.agent_id,
                team_id=fixture.team_b_id,
                knowledge_base_id=fixture.shared_b_id,
            )

    assert before.knowledge_base_version_id == "kbver_shared_b"
    assert {item.knowledge_base_id for item in after} == {
        fixture.private_knowledge_base_id
    }
    assert getattr(revoked.value, "code", None) == "KNOWLEDGE_GRANT_REQUIRED"
