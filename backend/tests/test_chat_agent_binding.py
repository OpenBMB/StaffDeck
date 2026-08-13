from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import chat as chat_api
from app.agents.branching import agent_private_metadata
from app.api.chat import (
    _bind_request_to_session_agent,
    _ensure_chat_agent_available,
    _user_message_metadata,
    create_chat_session,
    list_slash_commands,
    list_chat_sessions,
)
from app.core.agent_loop import AgentLoop, AgentLoopPreconditionError
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    ExternalSessionBinding,
    GeneralSkill,
    Message,
    ModelConfig,
    PersonaConfig,
    ScheduledTaskRun,
    Skill,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.session.session_schema import ChatSessionCreateRequest, ChatTurnRequest


def test_existing_chat_session_cannot_switch_agent() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(AgentProfile(id="agent_a", tenant_id="tenant_demo", name="客服 A", is_overall=False))
        db.add(AgentProfile(id="agent_b", tenant_id="tenant_demo", name="客服 B", is_overall=False))
        session = ChatSession(
            id="session_bound",
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_a",
        )
        db.add(session)
        db.commit()

        request = ChatTurnRequest(
            tenant_id="tenant_demo",
            session_id=session.id,
            user_id="user_demo",
            agent_id="agent_b",
            message="你好",
        )

        with pytest.raises(HTTPException) as exc_info:
            _bind_request_to_session_agent(db, request, session, current_user)

        assert exc_info.value.status_code == 409
        assert db.get(ChatSession, session.id).agent_id == "agent_a"


def test_chat_agent_must_be_active_non_overall_agent() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体", is_overall=True)
        )
        db.add(
            AgentProfile(
                id="agent_archived",
                tenant_id="tenant_demo",
                name="已归档",
                is_overall=False,
                status="archived",
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as missing:
            _ensure_chat_agent_available(db, "tenant_demo", None, current_user)
        with pytest.raises(HTTPException) as overall:
            _ensure_chat_agent_available(db, "tenant_demo", "agent_overall", current_user)
        with pytest.raises(HTTPException) as archived:
            _ensure_chat_agent_available(db, "tenant_demo", "agent_archived", current_user)

        assert missing.value.status_code == 400
        assert overall.value.status_code == 404
        assert archived.value.status_code == 404


def test_chat_slash_commands_only_list_bound_executable_resources() -> None:
    with _test_session() as db:
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(current_user)
        db.add(
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="客服",
                is_overall=False,
                metadata_json={"owner_user_id": current_user.id},
            )
        )
        sop = Skill(
            tenant_id="tenant_demo",
            skill_id="refund_v1",
            name="退款流程",
            status="published",
            content_json={"start_node_id": "start", "nodes": [{"node_id": "start"}]},
        )
        general_skill = GeneralSkill(
            tenant_id="tenant_demo",
            slug="weather",
            name="天气查询",
            status="published",
            capability_scope="general",
            skill_markdown="# Weather",
        )
        hidden_skill = GeneralSkill(
            tenant_id="tenant_demo",
            slug="sop-only",
            name="仅 SOP 技能",
            status="published",
            capability_scope="sop_specific",
            skill_markdown="# SOP only",
        )
        tool = Tool(
            tenant_id="tenant_demo",
            name="price_query",
            display_name="价格查询",
            method="GET",
            url="https://example.test/prices",
            enabled=True,
            capability_scope="general",
        )
        db.add(sop)
        db.add(general_skill)
        db.add(hidden_skill)
        db.add(tool)
        db.flush()
        for resource_type, resource_id in (
            ("skill", sop.id),
            ("general_skill", general_skill.id),
            ("general_skill", hidden_skill.id),
            ("tool", tool.id),
        ):
            db.add(
                AgentResourceBinding(
                    tenant_id="tenant_demo",
                    agent_id="agent_demo",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    status="active",
                    metadata_json=agent_private_metadata("agent_demo"),
                )
            )
        db.commit()

        rows = list_slash_commands(
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            current_user=current_user,
            db=db,
        )

        assert {(row.kind, row.target) for row in rows} == {
            ("sop", "refund_v1"),
            ("skill", "weather"),
            ("tool", "price_query"),
        }


def test_create_chat_session_always_creates_new_agent_session() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="研发",
                is_overall=False,
                metadata_json={"owner_user_id": "user_demo"},
            )
        )
        db.add(
            ChatSession(
                id="session_existing",
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
            )
        )
        db.commit()

        first = create_chat_session(
            ChatSessionCreateRequest(tenant_id="tenant_demo", agent_id="agent_demo"),
            current_user=current_user,
            db=db,
        )
        second = create_chat_session(
            ChatSessionCreateRequest(tenant_id="tenant_demo", agent_id="agent_demo"),
            current_user=current_user,
            db=db,
        )
        session_rows = db.exec(
            select(ChatSession).where(ChatSession.agent_id == "agent_demo")
        ).all()

        assert first.id != "session_existing"
        assert second.id not in {"session_existing", first.id}
        assert len(session_rows) == 3


def test_chat_session_list_exposes_scheduled_origin_without_title_inference() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add(
            ChatSession(
                id="session_normal",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="定时任务：手动命名",
            )
        )
        db.add(
            ChatSession(
                id="session_scheduled",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="已重命名",
            )
        )
        db.add(
            ScheduledTaskRun(
                id="schedrun_demo",
                tenant_id="tenant_demo",
                scheduled_task_id="sched_demo",
                agent_id="agent_demo",
                user_id="user_demo",
                session_id="session_scheduled",
                scheduled_for=utc_now(),
            )
        )
        db.commit()

        rows = list_chat_sessions("tenant_demo", current_user=current_user, db=db)
        by_id = {row.id: row for row in rows}

        assert by_id["session_normal"].is_scheduled is False
        assert by_id["session_scheduled"].is_scheduled is True


def test_chat_session_list_hides_pilotdeck_sessions_from_all_supported_origins() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        current_user = User(
            id="user_demo", tenant_id="tenant_demo", username="demo", password_hash="x"
        )
        db.add(current_user)
        db.add_all(
            [
                ChatSession(
                    id="session_visible",
                    tenant_id="tenant_demo",
                    user_id=current_user.id,
                    title="普通对话",
                ),
                ChatSession(
                    id="session_pilotdeck_api",
                    tenant_id="tenant_demo",
                    user_id=current_user.id,
                    title="PilotDeck API",
                    channel="public_api",
                ),
                ChatSession(
                    id="session_pilotdeck_legacy",
                    tenant_id="tenant_demo",
                    user_id=current_user.id,
                    title="PilotDeck Legacy",
                    channel="pilotdeck_group_chat",
                ),
                ChatSession(
                    id="session_pilotdeck_event",
                    tenant_id="tenant_demo",
                    user_id=current_user.id,
                    title="PilotDeck Event",
                ),
            ]
        )
        db.add(
            ExternalSessionBinding(
                tenant_id="tenant_demo",
                credential_id="credential_pilotdeck",
                agent_id="agent_demo",
                external_session_id="pilotdeck-room-1",
                session_id="session_pilotdeck_api",
                metadata_json={"channel": "pilotdeck_group_chat"},
            )
        )
        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id="session_pilotdeck_event",
                event_type="user_message_received",
                payload_json={"channel": "pilotdeck_group_chat"},
            )
        )
        db.commit()

        rows = list_chat_sessions("tenant_demo", current_user=current_user, db=db)

        assert [row.id for row in rows] == ["session_visible"]
        assert db.get(ChatSession, "session_pilotdeck_api") is not None
        assert db.get(ChatSession, "session_pilotdeck_legacy") is not None
        assert db.get(ChatSession, "session_pilotdeck_event") is not None


def test_agent_loop_persists_pilotdeck_origin_on_legacy_session() -> None:
    with _test_session() as db:
        loop = AgentLoop(db)
        session = loop._get_or_create_session(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                agent_id="agent_demo",
                message="请处理",
                channel="pilotdeck_group_chat",
            )
        )

        assert session.channel == "pilotdeck_group_chat"


def test_session_title_summary_uses_first_user_message_when_title_empty(monkeypatch) -> None:
    engine = _test_engine()
    monkeypatch.setattr(chat_api, "engine", engine)
    with Session(engine) as db:
        db.add(ChatSession(id="session_title", tenant_id="tenant_demo", user_id="user_demo"))
        db.add(
            Message(
                id="msg_user",
                tenant_id="tenant_demo",
                session_id="session_title",
                role="user",
                content="请查询北京今天的天气。",
            )
        )
        db.commit()

    chat_api._summarize_session_title_once("tenant_demo", "user_demo", "session_title", None)

    with Session(engine) as db:
        row = db.get(ChatSession, "session_title")
        event = db.exec(
            select(AgentEvent).where(
                AgentEvent.session_id == "session_title",
                AgentEvent.event_type == chat_api.SESSION_TITLE_SUMMARY_EVENT,
            )
        ).first()

    assert row is not None
    assert row.title == "请查询北京今天的天气"
    assert event is not None
    assert event.payload_json["title"] == "请查询北京今天的天气"


def test_session_title_summary_does_not_override_existing_title(monkeypatch) -> None:
    engine = _test_engine()
    monkeypatch.setattr(chat_api, "engine", engine)
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_manual_title",
                tenant_id="tenant_demo",
                user_id="user_demo",
                title="手动标题",
            )
        )
        db.add(
            Message(
                id="msg_user_manual",
                tenant_id="tenant_demo",
                session_id="session_manual_title",
                role="user",
                content="请查询北京今天的天气。",
            )
        )
        db.commit()

    chat_api._summarize_session_title_once("tenant_demo", "user_demo", "session_manual_title", None)

    with Session(engine) as db:
        row = db.get(ChatSession, "session_manual_title")
        events = db.exec(
            select(AgentEvent).where(AgentEvent.session_id == "session_manual_title")
        ).all()

    assert row is not None
    assert row.title == "手动标题"
    assert events == []


def test_scheduled_task_chat_turn_marks_user_message_metadata() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="每天18点复盘差评",
        interaction_mode="scheduled_task",
    )

    assert _user_message_metadata(request) == {"interaction_mode": "scheduled_task"}


def test_normal_chat_turn_user_message_metadata_is_empty() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        session_id="session_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        message="每天18点复盘差评",
    )

    assert _user_message_metadata(request) == {}


def test_chat_turn_can_select_enabled_model_config() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            ModelConfig(
                id="model_default",
                tenant_id="tenant_demo",
                name="默认模型",
                api_key_encrypted="",
                model="default-model",
                is_default=True,
            )
        )
        db.add(
            ModelConfig(
                id="model_selected",
                tenant_id="tenant_demo",
                name="选择模型",
                api_key_encrypted="",
                model="selected-model",
            )
        )
        db.commit()
        loop = AgentLoop(db)

        model = loop._get_request_model(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                model_config_id="model_selected",
                message="你好",
            )
        )

        assert model is not None
        assert model.id == "model_selected"


def test_chat_turn_rejects_disabled_selected_model_config() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            ModelConfig(
                id="model_disabled",
                tenant_id="tenant_demo",
                name="停用模型",
                api_key_encrypted="",
                model="disabled-model",
                enabled=False,
            )
        )
        db.commit()
        loop = AgentLoop(db)

        with pytest.raises(AgentLoopPreconditionError) as exc_info:
            loop._get_request_model(
                ChatTurnRequest(
                    tenant_id="tenant_demo",
                    agent_id="agent_demo",
                    model_config_id="model_disabled",
                    message="你好",
                )
            )

        assert exc_info.value.code == "disabled_model_config"


def test_agent_persona_prompt_includes_employee_identity_and_metadata() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_dev",
                tenant_id="tenant_demo",
                name="研发员工",
                description="负责研发资料查询、SOP 执行和交付记录沉淀。",
                is_overall=False,
                metadata_json={
                    "role_name": "研发",
                    "work_styles": ["目标明确", "证据优先"],
                    "expertise_tags": ["代码检索", "SOP 执行"],
                    "work_modes": ["理解需求", "推进执行"],
                    "owner_user_id": "user_demo",
                },
            )
        )
        db.commit()

        prompt = AgentLoop(db)._get_persona_prompt("tenant_demo", "agent_dev")

        assert prompt is not None
        assert "员工名称：研发员工" in prompt
        assert "员工描述：负责研发资料查询、SOP 执行和交付记录沉淀。" in prompt
        assert "岗位：研发" in prompt
        assert "工作风格：目标明确、证据优先" in prompt
        assert "擅长领域：代码检索、SOP 执行" in prompt
        assert "工作方式：理解需求、推进执行" in prompt
        assert "owner_user_id" not in prompt
        assert "user_demo" not in prompt


def test_agent_persona_prompt_keeps_custom_prompt_with_identity() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(PersonaConfig(tenant_id="tenant_demo", system_prompt="全局员工设定"))
        db.add(
            AgentProfile(
                id="agent_finance",
                tenant_id="tenant_demo",
                name="财务员工",
                description="负责报销核对。",
                persona_prompt="只能在有证据时给结论。\n必要时先追问缺失凭证。",
                is_overall=False,
                metadata_json={"role_name": "财务"},
            )
        )
        db.commit()

        prompt = AgentLoop(db)._get_persona_prompt("tenant_demo", "agent_finance")

        assert prompt is not None
        assert "员工名称：财务员工" in prompt
        assert "岗位：财务" in prompt
        assert "员工角色补充要求：" in prompt
        assert "只能在有证据时给结论。\n必要时先追问缺失凭证。" in prompt
        assert "全局员工设定" not in prompt


def _test_session() -> Session:
    return Session(_test_engine())


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine
