from __future__ import annotations

import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.agents import list_agents
from app.api.knowledge_bases import list_knowledge_bases
from app.db import staffdeck_seed
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.db.seed import seed_demo_data
from app.skills.skill_schema import SkillCard

EXPECTED_KNOWLEDGE_COUNTS = {
    "IT": 2,
    "人事": 3,
    "法务": 4,
    "行政": 2,
    "财务": 3,
    "销售": 1,
    "市场": 1,
    "采购": 1,
    "项目管理": 1,
    "数据分析": 1,
}

EXPECTED_EXPANDED_EMPLOYEE_PROFILES = {
    "销售": ("客户拓展顾问", "sales-handshake", "sales-advisor"),
    "市场": ("市场内容策划", "marketing-spark", "marketing-planner"),
    "采购": ("采购协同专员", "procurement-check", "procurement-coordinator"),
    "项目管理": ("项目推进经理", "project-board", "project-manager"),
    "数据分析": ("经营分析师", "data-insight", "data-analyst"),
}


class _FlushOnlySession:
    def flush(self) -> None:
        pass


def _seeded_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    seed_demo_data(session)
    session.commit()
    return session


def test_staffdeck_seed_reads_fixture_as_utf8(monkeypatch) -> None:
    class FakeFixturePath:
        def exists(self) -> bool:
            return True

        def read_text(self, *, encoding=None) -> str:
            assert encoding == "utf-8"
            return "{}"

    monkeypatch.setattr(staffdeck_seed, "FIXTURE_PATH", FakeFixturePath())
    monkeypatch.setattr(staffdeck_seed, "_seed_agents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_skills", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_general_skills", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_tools", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_knowledge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_agent_resource_bindings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_skill_branches", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_seed_knowledge_branches", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_publish_gallery_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staffdeck_seed, "_sync_seed_agents_to_current_admin", lambda *_args, **_kwargs: None)

    staffdeck_seed.seed_staffdeck_admin_gallery(_FlushOnlySession())


def test_staffdeck_seed_requires_every_bundled_fixture(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        staffdeck_seed._load_seed_fixtures((tmp_path / "missing-fixture.json",))


def test_expanded_staffdeck_skills_match_runtime_schema() -> None:
    data = json.loads(staffdeck_seed.EXPANDED_FIXTURE_PATH.read_text(encoding="utf-8"))

    # 每个预置员工一张卡；此外允许绑定到主种子员工的策划卡（如飞书审批提交）。
    curated_extra_skill_ids = {"lark_approval_submit"}
    for key in ("skills", "skill_versions"):
        rows = data[key]
        assert len(rows) == (
            len(EXPECTED_EXPANDED_EMPLOYEE_PROFILES) + len(curated_extra_skill_ids)
        )
        assert curated_extra_skill_ids <= {row["skill_id"] for row in rows}
        for row in rows:
            SkillCard.model_validate(row["content_json"])
    for key in ("agent_skill_branches", "agent_skill_branch_versions"):
        rows = data[key]
        assert len(rows) == len(EXPECTED_EXPANDED_EMPLOYEE_PROFILES)
        for row in rows:
            SkillCard.model_validate(row["content_json"])


def test_staffdeck_seed_exposes_selected_agents_with_knowledge_bases() -> None:
    with _seeded_session() as db:
        admin = db.exec(
            select(User).where(User.tenant_id == "tenant_demo", User.username == "admin")
        ).one()
        agents = {
            agent.name: agent
            for agent in list_agents("tenant_demo", db=db, current_user=admin)
            if agent.name in EXPECTED_KNOWLEDGE_COUNTS
        }

        assert set(agents) == set(EXPECTED_KNOWLEDGE_COUNTS)
        assert not db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "默认智能体",
                AgentProfile.status == "active",
            )
        ).first()
        for name, expected_count in EXPECTED_KNOWLEDGE_COUNTS.items():
            agent = agents[name]
            bound_count = sum(
                1
                for resource in agent.resources
                if resource.resource_type == "knowledge_base" and resource.status == "active"
            )
            scoped_knowledge = list_knowledge_bases("tenant_demo", agent.id, db=db)

            assert bound_count == expected_count
            assert len(scoped_knowledge) == expected_count
            assert all(item.document_count > 0 for item in scoped_knowledge)
            assert all(item.chunk_count > 0 for item in scoped_knowledge)


def test_staffdeck_seed_adds_expanded_employee_profiles_idempotently() -> None:
    with _seeded_session() as db:
        seed_demo_data(db)
        db.commit()

        rows = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name.in_(EXPECTED_EXPANDED_EMPLOYEE_PROFILES.keys()),
            )
        ).all()

        assert len(rows) == len(EXPECTED_EXPANDED_EMPLOYEE_PROFILES)
        for row in rows:
            role_name, avatar_preset, role_key = EXPECTED_EXPANDED_EMPLOYEE_PROFILES[row.name]
            assert row.metadata_json["role_name"] == role_name
            assert row.metadata_json["avatar_preset"] == avatar_preset
            assert row.metadata_json["role_key"] == role_key
            assert row.metadata_json["managed_by_seed"] is True

            bindings = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == "tenant_demo",
                    AgentResourceBinding.agent_id == row.id,
                    AgentResourceBinding.status == "active",
                )
            ).all()
            assert sorted(binding.resource_type for binding in bindings) == [
                "knowledge_base",
                "skill",
            ]

            knowledge_base_id = next(
                binding.resource_id
                for binding in bindings
                if binding.resource_type == "knowledge_base"
            )
            skill_id = next(
                binding.resource_id for binding in bindings if binding.resource_type == "skill"
            )
            assert len(
                db.exec(
                    select(KnowledgeBaseVersion).where(
                        KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id
                    )
                ).all()
            ) == 1
            for model in (
                KnowledgeDocument,
                KnowledgeBucket,
                KnowledgeChunk,
                KnowledgeIngestJob,
            ):
                assert len(
                    db.exec(
                        select(model).where(model.knowledge_base_id == knowledge_base_id)
                    ).all()
                ) == 1

            skill = db.get(Skill, skill_id)
            assert skill is not None
            assert len(
                db.exec(
                    select(AgentSkillBranch).where(
                        AgentSkillBranch.agent_id == row.id,
                        AgentSkillBranch.skill_id == skill.skill_id,
                    )
                ).all()
            ) == 1
            assert len(
                db.exec(
                    select(AgentSkillBranchVersion).where(
                        AgentSkillBranchVersion.agent_id == row.id,
                        AgentSkillBranchVersion.skill_id == skill.skill_id,
                    )
                ).all()
            ) == 1


def test_staffdeck_seed_applies_reliability_defaults_to_existing_rows() -> None:
    with _seeded_session() as db:
        archive_tool = db.exec(
            select(Tool).where(
                Tool.tenant_id == "tenant_demo",
                Tool.name == "contract.archive_query",
            )
        ).one()
        leave_skill = db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id == "leave_apply_v1",
            )
        ).one()
        policy_node = next(
            node
            for node in leave_skill.content_json.get("nodes", [])
            if node.get("node_id") == "check_policy"
        )

        assert archive_tool.config_json["execution"] == {"timeout_seconds": 20}
        assert policy_node["knowledge_scope"]["query_fields"] == ["leave_type"]


def test_staffdeck_seed_uses_existing_admin_id_for_seeded_agents() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            User(
                id="user_existing_admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="Existing Admin",
                role="admin",
                password_hash="test",
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        rows = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name.in_(EXPECTED_KNOWLEDGE_COUNTS.keys()),
            )
        ).all()

        assert len(rows) == len(EXPECTED_KNOWLEDGE_COUNTS)
        assert {
            row.metadata_json.get("owner_user_id") for row in rows
        } == {"user_existing_admin"}


def test_staffdeck_seed_preserves_custom_avatar_on_restart() -> None:
    with _seeded_session() as db:
        agent = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == "tenant_demo",
                AgentProfile.name == "IT",
            )
        ).one()
        metadata = dict(agent.metadata_json)
        metadata.update(
            {
                "avatar_kind": "upload",
                "avatar_image": "data:image/png;base64,CUSTOM",
                "avatar_preset": "quality-star",
                "avatar_text": "I",
                "avatar_tone": "blue",
            }
        )
        agent.metadata_json = metadata
        db.add(agent)
        db.commit()

        seed_demo_data(db)
        db.commit()
        db.refresh(agent)

        assert agent.metadata_json["avatar_kind"] == "upload"
        assert agent.metadata_json["avatar_image"] == "data:image/png;base64,CUSTOM"
        assert agent.metadata_json["avatar_preset"] == "quality-star"
        assert agent.metadata_json["avatar_text"] == "I"
        assert agent.metadata_json["avatar_tone"] == "blue"


def test_staffdeck_seed_does_not_overwrite_non_seed_employee_name_conflict() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            User(
                id="admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="Administrator",
                role="admin",
                password_hash="test",
            )
        )
        db.add(
            AgentProfile(
                id="agent_custom_it",
                tenant_id="tenant_demo",
                name="IT",
                description="用户原有的 IT 员工",
                status="active",
                metadata_json={
                    "owner_user_id": "user_custom",
                    "owner_username": "custom",
                    "created_by": "custom",
                },
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        row = db.get(AgentProfile, "agent_custom_it")

        assert row is not None
        assert row.description == "用户原有的 IT 员工"
        assert row.metadata_json.get("owner_user_id") == "user_custom"
        assert row.metadata_json.get("seed_source") is None


def test_staffdeck_seed_archives_legacy_default_agent() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo Enterprise"))
        db.add(
            AgentProfile(
                id="agent_tenant_demo_default",
                tenant_id="tenant_demo",
                name="默认智能体",
                description="默认对话可见域",
                status="active",
            )
        )
        db.commit()

        seed_demo_data(db)
        db.commit()

        row = db.get(AgentProfile, "agent_tenant_demo_default")
        admin = db.exec(
            select(User).where(User.tenant_id == "tenant_demo", User.username == "admin")
        ).one()
        listed_ids = {agent.id for agent in list_agents("tenant_demo", db=db, current_user=admin)}

        assert row is not None
        assert row.status == "archived"
        assert row.metadata_json.get("hidden_from_staffdeck") is True
        assert row.metadata_json.get("is_default_employee") is True
        assert "agent_tenant_demo_default" not in listed_ids
