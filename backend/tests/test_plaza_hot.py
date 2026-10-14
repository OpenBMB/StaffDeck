"""plaza_hot 热门资源聚合接口的口径回归测试。

参考 test_gallery_counts 的做法：不起 TestClient，直接调 handler，
用 in-memory SQLite + StaticPool 造最小租户数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.plaza_hot import plaza_hot
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    GeneralSkill,
    KnowledgeBase,
    ModelConfig,
    Skill,
    Tenant,
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


def _demo_admin():
    return User(
        id="user_admin",
        tenant_id=TENANT,
        username="user_admin",
        password_hash=hash_password("demo"),
        role="admin",
    )


def _call(db: Session):
    return plaza_hot(tenant_id=TENANT, db=db, current_user=_demo_admin())


def test_hot_empty_tenant_returns_empty_items():
    with _test_session() as db:
        _seed_minimal_tenant(db)
        payload = _call(db)
        assert payload.items == []
        assert payload.generated_at is not None


def test_hot_ranks_by_real_counters_and_skips_unpublished():
    with _test_session() as db:
        _seed_minimal_tenant(db)
        now = datetime(2026, 10, 13, 12, 0, 0)
        older = now - timedelta(days=3)

        # 数字员工：usage_count 就是热度；未发布 / overall / 下线不进榜
        db.add(
            AgentProfile(
                id="agent_a", tenant_id=TENANT, name="报销专员", status="active",
                usage_count=42, metadata_json={"published_to_gallery": True},
            )
        )
        db.add(
            AgentProfile(
                id="agent_b", tenant_id=TENANT, name="新人专员", status="active",
                usage_count=0, metadata_json={"published_to_gallery": True},
            )
        )
        db.add(
            AgentProfile(
                id="agent_off", tenant_id=TENANT, name="下线员工", status="offline",
                usage_count=999, metadata_json={"published_to_gallery": True},
            )
        )

        # 技能：download_count 是热度；draft 不进榜
        gs_hot = GeneralSkill(
            tenant_id=TENANT, slug="ppt-builder", name="PPT 技能",
            skill_markdown="# x", status="published", download_count=7,
        )
        gs_draft = GeneralSkill(
            tenant_id=TENANT, slug="draft", name="草稿技能",
            skill_markdown="# x", status="draft", download_count=100,
        )
        db.add(gs_hot)
        db.add(gs_draft)
        db.add(_gallery_binding("general_skill", gs_hot.id))

        # SOP：published；热度 = 被复制分支数（排除 overall 宿主自己的分支）
        sop = Skill(
            tenant_id=TENANT, skill_id="sop_hot", name="指标分析 SOP",
            content_json={}, status="published",
        )
        db.add(sop)
        db.add(_gallery_binding("skill", sop.id))
        db.add(
            AgentSkillBranch(
                tenant_id=TENANT, agent_id="agent_a", skill_id="sop_hot__branch1",
                source_skill_id=sop.id, content_json={},
            )
        )
        db.add(
            AgentSkillBranch(
                tenant_id=TENANT, agent_id="agent_a", skill_id="sop_hot__branch2",
                source_skill_id=sop.id, content_json={},
            )
        )
        # overall 宿主自己的发布分支不计热度
        db.add(
            AgentSkillBranch(
                tenant_id=TENANT, agent_id="agent_overall", skill_id="sop_hot__pub",
                source_skill_id=sop.id, content_json={},
            )
        )

        # 知识库：active + overall 绑定可见；热度 = 员工侧复制绑定数
        kb = KnowledgeBase(tenant_id=TENANT, name="产品资料库", status="active")
        db.add(kb)
        db.add(_gallery_binding("knowledge_base", kb.id))
        db.add(
            AgentResourceBinding(
                tenant_id=TENANT, agent_id="agent_a",
                resource_type="knowledge_base", resource_id=kb.id,
                status="active", metadata_json={},
            )
        )
        db.commit()

        payload = _call(db)
        by_ref = {item.ref_id: item for item in payload.items}

        assert by_ref["agent_a"].metric_value == 42
        assert by_ref["agent_a"].metric_label == "被使用"
        assert by_ref[gs_hot.id].metric_value == 7
        assert by_ref[sop.id].metric_value == 2  # overall 的发布分支已排除
        assert by_ref[kb.id].metric_value == 1  # overall 的发布绑定已排除
        assert "agent_off" not in by_ref
        assert gs_draft.id not in by_ref
        assert by_ref["agent_b"].metric_value == 0  # 0 分也返回，页面不为空

        # 排序：score 降序，冠军是 usage=42 的员工
        assert payload.items[0].ref_id == "agent_a"
        scores = [item.score for item in payload.items]
        assert scores == sorted(scores, reverse=True)
        assert len(payload.items) <= 10
