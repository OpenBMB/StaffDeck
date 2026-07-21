import json
from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_autoroute as autoroute_module
import app.channels.service_intake as intake_module
import app.core.agent_loop as agent_loop_module
from app.channels.service_autoroute import (
    AUTO_ROUTE_CONFIDENCE_THRESHOLD,
    classify_intent,
    maybe_auto_route,
)
from app.channels.service_intake import process_inbound
from app.channels.service_routing import resolve_current_agent
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelConvState,
    ChannelDelivery,
    ChannelInboundEvent,
    ChatSession,
    ModelConfig,
    Tenant,
    User,
    utc_now,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class FakeLLMClient:
    calls: list[dict] = []
    script: object = json.dumps({"agent_id": "stay", "confidence": 0.9, "reason": "意图不明"})
    error: Exception | None = None

    def __init__(self, model_config):
        self.model_config = model_config

    def generate_text(self, system_prompt, user_payload, response_format=None):
        type(self).calls.append(
            {"system": system_prompt, "payload": user_payload, "format": response_format}
        )
        if type(self).error:
            raise type(self).error
        return type(self).script


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    FakeLLMClient.calls = []
    FakeLLMClient.script = json.dumps({"agent_id": "stay", "confidence": 0.9, "reason": "意图不明"})
    FakeLLMClient.error = None
    monkeypatch.setattr(autoroute_module, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(
        autoroute_module,
        "model_for_agent",
        lambda db, tenant_id, agent_id, role: ModelConfig(
            tenant_id=tenant_id, name="fake", api_key_encrypted="x", model="fake-model"
        ),
    )
    yield


CANDIDATES = [
    {"agent_id": "agent_xz", "name": "行政", "description": "会议室预订、差旅、行政流程"},
    {"agent_id": "agent_cw", "name": "财务", "description": "报销、发票、付款"},
]


# ---------- 分类器 ----------


def test_classify_hit_switches() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        FakeLLMClient.script = json.dumps(
            {"agent_id": "agent_cw", "confidence": 0.92, "reason": "报销属于财务"}
        )
        decision = classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "差旅费怎么报销？")
        assert decision.switched is True
        assert decision.agent_id == "agent_cw"
        assert decision.target_agent_id == "agent_cw"
        assert decision.confidence == 0.92
        # 描述注入 prompt 载荷
        payload = FakeLLMClient.calls[0]["payload"]
        assert payload["candidates"][0]["description"] == "会议室预订、差旅、行政流程"
        assert FakeLLMClient.calls[0]["format"] == {"type": "json_object"}


def test_classify_stay_variants() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        decision = classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "会议室还有吗")
        assert decision.switched is False
        assert decision.agent_id == "agent_xz"

        FakeLLMClient.script = json.dumps({"agent_id": "agent_xz", "confidence": 0.99, "reason": "行政领域"})
        decision = classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "帮我订会议室")
        assert decision.switched is False


def test_classify_low_confidence_falls_back() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.5, "reason": "不太确定"})
        decision = classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?")
        assert decision.switched is False
        assert decision.agent_id == "agent_xz"
        assert AUTO_ROUTE_CONFIDENCE_THRESHOLD == 0.75


def test_classify_bad_json_and_llm_error_fall_back() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        FakeLLMClient.script = "这不是 JSON"
        assert classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?").switched is False

        FakeLLMClient.script = json.dumps(["not", "a", "dict"])
        assert classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?").switched is False

        FakeLLMClient.script = json.dumps({"agent_id": "agent_nope", "confidence": 0.99, "reason": "幻觉"})
        assert classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?").switched is False

        FakeLLMClient.error = RuntimeError("LLM 超时")
        assert classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?").switched is False


def test_classify_without_model_config_stays(monkeypatch) -> None:
    engine = _test_engine()
    monkeypatch.setattr(autoroute_module, "model_for_agent", lambda db, tenant_id, agent_id, role: None)
    with Session(engine) as db:
        FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.99, "reason": "x"})
        decision = classify_intent(db, "tenant_demo", CANDIDATES, "agent_xz", "报销?")
        assert decision.switched is False
        assert FakeLLMClient.calls == []


# ---------- 粘性保护 ----------


def _seed_binding(engine, *, auto_route=None, single_mount=False) -> str:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_xz",
                tenant_id="tenant_demo",
                name="行政",
                description="会议室预订、差旅、行政流程",
                metadata_json={},
            )
        )
        db.add(
            AgentProfile(
                id="agent_cw",
                tenant_id="tenant_demo",
                name="财务",
                description="报销、发票、付款",
                metadata_json={},
            )
        )
        config = {"ilink_bot_id": "bot_1@im.bot"}
        if auto_route is not None:
            config["auto_route"] = auto_route
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            config_json=config,
        )
        db.add(binding)
        db.flush()
        db.add(
            ChannelBindingAgent(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                agent_id="agent_xz",
                is_default=True,
                sort_order=0,
            )
        )
        if not single_mount:
            db.add(
                ChannelBindingAgent(
                    tenant_id="tenant_demo",
                    binding_id=binding.id,
                    agent_id="agent_cw",
                    is_default=False,
                    sort_order=1,
                )
            )
        db.commit()
        return binding.id


def _load_binding(engine, binding_id: str) -> ChannelBinding:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        db.expunge(binding)
        return binding


def test_sticky_rules_skip_classification() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                external_conv_id="wechat_p2p_u1",
                current_agent_id="agent_xz",
            )
        )
        db.add(
            ChatSession(
                id="s_sop",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id=binding_id,
                status="handoff",
            )
        )
        db.commit()
        binding = db.get(ChannelBinding, binding_id)
        # handoff 进行中:硬跳过
        assert maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗") is None

        # 手动保护窗内:硬跳过
        session = db.get(ChatSession, "s_sop")
        session.status = "active"
        db.add(session)
        state = db.exec(select(ChannelConvState)).one()
        state.manual_pin_until = utc_now() + timedelta(minutes=5)
        db.add(state)
        db.commit()
        assert maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗") is None

    assert FakeLLMClient.calls == []


def test_sop_active_uses_higher_threshold() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                external_conv_id="wechat_p2p_u1",
                current_agent_id="agent_xz",
            )
        )
        db.add(
            ChatSession(
                id="s_sop",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id=binding_id,
                active_skill_id="meeting_room_sop",
            )
        )
        db.commit()
        binding = db.get(ChannelBinding, binding_id)

        # SOP 进行中:0.85 超过常规阈值但未达 0.9,不切
        FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.85, "reason": "财务问题"})
        decision = maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗")
        assert decision is not None and decision.switched is False
        assert decision.threshold == 0.9

        # 0.92 达高阈值:切换,且 SOP 会话原样保留(冻结可续)
        FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.92, "reason": "明确财务"})
        decision = maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗")
        assert decision is not None and decision.switched is True
        assert decision.threshold == 0.9
        sop_session = db.get(ChatSession, "s_sop")
        assert sop_session.active_skill_id == "meeting_room_sop"
        assert sop_session.agent_id == "agent_xz"


def test_no_sop_uses_normal_threshold() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.8, "reason": "财务问题"})
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        # 无 SOP:0.8 过常规 0.75 阈值,正常切
        decision = maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗")
        assert decision is not None and decision.switched is True
        assert decision.threshold == 0.75


def test_parse_command_current_alias() -> None:
    from app.channels.service_routing import parse_command

    assert parse_command("/目前").kind == "current"
    assert parse_command("/当前").kind == "current"


def test_auto_route_disabled_and_single_mount_skip() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, auto_route=False)
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗") is None

    engine2 = _test_engine()
    binding_id2 = _seed_binding(engine2, single_mount=True)
    with Session(engine2) as db:
        binding = db.get(ChannelBinding, binding_id2)
        assert maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销吗") is None

    assert FakeLLMClient.calls == []


def test_auto_route_hit_updates_pointer() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.9, "reason": "财务问题"})
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        decision = maybe_auto_route(db, binding, "agent_xz", "wechat_p2p_u1", "报销怎么走")
        assert decision is not None and decision.switched is True
        current, _ = resolve_current_agent(db, binding, "wechat_p2p_u1")
        assert current == "agent_cw"


# ---------- intake 集成 ----------


def _p2p_message(event_id: str, text: str) -> dict:
    return {
        "message_id": event_id,
        "from_user_id": "user_ab12cd34@im.wechat",
        "to_user_id": "bot_1@im.bot",
        "client_id": f"wx-{event_id}",
        "session_id": "user_ab12cd34@im.wechat#bot_1@im.bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": f"ctx_{event_id}",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


class RecordingAgentLoop:
    calls: list = []

    def __init__(self, db):
        self.db = db

    def handle_turn(self, request):
        type(self).calls.append(request)
        self.db.commit()


@pytest.fixture(autouse=True)
def _fake_agent_loop(monkeypatch):
    RecordingAgentLoop.calls = []
    monkeypatch.setattr(agent_loop_module, "AgentLoop", RecordingAgentLoop)
    monkeypatch.setattr(intake_module, "_send_wechat_typing", lambda *args, **kwargs: None)
    yield


def test_intake_auto_route_switches_with_notice_and_event() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.9, "reason": "财务问题"})

    assert process_inbound(binding, _p2p_message("evt_ar1", "报销怎么走"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_cw"

    with Session(engine) as db:
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()
        assert any("已为你转接「财务」" in n.text for n in notices)
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(events) == 1
        payload = events[0].payload_json
        assert payload["switched"] is True
        assert payload["agent_id"] == "agent_cw"
        assert payload["current_agent_id"] == "agent_xz"
        assert payload["confidence"] == 0.9
        assert payload["threshold"] == 0.75
        # 会话锚定在财务名下
        chat_session = db.exec(select(ChatSession)).one()
        assert chat_session.agent_id == "agent_cw"


def test_intake_sop_event_carries_effective_threshold() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    with Session(engine) as db:
        db.add(
            ChannelConvState(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
                current_agent_id="agent_xz",
            )
        )
        db.add(
            ChatSession(
                id="s_sop_intake",
                tenant_id="tenant_demo",
                agent_id="agent_xz",
                channel="wechat",
                external_conv_id="wechat_p2p_user_ab12cd34@im.wechat",
                channel_binding_id=binding_id,
                active_skill_id="meeting_room_sop",
            )
        )
        db.commit()
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.85, "reason": "财务倾向"})

    # SOP 进行中:0.85 未达 0.9 不切,但分类已执行,事件带生效阈值 0.9
    assert process_inbound(binding, _p2p_message("evt_sop1", "报销怎么走"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    with Session(engine) as db:
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(events) == 1
        payload = events[0].payload_json
        assert payload["switched"] is False
        assert payload["threshold"] == 0.9


def test_intake_auto_route_stay_no_notice() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_ar2", "会议室还有吗"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    with Session(engine) as db:
        notices = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "notice")
        ).all()
        assert notices == []
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["switched"] is False


def test_intake_new_conversation_initial_dispatch() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.95, "reason": "首条即财务"})

    # 全新会话(无指针无历史):首条消息即可分发到非默认员工
    assert process_inbound(binding, _p2p_message("evt_ar3", "我要报销"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_cw"
    payload = FakeLLMClient.calls[0]["payload"]
    assert payload["recent_messages"] == []


def test_manual_switch_writes_pin_window_and_blocks_auto_route() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_xz", "confidence": 0.99, "reason": "行政问题"})

    # 手动切换到财务:写 10 分钟保护窗
    assert process_inbound(binding, _p2p_message("evt_ar4", "/切换 财务"), db_engine=engine) is False
    with Session(engine) as db:
        state = db.exec(select(ChannelConvState)).one()
        assert state.current_agent_id == "agent_cw"
        assert state.manual_pin_until is not None
        assert state.manual_pin_until > utc_now() + timedelta(minutes=9)

    # 保护窗内:即使分类器想切回行政也不触发(LLM 不被调用)
    assert process_inbound(binding, _p2p_message("evt_ar5", "帮我订会议室"), db_engine=engine) is True
    assert FakeLLMClient.calls == []
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_cw"

    # 窗外:恢复"每条都判"
    with Session(engine) as db:
        state = db.exec(select(ChannelConvState)).one()
        state.manual_pin_until = utc_now() - timedelta(minutes=1)
        db.add(state)
        db.commit()
    assert process_inbound(binding, _p2p_message("evt_ar6", "帮我订会议室"), db_engine=engine) is True
    assert len(FakeLLMClient.calls) == 1
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"


def test_intake_auto_route_disabled_uses_default() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine, auto_route=False)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = json.dumps({"agent_id": "agent_cw", "confidence": 0.99, "reason": "财务"})

    assert process_inbound(binding, _p2p_message("evt_ar7", "我要报销"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    assert FakeLLMClient.calls == []
    with Session(engine) as db:
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert events == []


def test_intake_classification_failure_still_runs_turn() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.error = RuntimeError("LLM 超时")

    # 分类异常回退当前员工,消息正常进入 AgentLoop
    assert process_inbound(binding, _p2p_message("evt_ar8", "报销怎么走"), db_engine=engine) is True
    assert RecordingAgentLoop.calls[-1].agent_id == "agent_xz"
    with Session(engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.status == "done"
        # 失败原因落决策事件 error 字段
        route_events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(route_events) == 1
        payload = route_events[0].payload_json
        assert payload["switched"] is False
        assert "LLM 超时" in payload["error"]


def test_intake_bad_json_error_in_decision_event() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)
    FakeLLMClient.script = "这不是 JSON"

    assert process_inbound(binding, _p2p_message("evt_ar9", "报销怎么走"), db_engine=engine) is True
    with Session(engine) as db:
        route_events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(route_events) == 1
        payload = route_events[0].payload_json
        assert payload["switched"] is False
        # 坏 JSON 的解析失败摘要可辨
        assert "Expecting value" in payload["error"]


def test_normal_decision_event_error_empty() -> None:
    engine = _test_engine()
    binding_id = _seed_binding(engine)
    binding = _load_binding(engine, binding_id)

    assert process_inbound(binding, _p2p_message("evt_ar10", "会议室还有吗"), db_engine=engine) is True
    with Session(engine) as db:
        route_events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "auto_route_decision")
        ).all()
        assert len(route_events) == 1
        assert route_events[0].payload_json["error"] == ""


# ---------- PUT auto_route 读写 ----------


def _make_api_client(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.channels as channels_api
    from app.db import get_session

    app = FastAPI()
    app.include_router(channels_api.router)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app)


def _seed_api_users(engine) -> dict[str, User]:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        owner = User(id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x")
        db.add(owner)
        db.add(
            AgentProfile(
                id="agent_xz",
                tenant_id="tenant_demo",
                name="行政",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
        return {"owner": owner}


def _auth(user: User) -> dict[str, str]:
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user)}"}


def test_put_auto_route_read_write() -> None:
    engine = _test_engine()
    users = _seed_api_users(engine)
    with Session(engine) as db:
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_xz",
            channel="wechat",
            status="active",
            created_by_user_id="user_owner",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id

    client = _make_api_client(engine)
    # 默认开
    listed = client.get(
        "/api/enterprise/channels?tenant_id=tenant_demo&agent_id=agent_xz",
        headers=_auth(users["owner"]),
    )
    assert listed.json()[0]["auto_route"] is True

    # 显式关闭
    updated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_xz"}], "auto_route": False},
        headers=_auth(users["owner"]),
    )
    assert updated.status_code == 200
    assert updated.json()["auto_route"] is False
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        assert binding.config_json["auto_route"] is False

    # 不传 auto_route:保持不动
    updated = client.put(
        f"/api/enterprise/channels/{binding_id}?tenant_id=tenant_demo",
        json={"agents": [{"agent_id": "agent_xz"}]},
        headers=_auth(users["owner"]),
    )
    assert updated.json()["auto_route"] is False
