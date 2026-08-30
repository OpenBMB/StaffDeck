"""验证团队 Agent 对共享知识草稿、发布与回滚的显式工具边界。"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_agent_private_knowledge_branch
from app.capabilities.contracts import CapabilityContext
from app.core.capability_manifest import CapabilityManifestBuilder
from app.core.harness_capability_invoker import HarnessCapabilityInvoker
from app.db.models import (
    AgentProfile,
    ChatSession,
    KnowledgeBase,
    KnowledgeBaseAuditEvent,
    KnowledgeBaseVersion,
    KnowledgeIngestJob,
    ModelConfig,
    Team,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
    TeamMember,
    Tenant,
)
from app.knowledge.errors import KnowledgeError


@dataclass(frozen=True)
class _AgentToolFixture:
    """Agent 工具矩阵中的可信团队、知识库和角色标识。"""

    team_id: str
    knowledge_base_id: str
    published_version_id: str
    session_id: str
    reader_id: str
    editor_id: str
    publisher_id: str


def _runtime_type():
    """延迟读取 Agent 运行时，让缺失实现形成明确的 TDD 红灯。"""
    module = importlib.import_module("app.capabilities.local_knowledge")
    runtime = getattr(module, "SharedKnowledgeAgentRuntime", None)
    assert runtime is not None, "missing SharedKnowledgeAgentRuntime"
    return runtime


def _session() -> Session:
    """创建隔离内存数据库，副作用仅限单个 Agent 工具测试。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_agent_tool_matrix(db: Session) -> _AgentToolFixture:
    """建立一个默认共享库及 reader/editor/publisher 三种实时授权。"""
    team_id = "team_agent_tools"
    knowledge_base_id = "kb_agent_tools"
    published_version_id = "kbver_agent_tools_1"
    session_id = "session_agent_tools"
    roles = {
        "agent_reader": "reader",
        "agent_editor": "editor",
        "agent_publisher": "publisher",
    }
    db.add(Tenant(id="tenant_demo", name="Demo"))
    team = Team(
        id=team_id,
        tenant_id="tenant_demo",
        name="Agent 维护团队",
        owner_user_id="user_admin",
        default_knowledge_base_id=knowledge_base_id,
    )
    base = KnowledgeBase(
        id=knowledge_base_id,
        tenant_id="tenant_demo",
        name="Agent 维护知识库",
        mode="shared",
        published_version_id=published_version_id,
    )
    published = KnowledgeBaseVersion(
        id=published_version_id,
        tenant_id="tenant_demo",
        knowledge_base_id=knowledge_base_id,
        version="1.0.0",
        name=base.name,
        publication_state="released",
    )
    db.add(team)
    db.add(base)
    db.add(published)
    db.add(
        ChatSession(
            id=session_id,
            tenant_id="tenant_demo",
            user_id="user_admin",
            team_id=team_id,
            channel="web",
            status="active",
        )
    )
    db.add(
        TeamKnowledgeBaseBinding(
            tenant_id="tenant_demo",
            team_id=team_id,
            knowledge_base_id=knowledge_base_id,
            created_by_user_id="user_admin",
        )
    )
    for agent_id, permission in roles.items():
        db.add(AgentProfile(id=agent_id, tenant_id="tenant_demo", name=agent_id))
        db.add(TeamMember(team_id=team_id, agent_id=agent_id))
        db.add(
            TeamKnowledgeBaseGrant(
                tenant_id="tenant_demo",
                team_id=team_id,
                knowledge_base_id=knowledge_base_id,
                agent_id=agent_id,
                permission=permission,
                created_by_user_id="user_admin",
            )
        )
    db.commit()
    return _AgentToolFixture(
        team_id=team_id,
        knowledge_base_id=knowledge_base_id,
        published_version_id=published_version_id,
        session_id=session_id,
        reader_id="agent_reader",
        editor_id="agent_editor",
        publisher_id="agent_publisher",
    )


def _context(
    fixture: _AgentToolFixture,
    agent_id: str,
    *,
    turn_id: str,
) -> CapabilityContext:
    """用服务端团队会话标识构造一次可信 Agent 工具上下文。"""
    return CapabilityContext(
        request_id=f"request-{turn_id}",
        tenant_id="tenant_demo",
        agent_id=agent_id,
        user_id="user_admin",
        session_id=fixture.session_id,
        turn_id=turn_id,
        channel="web",
        team_id=fixture.team_id,
    )


def _add_dedicated_knowledge_for_agent(
    db: Session,
    *,
    agent_id: str,
) -> tuple[KnowledgeBase, KnowledgeBaseVersion]:
    """为指定员工创建专用知识及其分支版本；副作用仅写入当前测试会话。"""
    dedicated_base = KnowledgeBase(
        id=f"kb_dedicated_{agent_id}",
        tenant_id="tenant_demo",
        name=f"{agent_id} 专用知识",
        mode="dedicated",
    )
    db.add(dedicated_base)
    db.flush()
    ensure_agent_private_knowledge_branch(
        db,
        "tenant_demo",
        agent_id,
        dedicated_base,
    )
    db.commit()
    dedicated_version = db.exec(
        select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.knowledge_base_id == dedicated_base.id
        )
    ).one()
    return dedicated_base, dedicated_version


def test_shared_version_listing_rejects_a_dedicated_team_read_projection() -> None:
    """群聊可读专用库不等于共享版本维护动作可以把它列为目标。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        dedicated_base, _dedicated_version = _add_dedicated_knowledge_for_agent(
            db,
            agent_id=fixture.publisher_id,
        )
        runtime = runtime_model(db)

        with pytest.raises(KnowledgeError) as denied:
            runtime.execute(
                _context(fixture, fixture.publisher_id, turn_id="turn-list-dedicated"),
                "knowledge_list_versions",
                {"knowledge_base_id": dedicated_base.id},
            )

    assert denied.value.code == "KNOWLEDGE_GRANT_REQUIRED"


def test_shared_draft_update_rejects_a_dedicated_team_read_projection() -> None:
    """绕过能力发现直调共享草稿动作时，专用版本仍在授权层被拒绝。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        _dedicated_base, dedicated_version = _add_dedicated_knowledge_for_agent(
            db,
            agent_id=fixture.publisher_id,
        )
        runtime = runtime_model(db)

        with pytest.raises(KnowledgeError) as denied:
            runtime.execute(
                _context(fixture, fixture.publisher_id, turn_id="turn-update-dedicated"),
                "knowledge_update_draft",
                {
                    "draft_version_id": dedicated_version.id,
                    "title": "不应写入",
                    "filename": "denied.md",
                    "content": "专用知识只加入群聊读取上下文。",
                    "idempotency_key": "dedicated-update-1",
                },
            )

    assert denied.value.code == "KNOWLEDGE_GRANT_REQUIRED"


def test_editor_uses_team_default_and_idempotently_records_draft_provenance() -> None:
    """省略目标时只用团队默认库，并让同键重试返回同一草稿与审计事件。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        runtime = runtime_model(db)
        context = _context(fixture, fixture.editor_id, turn_id="turn-create")
        arguments = {
            "change_reason": "补充本周选题复盘",
            "source_references": [{"type": "message", "id": "msg-1"}],
            "idempotency_key": "create-draft-1",
        }

        first = runtime.execute(context, "knowledge_create_draft", arguments)
        db.commit()
        replay = runtime.execute(context, "knowledge_create_draft", arguments)
        with pytest.raises(KnowledgeError) as changed_input:
            runtime.execute(
                context,
                "knowledge_create_draft",
                {**arguments, "change_reason": "复用同键写入另一项变更"},
            )
        drafts = list(
            db.exec(
                select(KnowledgeBaseVersion).where(
                    KnowledgeBaseVersion.knowledge_base_id
                    == fixture.knowledge_base_id,
                    KnowledgeBaseVersion.publication_state == "draft",
                )
            ).all()
        )
        events = list(
            db.exec(
                select(KnowledgeBaseAuditEvent).where(
                    KnowledgeBaseAuditEvent.action == "draft_created"
                )
            ).all()
        )
        base = db.get(KnowledgeBase, fixture.knowledge_base_id)

    assert first.data == replay.data
    assert first.replayed is False
    assert replay.replayed is True
    assert first.data["knowledge_base_id"] == fixture.knowledge_base_id
    assert first.data["parent_published_version_id"] == fixture.published_version_id
    assert len(drafts) == 1
    assert drafts[0].source_team_id == fixture.team_id
    assert drafts[0].created_by_agent_id == fixture.editor_id
    assert drafts[0].metadata_json["provenance"] == {
        "source_task_id": "turn-create",
        "source_conversation_id": fixture.session_id,
        "source_references": [{"type": "message", "id": "msg-1"}],
    }
    assert len(events) == 1
    assert events[0].id == first.data["audit_event_id"]
    assert base is not None
    assert base.published_version_id == fixture.published_version_id
    assert changed_input.value.code == "KNOWLEDGE_IDEMPOTENCY_CONFLICT"


def test_editor_update_creates_one_draft_ingest_job_for_retries() -> None:
    """显式更新草稿只创建一个带来源的摄取任务，重试不重复写入。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        runtime = runtime_model(db)
        context = _context(fixture, fixture.editor_id, turn_id="turn-update")
        draft = runtime.execute(
            context,
            "knowledge_create_draft",
            {
                "change_reason": "创建编辑草稿",
                "idempotency_key": "create-for-update",
            },
        )
        db.commit()
        arguments = {
            "draft_version_id": draft.data["draft_version_id"],
            "title": "选题复盘",
            "filename": "topic-review.md",
            "content": "# 选题复盘\n\n本周验证了三个高意向主题。",
            "source_references": [{"type": "task", "id": "research-1"}],
            "idempotency_key": "update-draft-1",
        }

        first = runtime.execute(context, "knowledge_update_draft", arguments)
        db.commit()
        replay = runtime.execute(context, "knowledge_update_draft", arguments)
        jobs = list(db.exec(select(KnowledgeIngestJob)).all())
        events = list(
            db.exec(
                select(KnowledgeBaseAuditEvent).where(
                    KnowledgeBaseAuditEvent.action == "draft_updated"
                )
            ).all()
        )

    assert first.data == replay.data
    assert replay.replayed is True
    assert first.ingest_job_id == first.data["ingest_job_id"]
    assert len(jobs) == 1
    assert jobs[0].knowledge_base_version_id == draft.data["draft_version_id"]
    assert jobs[0].status == "queued"
    assert jobs[0].metadata_json["metadata"]["source_references"] == [
        {"type": "task", "id": "research-1"}
    ]
    assert len(events) == 1


def test_publisher_obeys_readiness_then_publishes_rejects_and_rolls_back() -> None:
    """发布受摄取就绪与 CAS 约束，驳回和回滚均保留版本历史。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        runtime = runtime_model(db)
        editor_context = _context(fixture, fixture.editor_id, turn_id="turn-editor")
        publisher_context = _context(
            fixture,
            fixture.publisher_id,
            turn_id="turn-publisher",
        )
        draft = runtime.execute(
            editor_context,
            "knowledge_create_draft",
            {
                "change_reason": "准备正式更新",
                "idempotency_key": "create-publishable",
            },
        )
        blocking_job = KnowledgeIngestJob(
            tenant_id="tenant_demo",
            knowledge_base_id=fixture.knowledge_base_id,
            knowledge_base_version_id=draft.data["draft_version_id"],
            filename="pending.md",
            status="queued",
            stage="queued",
        )
        db.add(blocking_job)
        db.commit()
        publish_arguments = {
            "draft_version_id": draft.data["draft_version_id"],
            "expected_published_version_id": fixture.published_version_id,
            "change_reason": "审核通过",
            "idempotency_key": "publish-draft-1",
        }

        with pytest.raises(KnowledgeError) as not_ready:
            runtime.execute(
                publisher_context,
                "knowledge_publish_draft",
                publish_arguments,
            )
        blocking_job.status = "succeeded"
        db.add(blocking_job)
        db.commit()
        published = runtime.execute(
            publisher_context,
            "knowledge_publish_draft",
            publish_arguments,
        )
        db.commit()
        replay = runtime.execute(
            publisher_context,
            "knowledge_publish_draft",
            publish_arguments,
        )
        rolled_back = runtime.execute(
            publisher_context,
            "knowledge_rollback",
            {
                "target_version_id": fixture.published_version_id,
                "expected_published_version_id": draft.data["draft_version_id"],
                "change_reason": "回退验证",
                "idempotency_key": "rollback-1",
            },
        )
        rejected_draft = runtime.execute(
            publisher_context,
            "knowledge_create_draft",
            {
                "change_reason": "准备驳回验证",
                "idempotency_key": "create-rejected",
            },
        )
        rejected = runtime.execute(
            publisher_context,
            "knowledge_reject_draft",
            {
                "draft_version_id": rejected_draft.data["draft_version_id"],
                "change_reason": "来源不完整",
                "idempotency_key": "reject-1",
            },
        )
        db.commit()
        base = db.get(KnowledgeBase, fixture.knowledge_base_id)
        version_rows = list(
            db.exec(
                select(KnowledgeBaseVersion).where(
                    KnowledgeBaseVersion.knowledge_base_id
                    == fixture.knowledge_base_id
                )
            ).all()
        )

    assert not_ready.value.code == "KNOWLEDGE_VERSION_NOT_READY"
    assert published.data == replay.data
    assert replay.replayed is True
    assert published.data["published_version_id"] == draft.data["draft_version_id"]
    assert rolled_back.data["target_version_id"] == fixture.published_version_id
    assert rejected.data["publication_state"] == "rejected"
    assert base is not None
    assert base.published_version_id == fixture.published_version_id
    assert {row.publication_state for row in version_rows} == {
        "released",
        "rejected",
    }


def test_frozen_harness_action_is_revoked_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    """清单冻结后撤销团队授权时，Harness 在执行动作前立即拒绝。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        session = db.get(ChatSession, fixture.session_id)
        assert session is not None
        manifest = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            fixture.publisher_id,
            None,
            None,
            team_id=fixture.team_id,
        )
        grant = db.exec(
            select(TeamKnowledgeBaseGrant).where(
                TeamKnowledgeBaseGrant.team_id == fixture.team_id,
                TeamKnowledgeBaseGrant.agent_id == fixture.publisher_id,
            )
        ).one()
        grant.status = "revoked"
        db.add(grant)
        db.commit()
        invoker = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant_demo",
            session=session,
            task_frame_id="turn-revoked",
            model_config=ModelConfig(
                id="model-test",
                tenant_id="tenant_demo",
                name="测试模型",
                api_key_encrypted="test",
                model="test-model",
            ),
            manifest=manifest,
            active_skill=None,
            active_step_id=None,
            agent_id=fixture.publisher_id,
        )

        result = invoker.invoke(
            "knowledge_rollback",
            {
                "target_version_id": fixture.published_version_id,
                "expected_published_version_id": fixture.published_version_id,
                "change_reason": "撤权后不应执行",
                "idempotency_key": "revoked-1",
            },
        )

    assert result["success"] is False
    assert result["error"]["code"] == "CAPABILITY_AUTHORIZATION_REVOKED"


def test_harness_dispatches_an_authorized_shared_knowledge_action(
    tmp_path,
    monkeypatch,
) -> None:
    """Harness 将知识动作交给可信运行时，而不是误当成只读检索。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        session = db.get(ChatSession, fixture.session_id)
        assert session is not None
        manifest = CapabilityManifestBuilder(db).build(
            "tenant_demo",
            fixture.editor_id,
            None,
            None,
            team_id=fixture.team_id,
        )
        invoker = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant_demo",
            session=session,
            task_frame_id="turn-harness-create",
            model_config=ModelConfig(
                id="model-harness-create",
                tenant_id="tenant_demo",
                name="测试模型",
                api_key_encrypted="test",
                model="test-model",
            ),
            manifest=manifest,
            active_skill=None,
            active_step_id=None,
            agent_id=fixture.editor_id,
        )

        result = invoker.invoke(
            "knowledge_create_draft",
            {
                "change_reason": "由团队流程创建草稿",
                "idempotency_key": "harness-create-1",
            },
        )
        draft = db.get(
            KnowledgeBaseVersion,
            result.get("data", {}).get("draft_version_id"),
        )

    assert result["success"] is True
    assert draft is not None
    assert draft.source_team_id == fixture.team_id
    assert draft.created_by_agent_id == fixture.editor_id


def test_reader_cannot_create_a_shared_draft_even_by_calling_runtime_directly() -> None:
    """绕过能力发现直接调用运行时时，实时 reader 授权仍不能写入。"""
    runtime_model = _runtime_type()
    with _session() as db:
        fixture = _seed_agent_tool_matrix(db)
        runtime = runtime_model(db)

        with pytest.raises(KnowledgeError) as denied:
            runtime.execute(
                _context(fixture, fixture.reader_id, turn_id="turn-reader"),
                "knowledge_create_draft",
                {
                    "change_reason": "越权写入",
                    "idempotency_key": "reader-write-1",
                },
            )

    assert denied.value.code == "KNOWLEDGE_GRANT_REQUIRED"
