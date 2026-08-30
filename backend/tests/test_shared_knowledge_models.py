from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import models as db_models
from app.db.models import KnowledgeBase, KnowledgeBaseVersion, Team


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_existing_knowledge_defaults_to_dedicated_without_a_published_pointer() -> None:
    knowledge_base = KnowledgeBase(tenant_id="tenant_demo", name="私有资料")
    version = KnowledgeBaseVersion(
        tenant_id="tenant_demo",
        knowledge_base_id=knowledge_base.id,
        name=knowledge_base.name,
    )
    team = Team(
        tenant_id="tenant_demo",
        name="项目 A",
        owner_user_id="user_admin",
    )

    assert knowledge_base.mode == "dedicated"
    assert knowledge_base.published_version_id is None
    assert version.publication_state == "released"
    assert version.parent_version_id is None
    assert team.default_knowledge_base_id is None


def test_team_binding_and_grant_identity_are_unique_per_context() -> None:
    assert hasattr(db_models, "TeamKnowledgeBaseBinding")
    assert hasattr(db_models, "TeamKnowledgeBaseGrant")
    binding_model = db_models.TeamKnowledgeBaseBinding
    grant_model = db_models.TeamKnowledgeBaseGrant

    with _test_session() as db:
        db.add(
            binding_model(
                tenant_id="tenant_demo",
                team_id="team_a",
                knowledge_base_id="kb_shared",
                created_by_user_id="user_admin",
            )
        )
        db.add(
            grant_model(
                tenant_id="tenant_demo",
                team_id="team_a",
                knowledge_base_id="kb_shared",
                agent_id="agent_editor",
                permission="editor",
                created_by_user_id="user_admin",
            )
        )
        db.commit()

        db.add(
            binding_model(
                tenant_id="tenant_demo",
                team_id="team_a",
                knowledge_base_id="kb_shared",
                created_by_user_id="user_admin",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(
            grant_model(
                tenant_id="tenant_demo",
                team_id="team_a",
                knowledge_base_id="kb_shared",
                agent_id="agent_editor",
                permission="publisher",
                created_by_user_id="user_admin",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_agent_audit_idempotency_key_is_unique_for_one_action() -> None:
    assert hasattr(db_models, "KnowledgeBaseAuditEvent")
    audit_model = db_models.KnowledgeBaseAuditEvent

    with _test_session() as db:
        first = audit_model(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            actor_type="agent",
            actor_id="agent_editor",
            action="draft_created",
            idempotency_key="turn-1-call-1",
        )
        db.add(first)
        db.commit()

        duplicate = audit_model(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            team_id="team_a",
            actor_type="agent",
            actor_id="agent_editor",
            action="draft_created",
            idempotency_key="turn-1-call-1",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
