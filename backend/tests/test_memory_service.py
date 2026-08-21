from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.memories import clear_my_memories, list_memories
from app.db.models import AgentProfile, ChatSession, MemoryRecord, Message, ModelConfig, Tenant, User
from app.llm.client import LLMClient
from app.memory.jobs import _conversation_messages_for_turn
from app.memory.service import MemoryService, memory_rows_for_read
from app.session.session_schema import ChatTurnRequest, StepAgentResult


def test_memory_capture_uses_model_updates_and_deduplicates_profile_name(monkeypatch) -> None:
    captured_payload = {}

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        captured_payload.update(payload)
        return {
            "memories": [
                {
                    "operation": "upsert",
                    "kind": "profile",
                    "key": "preferred_name",
                        "content": "xyq",
                    "importance": 0.95,
                    "reason": "用户更新了称呼。",
                }
            ],
            "updated_summary": "用户当前称呼为 xyq，正在测试客服购买和售后流程。",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    with _test_session() as db:
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="user_demo",
                username="user_demo",
                session_id="old_session",
                kind="profile",
                    content="hm",
                    importance=0.95,
                    metadata_json={"source": "profile_extractor", "key": "preferred_name"},
            )
        )
        db.commit()

        saved = MemoryService(db).capture_turn(
            ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="我叫xyq"),
            ChatSession(id="session_test", tenant_id="tenant_demo", user_id="user_demo"),
            StepAgentResult(),
            None,
            ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
            [
                {"role": "user", "content": "我叫xyq"},
                {"role": "assistant", "content": "好的，已记住您的称呼。"},
            ],
        )
        db.commit()

        rows = list(db.exec(select(MemoryRecord).where(MemoryRecord.user_id == "user_demo")).all())

    profile_rows = [row for row in rows if row.kind == "profile"]
    summary_rows = [row for row in rows if row.kind == "summary"]
    assert len(profile_rows) == 1
    assert profile_rows[0].content == "xyq"
    assert profile_rows[0].metadata_json["key"] == "preferred_name"
    assert summary_rows == []
    assert saved
    assert captured_payload["existing_memories"] == "- profile/preferred_name: hm"
    assert captured_payload["conversation_context"]["messages"] == [
        {"role": "user", "content": "我叫xyq"},
        {"role": "assistant", "content": "好的，已记住您的称呼。"},
    ]
    assert "user_message" not in captured_payload
    assert "assistant_reply" not in captured_payload
    assert "recent_messages" not in captured_payload


def test_memory_capture_ignores_summary_updates(monkeypatch) -> None:
    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        assert "用户长期摘要" in payload["existing_memories"]
        return {
            "memories": [],
            "updated_summary": "用户希望客服回复简洁，并正在验证多轮下单流程。",
        }

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    with _test_session() as db:
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="user_demo",
                username="user_demo",
                session_id="old_session",
                kind="summary",
                content="用户长期摘要\n- 用户本轮诉求：我要买东西；最近处理结果：请问数量",
                importance=0.8,
                metadata_json={"turn_count": 3},
            )
        )
        db.commit()

        MemoryService(db).capture_turn(
            ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="一个"),
            ChatSession(id="session_test", tenant_id="tenant_demo", user_id="user_demo"),
            StepAgentResult(),
            None,
            ModelConfig(tenant_id="tenant_demo", name="demo", api_key_encrypted="", model="demo"),
            [
                {"role": "user", "content": "一个"},
                {"role": "assistant", "content": "已为您创建订单。"},
            ],
        )
        db.commit()

        rows = list(db.exec(select(MemoryRecord).where(MemoryRecord.kind == "summary")).all())

    assert len(rows) == 1
    assert rows[0].content == "用户长期摘要\n- 用户本轮诉求：我要买东西；最近处理结果：请问数量"
    assert rows[0].metadata_json["turn_count"] == 3


def test_memory_job_reads_canonical_history_through_its_own_turn() -> None:
    with _test_session() as db:
        db.add_all(
            [
                Message(
                    id="msg_user_1",
                    tenant_id="tenant_demo",
                    session_id="session_test",
                    role="user",
                    content="我32岁",
                ),
                Message(
                    id="msg_assistant_1",
                    tenant_id="tenant_demo",
                    session_id="session_test",
                    role="assistant",
                    content="已记录",
                    metadata_json={"turn_id": "msg_user_1"},
                ),
                Message(
                    id="msg_user_2",
                    tenant_id="tenant_demo",
                    session_id="session_test",
                    role="user",
                    content="这是更晚一轮",
                ),
                Message(
                    id="msg_assistant_2",
                    tenant_id="tenant_demo",
                    session_id="session_test",
                    role="assistant",
                    content="更晚一轮回复",
                    metadata_json={"turn_id": "msg_user_2"},
                ),
            ]
        )
        db.commit()

        messages = _conversation_messages_for_turn(
            db, "tenant_demo", "session_test", "msg_user_1"
        )

    assert messages == [
        {"role": "user", "content": "我32岁"},
        {"role": "assistant", "content": "已记录"},
    ]


def test_memory_recall_excludes_summary_history() -> None:
    with _test_session() as db:
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="user_demo",
                username="user_demo",
                session_id="old_session",
                kind="summary",
                content="用户正在测试客服购买和售后流程。",
                importance=0.9,
            )
        )
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="user_demo",
                username="user_demo",
                session_id="old_session",
                kind="preference",
                content="用户偏好客服回复简洁。",
                importance=0.85,
                metadata_json={"key": "communication_style"},
            )
        )
        db.commit()

        rows = MemoryService(db).recall("tenant_demo", "user_demo", "客服回复")

    assert [row.kind for row in rows] == ["preference"]
    assert rows[0].content == "用户偏好客服回复简洁。"


def test_memory_recall_uses_lexical_candidates_and_bounds_context() -> None:
    with _test_session() as db:
        db.add(
            MemoryRecord(
                id="memory_profile_priority",
                tenant_id="tenant_demo",
                user_id="user_demo",
                kind="profile",
                content="用户姓名是张三",
                importance=0.95,
            )
        )
        db.add(
            MemoryRecord(
                id="memory_chinese_hit",
                tenant_id="tenant_demo",
                user_id="user_demo",
                kind="fact",
                content="用户正在处理报销流程",
                importance=0.4,
            )
        )
        db.add(
            MemoryRecord(
                id="memory_english_hit",
                tenant_id="tenant_demo",
                user_id="user_demo",
                kind="preference",
                content="User prefers VPN setup instructions in English",
                importance=0.4,
            )
        )
        for index in range(20):
            db.add(
                MemoryRecord(
                    id=f"memory_bound_{index}",
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    kind="fact",
                    content=f"报销条目 {index} " + ("详细内容 " * 80),
                    importance=0.3,
                )
            )
        db.commit()

        rows = MemoryService(db).recall("tenant_demo", "user_demo", "报销流程 VPN")

    row_ids = {row.id for row in rows}
    assert {"memory_profile_priority", "memory_chinese_hit", "memory_english_hit"} <= row_ids
    assert len(rows) <= 12
    assert sum(len(row.content) for row in rows) <= 6000


def test_context_memories_returns_all_supported_memories_without_model_selection() -> None:
    with _test_session() as db:
        for index, kind in enumerate(["profile", "preference", "fact"]):
            db.add(
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    username="user_demo",
                    session_id="old_session",
                    kind=kind,
                    content=f"memory-{index}",
                    importance=0.5,
                )
            )
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="user_demo",
                username="user_demo",
                session_id="old_session",
                kind="summary",
                content="不进入 Router memory",
                importance=0.9,
            )
        )
        db.commit()

        rows = MemoryService(db).context_memories("tenant_demo", "user_demo")

    assert {row.content for row in rows} == {"memory-0", "memory-1", "memory-2"}


def test_memory_rows_for_read_deduplicates_by_structured_key_without_text_filtering() -> None:
    rows = [
        MemoryRecord(
            tenant_id="tenant_demo",
            user_id="user_demo",
            kind="profile",
            content="hm",
            metadata_json={"source": "profile_extractor", "key": "preferred_name"},
        ),
        MemoryRecord(
            tenant_id="tenant_demo",
            user_id="user_demo",
            kind="profile",
            content="another value",
            metadata_json={"source": "profile_extractor", "key": "preferred_name"},
        ),
        MemoryRecord(
            tenant_id="tenant_demo",
            user_id="user_demo",
            kind="summary",
            content="用户长期摘要\n- 用户本轮诉求：我要买东西；最近处理结果：请问数量",
            metadata_json={"turn_count": 3},
        ),
    ]

    visible = memory_rows_for_read(rows)

    assert [row.content for row in visible] == [
        "hm",
        "用户长期摘要\n- 用户本轮诉求：我要买东西；最近处理结果：请问数量",
    ]


def test_clear_my_memories_scopes_to_current_user_and_agent() -> None:
    with _test_session() as db:
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="user_demo",
            password_hash="hash",
        )
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(user)
        db.add(ChatSession(id="session_agent_a", tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_a"))
        db.add_all(
            [
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    username="user_demo",
                    session_id="session_direct",
                    kind="profile",
                    content="当前用户 agent_a 直接记忆",
                    metadata_json={"agent_id": "agent_a"},
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    username="user_demo",
                    session_id="session_agent_a",
                    kind="preference",
                    content="当前用户 agent_a 会话推断记忆",
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    username="user_demo",
                    session_id="session_other_agent",
                    kind="fact",
                    content="当前用户其他员工记忆",
                    metadata_json={"agent_id": "agent_b"},
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="other_user",
                    username="other_user",
                    session_id="session_agent_a",
                    kind="profile",
                    content="其他用户同员工记忆",
                    metadata_json={"agent_id": "agent_a"},
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="user_demo",
                    username="user_demo",
                    session_id="session_agent_a",
                    kind="conversation",
                    content="原始对话记录不清理",
                    metadata_json={"agent_id": "agent_a"},
                ),
            ]
        )
        db.commit()

        result = clear_my_memories("tenant_demo", "agent_a", user, db)
        remaining = list(db.exec(select(MemoryRecord).order_by(MemoryRecord.content)).all())

    assert result == {"deleted": 2}
    assert [row.content for row in remaining] == [
        "其他用户同员工记忆",
        "原始对话记录不清理",
        "当前用户其他员工记忆",
    ]


def test_list_memories_for_gallery_agent_only_returns_current_user_for_non_creator() -> None:
    with _test_session() as db:
        owner = User(id="owner_user", tenant_id="tenant_demo", username="owner", password_hash="hash")
        viewer = User(id="viewer_user", tenant_id="tenant_demo", username="viewer", password_hash="hash")
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(owner)
        db.add(viewer)
        db.add(
            AgentProfile(
                id="agent_gallery",
                tenant_id="tenant_demo",
                name="广场员工",
                status="active",
                metadata_json={
                    "owner_user_id": owner.id,
                    "owner_username": owner.username,
                    "published_to_gallery": True,
                },
            )
        )
        db.add_all(
            [
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id=viewer.id,
                    username=viewer.username,
                    kind="profile",
                    content="当前访问者自己的记忆",
                    metadata_json={"agent_id": "agent_gallery"},
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="other_user",
                    username="other",
                    kind="profile",
                    content="其他用户隐私记忆",
                    metadata_json={"agent_id": "agent_gallery"},
                ),
            ]
        )
        db.commit()

        result = list_memories(
            tenant_id="tenant_demo",
            agent_id="agent_gallery",
            user_id=None,
            username=None,
            q=None,
            limit=100,
            current_user=viewer,
            db=db,
        )

    assert [row["content"] for row in result] == ["当前访问者自己的记忆"]


def test_list_memories_non_creator_cannot_filter_into_other_user_memories() -> None:
    with _test_session() as db:
        owner = User(id="owner_user", tenant_id="tenant_demo", username="owner", password_hash="hash")
        viewer = User(id="viewer_user", tenant_id="tenant_demo", username="viewer", password_hash="hash")
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(owner)
        db.add(viewer)
        db.add(
            AgentProfile(
                id="agent_gallery",
                tenant_id="tenant_demo",
                name="广场员工",
                status="active",
                metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
            )
        )
        db.add(
            MemoryRecord(
                tenant_id="tenant_demo",
                user_id="other_user",
                username="other",
                kind="profile",
                content="其他用户隐私记忆",
                metadata_json={"agent_id": "agent_gallery"},
            )
        )
        db.commit()

        result = list_memories(
            tenant_id="tenant_demo",
            agent_id="agent_gallery",
            user_id="other_user",
            username=None,
            q=None,
            limit=100,
            current_user=viewer,
            db=db,
        )

    assert result == []


def test_list_memories_agent_creator_can_view_all_users_for_owned_agent() -> None:
    with _test_session() as db:
        owner = User(id="owner_user", tenant_id="tenant_demo", username="owner", password_hash="hash")
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_owned",
                tenant_id="tenant_demo",
                name="创建者员工",
                status="active",
                metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
            )
        )
        db.add_all(
            [
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id=owner.id,
                    username=owner.username,
                    kind="profile",
                    content="创建者自己的记忆",
                    metadata_json={"agent_id": "agent_owned"},
                ),
                MemoryRecord(
                    tenant_id="tenant_demo",
                    user_id="other_user",
                    username="other",
                    kind="profile",
                    content="其他用户对该员工的记忆",
                    metadata_json={"agent_id": "agent_owned"},
                ),
            ]
        )
        db.commit()

        result = list_memories(
            tenant_id="tenant_demo",
            agent_id="agent_owned",
            user_id=None,
            username=None,
            q=None,
            limit=100,
            current_user=owner,
            db=db,
        )

    assert sorted(row["content"] for row in result) == [
        "其他用户对该员工的记忆",
        "创建者自己的记忆",
    ]


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
