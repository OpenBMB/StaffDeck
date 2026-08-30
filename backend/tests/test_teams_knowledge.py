from __future__ import annotations

import pytest
from sqlmodel import Session, select
from test_teams_api import _seed_agents, _test_session

from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseAuditEvent,
    KnowledgeBaseVersion,
    Team,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
)
from app.knowledge.errors import KnowledgeError
from app.teams.schema import TeamKnowledgeSelection, TeamSharedKnowledgeCreate
from app.teams.service import (
    add_member,
    bind_team_knowledge_base,
    create_team,
    list_team_knowledge_bindings,
    remove_member,
    replace_team_knowledge_grants,
    set_team_default_knowledge_base,
    unbind_team_knowledge_base,
)


def _add_shared_base(
    db: Session,
    *,
    base_id: str,
    name: str,
    tenant_id: str = "tenant_demo",
) -> KnowledgeBase:
    """创建可被团队绑定的最小共享库和初始正式版本。"""
    version_id = f"kbver_{base_id}"
    base = KnowledgeBase(
        id=base_id,
        tenant_id=tenant_id,
        name=name,
        mode="shared",
        published_version_id=version_id,
    )
    version = KnowledgeBaseVersion(
        id=version_id,
        tenant_id=tenant_id,
        knowledge_base_id=base_id,
        version="1.0.0",
        name=name,
        publication_state="released",
    )
    db.add(base)
    db.add(version)
    db.flush()
    return base


def test_create_team_with_knowledge_is_atomic_and_sets_one_default() -> None:
    """团队、新共享库、绑定与默认目标要么一起成功，要么全部回滚。"""
    with _test_session() as db:
        _seed_agents(db)
        existing = _add_shared_base(db, base_id="kb_existing", name="现有共享库")
        foreign = _add_shared_base(
            db,
            base_id="kb_foreign",
            name="其他租户共享库",
            tenant_id="tenant_other",
        )
        db.commit()

        team = create_team(
            db,
            tenant_id="tenant_demo",
            name="内容团队",
            description=None,
            owner_user_id="user_admin",
            knowledge_bases=[
                TeamKnowledgeSelection(
                    existing_knowledge_base_id=existing.id,
                    is_default=True,
                ),
                TeamKnowledgeSelection(
                    create_shared=TeamSharedKnowledgeCreate(name="团队新共享库"),
                ),
            ],
        )

        bindings = list_team_knowledge_bindings(db, team)
        assert len(bindings) == 2
        assert team.default_knowledge_base_id == existing.id
        created = db.exec(
            select(KnowledgeBase).where(KnowledgeBase.name == "团队新共享库")
        ).one()
        assert created.mode == "shared"
        assert created.published_version_id

        with pytest.raises(KnowledgeError):
            create_team(
                db,
                tenant_id="tenant_demo",
                name="应回滚团队",
                description=None,
                owner_user_id="user_admin",
                knowledge_bases=[
                    TeamKnowledgeSelection(
                        create_shared=TeamSharedKnowledgeCreate(name="应回滚共享库"),
                    ),
                    TeamKnowledgeSelection(existing_knowledge_base_id=foreign.id),
                ],
            )

        assert db.exec(select(Team).where(Team.name == "应回滚团队")).first() is None
        assert db.exec(
            select(KnowledgeBase).where(KnowledgeBase.name == "应回滚共享库")
        ).first() is None


def test_bind_default_and_unbind_are_revision_protected() -> None:
    """绑定、默认切换与解绑都必须校验当前修订号并原子撤销授权。"""
    with _test_session() as db:
        _seed_agents(db)
        base = _add_shared_base(db, base_id="kb_policy", name="制度库")
        team = create_team(
            db,
            tenant_id="tenant_demo",
            name="制度团队",
            description=None,
            owner_user_id="user_admin",
        )
        add_member(db, team, agent_id="agent_worker")

        binding = bind_team_knowledge_base(
            db,
            team=team,
            selection=TeamKnowledgeSelection(existing_knowledge_base_id=base.id),
            actor_user_id="user_admin",
        )
        assert binding.revision == 1

        binding = set_team_default_knowledge_base(
            db,
            team=team,
            knowledge_base_id=base.id,
            is_default=True,
            expected_revision=1,
            actor_user_id="user_admin",
        )
        assert binding.revision == 2
        assert team.default_knowledge_base_id == base.id

        replace_team_knowledge_grants(
            db,
            team=team,
            knowledge_base_id=base.id,
            expected_revision=2,
            grants={"agent_worker": "editor"},
            actor_user_id="user_admin",
        )
        with pytest.raises(KnowledgeError) as stale:
            unbind_team_knowledge_base(
                db,
                team=team,
                knowledge_base_id=base.id,
                expected_revision=2,
                actor_user_id="user_admin",
            )
        assert stale.value.code == "KNOWLEDGE_BINDING_REVISION_CONFLICT"

        binding = unbind_team_knowledge_base(
            db,
            team=team,
            knowledge_base_id=base.id,
            expected_revision=3,
            actor_user_id="user_admin",
        )
        db.refresh(team)
        grant = db.exec(select(TeamKnowledgeBaseGrant)).one()
        assert binding.status == "revoked"
        assert binding.revision == 4
        assert grant.status == "revoked"
        assert team.default_knowledge_base_id is None


def test_grant_matrix_replacement_is_atomic_and_revision_protected() -> None:
    """权限矩阵整批替换，遗漏成员视为撤权，非法成员不能留下半份更新。"""
    with _test_session() as db:
        _seed_agents(db)
        base = _add_shared_base(db, base_id="kb_content", name="内容库")
        team = create_team(
            db,
            tenant_id="tenant_demo",
            name="内容权限团队",
            description=None,
            owner_user_id="user_admin",
        )
        add_member(db, team, agent_id="agent_tl", role="leader")
        add_member(db, team, agent_id="agent_worker")
        binding = bind_team_knowledge_base(
            db,
            team=team,
            selection=TeamKnowledgeSelection(existing_knowledge_base_id=base.id),
            actor_user_id="user_admin",
        )

        binding = replace_team_knowledge_grants(
            db,
            team=team,
            knowledge_base_id=base.id,
            expected_revision=binding.revision,
            grants={"agent_tl": "reader", "agent_worker": "editor"},
            actor_user_id="user_admin",
        )
        assert binding.revision == 2

        with pytest.raises(KnowledgeError) as stale:
            replace_team_knowledge_grants(
                db,
                team=team,
                knowledge_base_id=base.id,
                expected_revision=1,
                grants={"agent_tl": "publisher"},
                actor_user_id="user_admin",
            )
        assert stale.value.code == "KNOWLEDGE_BINDING_REVISION_CONFLICT"

        binding = replace_team_knowledge_grants(
            db,
            team=team,
            knowledge_base_id=base.id,
            expected_revision=2,
            grants={"agent_worker": "publisher"},
            actor_user_id="user_admin",
        )
        assert binding.revision == 3
        grants = db.exec(
            select(TeamKnowledgeBaseGrant).order_by(TeamKnowledgeBaseGrant.agent_id)
        ).all()
        assert [(row.agent_id, row.permission, row.status) for row in grants] == [
            ("agent_tl", "reader", "revoked"),
            ("agent_worker", "publisher", "active"),
        ]

        with pytest.raises(KnowledgeError):
            replace_team_knowledge_grants(
                db,
                team=team,
                knowledge_base_id=base.id,
                expected_revision=3,
                grants={"agent_worker": "reader", "agent_worker2": "editor"},
                actor_user_id="user_admin",
            )
        db.refresh(binding)
        active = db.exec(
            select(TeamKnowledgeBaseGrant).where(
                TeamKnowledgeBaseGrant.status == "active"
            )
        ).one()
        assert binding.revision == 3
        assert (active.agent_id, active.permission) == ("agent_worker", "publisher")

        actions = {
            event.action for event in db.exec(select(KnowledgeBaseAuditEvent)).all()
        }
        assert {"binding_created", "grant_created", "grant_changed", "grant_revoked"} <= actions


def test_member_removal_revokes_only_that_teams_grants() -> None:
    """复用员工从一个团队移除后仅撤销该团队授权，其他团队保持不变。"""
    with _test_session() as db:
        _seed_agents(db)
        base = _add_shared_base(db, base_id="kb_reused", name="复用共享库")
        team_a = create_team(
            db,
            tenant_id="tenant_demo",
            name="项目 A",
            description=None,
            owner_user_id="user_admin",
        )
        team_b = create_team(
            db,
            tenant_id="tenant_demo",
            name="项目 B",
            description=None,
            owner_user_id="user_admin",
        )
        for team in (team_a, team_b):
            add_member(db, team, agent_id="agent_worker")
            binding = bind_team_knowledge_base(
                db,
                team=team,
                selection=TeamKnowledgeSelection(existing_knowledge_base_id=base.id),
                actor_user_id="user_admin",
            )
            replace_team_knowledge_grants(
                db,
                team=team,
                knowledge_base_id=base.id,
                expected_revision=binding.revision,
                grants={"agent_worker": "editor"},
                actor_user_id="user_admin",
            )

        remove_member(db, team_a, "agent_worker")

        grants = db.exec(
            select(TeamKnowledgeBaseGrant).where(
                TeamKnowledgeBaseGrant.agent_id == "agent_worker"
            )
        ).all()
        status_by_team = {grant.team_id: grant.status for grant in grants}
        assert status_by_team == {team_a.id: "revoked", team_b.id: "active"}
        binding_a = db.exec(
            select(TeamKnowledgeBaseBinding).where(
                TeamKnowledgeBaseBinding.team_id == team_a.id
            )
        ).one()
        binding_b = db.exec(
            select(TeamKnowledgeBaseBinding).where(
                TeamKnowledgeBaseBinding.team_id == team_b.id
            )
        ).one()
        assert binding_a.revision == 3
        assert binding_b.revision == 2
