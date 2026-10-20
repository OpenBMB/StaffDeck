"""gallery_counts 聚合计数接口的口径回归测试。

参考 test_general_skills 的做法：不起 TestClient，直接调 handler，
用 in-memory SQLite + StaticPool 造最小租户数据。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.gallery import GalleryCountsRead, gallery_counts
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    ModelConfig,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret

TENANT = "tenant_demo"


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_minimal_tenant(db: Session) -> None:
    db.add(Tenant(id=TENANT, name="Demo"))
    db.add(
        User(
            id="user_demo",
            tenant_id=TENANT,
            username="user_demo",
            password_hash=hash_password("demo"),
        )
    )
    db.add(
        ModelConfig(
            tenant_id=TENANT,
            name="Fake model",
            api_key_encrypted=encrypt_secret("test-key"),
            model="fake",
            is_default=True,
            enabled=True,
        )
    )
    db.add(
        AgentProfile(
            id="agent_overall",
            tenant_id=TENANT,
            name="整体智能体",
            is_overall=True,
            status="active",
        )
    )
    db.commit()


def _gallery_binding(resource_type: str, resource_id: str) -> AgentResourceBinding:
    return AgentResourceBinding(
        tenant_id=TENANT,
        agent_id="agent_overall",
        resource_type=resource_type,
        resource_id=resource_id,
        status="active",
        metadata_json={},
    )


def _call(db: Session, user=None):
    return gallery_counts(tenant_id=TENANT, db=db, current_user=user)


def _demo_admin():
    return User(
        id="user_admin",
        tenant_id=TENANT,
        username="user_admin",
        password_hash=hash_password("demo"),
        role="admin",
    )


def test_counts_empty_tenant_returns_zeros():
    with _test_session() as db:
        _seed_minimal_tenant(db)
        counts = _call(db)
        assert counts == GalleryCountsRead()


def test_counts_cover_all_modules_with_frontend_filters():
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_pub",
                tenant_id=TENANT,
                name="已发布员工",
                status="active",
                metadata_json={"published_to_gallery": True},
            )
        )
        db.add(
            AgentProfile(
                id="agent_hidden_meta",
                tenant_id=TENANT,
                name="meta 有发布但 status 下线的员工",
                status="offline",
                metadata_json={"published_to_gallery": True},
            )
        )
        db.add(
            AgentProfile(
                id="agent_unpub",
                tenant_id=TENANT,
                name="未发布员工",
                status="active",
                metadata_json={},
            )
        )

        # skill: published + overall 绑定才计数；draft / 缺绑定 / 状态不符都不计。
        sop_pub = Skill(
            tenant_id=TENANT,
            skill_id="sop_pub",
            name="已发布 SOP",
            content_json={},
            status="published",
        )
        sop_draft = Skill(
            tenant_id=TENANT,
            skill_id="sop_draft",
            name="草稿 SOP",
            content_json={},
            status="draft",
        )
        db.add(sop_pub)
        db.add(sop_draft)
        db.add(_gallery_binding("skill", sop_pub.id))
        db.add(
            AgentResourceBinding(
                tenant_id=TENANT,
                agent_id="agent_overall",
                resource_type="skill",
                resource_id=sop_draft.id,
                status="active",
                metadata_json={},
            )
        )

        # general skill: published 计数；draft 不计。
        gs_pub = GeneralSkill(
            tenant_id=TENANT,
            slug="weather",
            name="天气技能",
            skill_markdown="# weather",
            status="published",
        )
        gs_draft = GeneralSkill(
            tenant_id=TENANT,
            slug="draft-skill",
            name="草稿技能",
            skill_markdown="# draft",
            status="draft",
        )
        db.add(gs_pub)
        db.add(gs_draft)
        db.add(_gallery_binding("general_skill", gs_pub.id))
        db.add(
            AgentResourceBinding(
                tenant_id=TENANT,
                agent_id="agent_overall",
                resource_type="general_skill",
                resource_id=gs_draft.id,
                status="active",
                metadata_json={},
            )
        )

        # knowledge base: active 且非“空默认知识库”才计数。
        kb_full = KnowledgeBase(tenant_id=TENANT, name="资料库", status="active")
        kb_default = KnowledgeBase(tenant_id=TENANT, name="默认知识库", status="active")
        kb_offline = KnowledgeBase(tenant_id=TENANT, name="下线库", status="archived")
        db.add(kb_full)
        db.add(kb_default)
        db.add(kb_offline)
        db.add(_gallery_binding("knowledge_base", kb_full.id))
        db.add(_gallery_binding("knowledge_base", kb_default.id))
        db.add(_gallery_binding("knowledge_base", kb_offline.id))

        # tools: 广场内所有工具定义都计数；全局 enabled 已废弃。
        tool_on = Tool(
            tenant_id=TENANT,
            name="搜天气",
            method="GET",
            url="https://example.com/weather",
            enabled=True,
        )
        tool_off = Tool(
            tenant_id=TENANT,
            name="搜新闻",
            method="GET",
            url="https://example.com/news",
            enabled=False,
        )
        db.add(tool_on)
        db.add(tool_off)
        db.add(_gallery_binding("tool", tool_on.id))
        db.add(
            AgentResourceBinding(
                tenant_id=TENANT,
                agent_id="agent_overall",
                resource_type="tool",
                resource_id=tool_off.id,
                status="active",
                metadata_json={},
            )
        )
        db.commit()

        counts = _call(db, _demo_admin())
        assert counts.agents == 1  # 只统计 published_to_gallery 且 active 的非整体员工
        assert counts.skills == 1
        assert counts.general_skills == 1
        # “资料库”计入；“默认知识库”空库不计；下线库不计。
        assert counts.knowledge == 1
        assert counts.tools == 2
