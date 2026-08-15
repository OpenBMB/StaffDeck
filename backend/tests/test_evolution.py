import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    is_open_gallery_resource,
)
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    EvolutionProposal,
    GeneralSkill,
    Tenant,
    User,
)
from app.evolution.service import EvolutionService, _json_diff, _risk_for_sop_diff
from app.evolution.schema import EvolutionAnalyzeRequest


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_json_diff_is_a_stable_json_patch_style_list() -> None:
    changes = _json_diff(
        {"nodes": [{"node_id": "collect", "instruction": "old"}], "enabled": True},
        {"nodes": [{"node_id": "collect", "instruction": "new"}], "enabled": True},
    )

    assert changes == [
        {
            "op": "replace",
            "path": "/nodes/0/instruction",
            "before": "old",
            "after": "new",
        }
    ]
    assert _risk_for_sop_diff(changes) == "medium"


def test_approve_gallery_skill_creates_employee_private_copy_and_can_rollback() -> None:
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_test",
            username="owner",
            password_hash="x",
        )
        overall = AgentProfile(
            id="agent_overall",
            tenant_id="tenant_test",
            name="开放广场",
            is_overall=True,
        )
        agent = AgentProfile(
            id="agent_finance",
            tenant_id="tenant_test",
            name="财务员工",
            metadata_json={"owner_user_id": owner.id},
        )
        source = GeneralSkill(
            id="genskill_policy",
            tenant_id="tenant_test",
            slug="policy-answer",
            name="政策答疑",
            skill_markdown="# 政策答疑\n旧说明\n",
            status="published",
        )
        db.add(owner)
        db.add(overall)
        db.add(agent)
        db.add(source)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_test", "general_skill", source.id, "active")
        ensure_private_resource_binding(
            db,
            "tenant_test",
            agent.id,
            "general_skill",
            source.id,
            "active",
        )
        proposal = EvolutionProposal(
            id="evo_private_copy",
            tenant_id="tenant_test",
            agent_id=agent.id,
            resource_type="general_skill",
            resource_id=source.id,
            resource_key=source.slug,
            resource_name=source.name,
            status="ready_for_review",
            hypothesis="指令不够明确",
            candidate_json={
                "skill_markdown": "# 政策答疑\n只引用正式政策回答。\n",
                "description": source.description,
            },
            diff_json=[
                {
                    "op": "replace",
                    "path": "/skill_markdown",
                    "before": source.skill_markdown,
                    "after": "# 政策答疑\n只引用正式政策回答。\n",
                }
            ],
            created_by_user_id=owner.id,
        )
        db.add(proposal)
        db.commit()

        published = EvolutionService(db).approve(proposal, owner)
        db.refresh(source)

        assert published.status == "published"
        assert published.resource_id != source.id
        assert source.skill_markdown == "# 政策答疑\n旧说明\n"
        private = db.get(GeneralSkill, published.resource_id)
        assert private is not None
        assert private.skill_markdown == "# 政策答疑\n只引用正式政策回答。\n"
        assert not is_open_gallery_resource(db, "tenant_test", "general_skill", private)
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == private.id,
            )
        ).first()
        assert binding is not None
        assert binding.status == "active"

        rolled_back = EvolutionService(db).rollback(published, owner)
        db.refresh(private)
        db.refresh(binding)
        assert rolled_back.status == "rolled_back"
        assert private.status == "archived"
        assert binding.status == "inactive"


def test_candidate_does_not_modify_private_skill_before_approval() -> None:
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        skill = GeneralSkill(
            id="genskill_private",
            tenant_id="tenant_test",
            slug="private-skill",
            name="Private",
            skill_markdown="original",
            status="published",
        )
        proposal = EvolutionProposal(
            tenant_id="tenant_test",
            agent_id="agent_private",
            resource_type="general_skill",
            resource_id=skill.id,
            resource_key=skill.slug,
            resource_name=skill.name,
            status="ready_for_review",
            candidate_json={"skill_markdown": "candidate"},
            diff_json=[
                {
                    "op": "replace",
                    "path": "/skill_markdown",
                    "before": "original",
                    "after": "candidate",
                }
            ],
            created_by_user_id="user_owner",
        )
        db.add(skill)
        db.add(proposal)
        db.commit()

        db.refresh(skill)
        assert skill.skill_markdown == "original"


def test_analyze_without_feedback_returns_localizable_error_code() -> None:
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_test",
            username="owner",
            password_hash="x",
        )
        agent = AgentProfile(
            id="agent_empty",
            tenant_id="tenant_test",
            name="Empty",
            metadata_json={"owner_user_id": owner.id},
        )
        db.add(owner)
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as caught:
            EvolutionService(db).analyze(
                agent.id,
                EvolutionAnalyzeRequest(tenant_id="tenant_test"),
                owner,
            )

        assert caught.value.status_code == 404
        assert caught.value.detail == {
            "code": "EVOLUTION_FEEDBACK_NOT_FOUND",
            "message": "未找到可用于改进的 Skill 或 SOP 反馈",
        }
