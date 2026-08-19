from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import ChannelInbound
from app.channels.adapters.feishu import FeishuAdapter, FeishuTokenProvider
from app.channels.crypto import encrypt_channel_secret
from app.channels.feishu_runtime import _normalize_event
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    HumanHandoffRequest,
    Tenant,
    User,
    utc_now,
)


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_tenant(db: Session) -> tuple[Tenant, User, User]:
    tenant = Tenant(id="tenant_demo", name="Demo")
    admin = User(
        id="admin_user",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="x",
    )
    assignee = User(
        id="assignee_user",
        tenant_id="tenant_demo",
        username="assignee",
        display_name="指派人",
        password_hash="x",
    )
    db.add(tenant)
    db.add(admin)
    db.add(assignee)
    db.commit()
    return tenant, admin, assignee


def _feishu_binding(
    *,
    binding_id: str = "binding_feishu",
    config: dict | None = None,
    app_id: str = "cli_app",
) -> ChannelBinding:
    return ChannelBinding(
        id=binding_id,
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        channel="feishu",
        status="active",
        config_json=config or {"app_id": app_id},
        credentials_enc=encrypt_channel_secret("secret-value"),
        external_account_key=f"feishu:app:7:{app_id}",
        provider_tenant_key="tenant_key",
        config_revision=1,
    )


def _channel_identity(
    *,
    staffdeck_user_id: str = "assignee_user",
    external_user_id: str = "ou_assignee",
    scope: str = "",
    channel: str = "feishu",
) -> ChannelIdentity:
    return ChannelIdentity(
        tenant_id="tenant_demo",
        channel=channel,
        external_account_scope=scope,
        external_user_id=external_user_id,
        staffdeck_user_id=staffdeck_user_id,
    )


def _pending_handoff(
    *,
    handoff_id: str = "handoff_demo",
    session_id: str = "session_demo",
    assignee_user_id: str = "assignee_user",
    notify_message_id: str = "",
    metadata: dict | None = None,
) -> HumanHandoffRequest:
    return HumanHandoffRequest(
        id=handoff_id,
        tenant_id="tenant_demo",
        session_id=session_id,
        agent_id="agent_demo",
        assignee_user_id=assignee_user_id,
        pending_question="网络故障",
        context_summary="user: 网络断了",
        status="pending",
        notify_message_id=notify_message_id,
        metadata_json=metadata or {},
    )


def _inbound(
    *,
    event_id: str = "om_inbound_1",
    from_user_id: str = "ou_assignee",
    text: str = "已处理",
    parent_id: str = "",
) -> ChannelInbound:
    return ChannelInbound(
        channel="feishu",
        event_id=event_id,
        from_user_id=from_user_id,
        to_user_id="ou_bot",
        session_id=from_user_id,
        group_id="",
        context_token=event_id,
        text=text,
        is_group=False,
        raw={},
        parent_id=parent_id,
    )


def _inbound_event(
    *,
    event_id: str = "om_inbound_1",
    binding_id: str = "binding_feishu",
) -> object:
    from app.db.models import ChannelInboundEvent

    return ChannelInboundEvent(
        id=f"chevt_{event_id}",
        tenant_id="tenant_demo",
        binding_id=binding_id,
        channel="feishu",
        event_id=event_id,
        payload_json={},
        status="processing",
        target_json={},
    )


# ---------------------------------------------------------------------------
# assignee 优先级链:SOP 节点 → 渠道默认 → owner → admin
# ---------------------------------------------------------------------------


def test_assignee_prefers_step_assignee_user_id() -> None:
    """SOP 节点指定 assignee_user_id 时,优先用它,忽略渠道默认/owner/admin。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        session = ChatSession(
            id="session_sop",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="active",
        )
        db.add(session)
        db.commit()

        service = HumanHandoffService(db, FakeEvents())
        handoff = service.create(
            "tenant_demo",
            session,
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工", "assignee_user_id": "assignee_user"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
            step_assignee_user_id="assignee_user",
            binding_default_assignee_user_id="admin_user",
        )
        assert handoff.assignee_user_id == "assignee_user"


def test_assignee_falls_back_to_binding_default() -> None:
    """SOP 节点未指定时,用渠道默认处理人。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        session = ChatSession(
            id="session_binding",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="active",
        )
        db.add(session)
        db.commit()

        service = HumanHandoffService(db, FakeEvents())
        handoff = service.create(
            "tenant_demo",
            session,
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
            step_assignee_user_id=None,
            binding_default_assignee_user_id="assignee_user",
        )
        assert handoff.assignee_user_id == "assignee_user"


def test_assignee_skips_invalid_configured_users() -> None:
    """失效、跨租户或渠道客户配置不能阻断 owner/admin 降级。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(Tenant(id="tenant_other", name="Other"))
        db.add(
            User(
                id="other_tenant_user",
                tenant_id="tenant_other",
                username="other",
                password_hash="x",
            )
        )
        db.add(
            User(
                id="channel_customer",
                tenant_id="tenant_demo",
                username="feishu_customer",
                source="feishu",
                password_hash="x",
            )
        )
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        session = ChatSession(
            id="session_invalid_assignee",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="active",
        )
        db.add(session)
        db.commit()

        service = HumanHandoffService(db, FakeEvents())
        for step_assignee, binding_assignee in (
            ("deleted_user", "other_tenant_user"),
            ("channel_customer", None),
        ):
            handoff = service.create(
                "tenant_demo",
                session,
                StepAgentResult(),
                current_step_resolver=lambda: {"name": "转人工"},
                assignee_resolver=lambda *_: "admin_user",
                context_summary=lambda _: "",
                pending_question=lambda *_: "问题",
                step_assignee_user_id=step_assignee,
                binding_default_assignee_user_id=binding_assignee,
            )
            assert handoff.assignee_user_id == "admin_user"


def test_assignee_falls_back_to_owner_then_admin() -> None:
    """SOP 与渠道默认都未指定时,走 assignee_resolver(owner → admin)。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        session = ChatSession(
            id="session_owner",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="active",
        )
        db.add(session)
        db.commit()

        service = HumanHandoffService(db, FakeEvents())
        handoff = service.create(
            "tenant_demo",
            session,
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
            step_assignee_user_id=None,
            binding_default_assignee_user_id=None,
        )
        assert handoff.assignee_user_id == "admin_user"


def test_handoff_metadata_no_longer_contains_contact_target() -> None:
    """确认 metadata_json 不再写入 contact_target 字段。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        session = ChatSession(
            id="session_meta",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="active",
        )
        db.add(session)
        db.commit()

        service = HumanHandoffService(db, FakeEvents())
        handoff = service.create(
            "tenant_demo",
            session,
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
        )
        assert "contact_target" not in (handoff.metadata_json or {})


# ---------------------------------------------------------------------------
# okf:Contact 概念类型已移除
# ---------------------------------------------------------------------------


def test_contact_removed_from_concept_types() -> None:
    from app.knowledge.okf import CONCEPT_TYPES

    assert "Contact" not in CONCEPT_TYPES


def test_okf_has_no_extract_contact_target() -> None:
    import app.knowledge.okf as okf_mod

    assert not hasattr(okf_mod, "extract_contact_target")
    assert not hasattr(okf_mod, "CONTACT_FRONTMATTER_KEYS")


# ---------------------------------------------------------------------------
# 飞书 open_id 解析:ChannelIdentity 主链路 + scope 隔离
# ---------------------------------------------------------------------------


def test_resolve_open_id_uses_channel_identity_with_binding_scope() -> None:
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        # 同一 assignee 在 scope_a 下有 open_id
        db.add(_channel_identity(external_user_id="ou_scope_a", scope="scope_a"))
        # 同一 assignee 在 scope_b 下有另一个 open_id
        db.add(_channel_identity(external_user_id="ou_scope_b", scope="scope_b"))
        db.commit()

        # binding 的 scope 是 scope_a,应取 ou_scope_a
        from app.channels.service_identity import external_account_scope

        original = external_account_scope

        def fake_scope(_db, _binding):
            return "scope_a"

        import app.channels.service_outbox as outbox_mod

        outbox_mod.external_account_scope = fake_scope
        try:
            open_id = _resolve_assignee_feishu_open_id(db, binding, "assignee_user")
            assert open_id == "ou_scope_a"
        finally:
            outbox_mod.external_account_scope = original


def test_resolve_open_id_returns_none_when_no_identity() -> None:
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()
        # 无 ChannelIdentity
        assert _resolve_assignee_feishu_open_id(db, binding, "assignee_user") is None


def test_resolve_open_id_isolates_across_bindings() -> None:
    """两个不同 binding(scope 不同),同一 assignee 各自查到不同 open_id。"""
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding_a = _feishu_binding(binding_id="binding_a", app_id="cli_a")
        binding_b = _feishu_binding(binding_id="binding_b", app_id="cli_b")
        db.add(binding_a)
        db.add(binding_b)
        db.add(_channel_identity(external_user_id="ou_a", scope="scope_a"))
        db.add(_channel_identity(external_user_id="ou_b", scope="scope_b"))
        db.commit()

        import app.channels.service_outbox as outbox_mod

        original = outbox_mod.external_account_scope
        scopes = {"binding_a": "scope_a", "binding_b": "scope_b"}

        def fake_scope(_db, b):
            return scopes.get(b.id, "")

        outbox_mod.external_account_scope = fake_scope
        try:
            assert _resolve_assignee_feishu_open_id(db, binding_a, "assignee_user") == "ou_a"
            assert _resolve_assignee_feishu_open_id(db, binding_b, "assignee_user") == "ou_b"
        finally:
            outbox_mod.external_account_scope = original


# ---------------------------------------------------------------------------
# handoff_notice 投递登记 + notify_message_id 回写
# ---------------------------------------------------------------------------


def test_notify_handoff_assignee_stages_handoff_notice_delivery() -> None:
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity(external_user_id="ou_assignee"))
        db.add(_pending_handoff())
        db.commit()

        notify_handoff_assignee(db, binding, _pending_handoff(), "网络故障", "user: 网络断了")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.target_json["receive_id_type"] == "open_id"
        assert delivery.target_json["receive_id"] == "ou_assignee"
        assert delivery.target_json["handoff_id"] == "handoff_demo"
        assert "指派人" in delivery.text  # User.display_name
        assert "网络故障" in delivery.text
        assert delivery.status == "pending"


def test_notify_handoff_assignee_deduplicates_existing_notice() -> None:
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        handoff = _pending_handoff()
        db.add(binding)
        db.add(_channel_identity(external_user_id="ou_assignee"))
        db.add(handoff)
        db.commit()

        notify_handoff_assignee(db, binding, handoff, "网络故障", "")
        notify_handoff_assignee(db, binding, handoff, "网络故障", "")

        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert len(deliveries) == 1


def test_notify_handoff_assignee_skips_when_no_open_id() -> None:
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_pending_handoff())
        db.commit()

        # 无 ChannelIdentity → 跳过,不登记 delivery
        notify_handoff_assignee(db, binding, _pending_handoff(), "问题", "")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert deliveries == []


def test_write_handoff_notify_message_id_persists_message_id() -> None:
    from app.channels.service_outbox import _write_handoff_notify_message_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(_pending_handoff(handoff_id="handoff_write"))
        db.commit()
        delivery = ChannelDelivery(
            tenant_id="tenant_demo",
            binding_id="binding_feishu",
            session_id="handoff:handoff_write",
            kind="handoff_notice",
            text="通知",
            target_json={"handoff_id": "handoff_write"},
            status="delivered",
            idempotency_key="k1",
        )
        db.add(delivery)
        db.commit()

        _write_handoff_notify_message_id(db, delivery, "om_notify_123")
        refreshed = db.get(HumanHandoffRequest, "handoff_write")
        assert refreshed is not None
        assert refreshed.notify_message_id == "om_notify_123"


# ---------------------------------------------------------------------------
# FeishuAdapter.send 透传 message_id
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        return self.handler(url, kwargs)

    def get(self, url, **kwargs):
        return self.handler(url, {**kwargs, "_method": "GET"})

    def patch(self, url, **kwargs):
        return self.handler(url, {**kwargs, "_method": "PATCH"})


def _httpx_response(status: int, payload: dict, url: str):
    import httpx

    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


def test_feishu_send_returns_message_id_for_p2p_message() -> None:
    def handler(url, kwargs):
        if "/auth/" in url:
            return _httpx_response(
                200, {"code": 0, "tenant_access_token": "token-a", "expire": 7200}, url
            )
        return _httpx_response(
            200, {"code": 0, "msg": "success", "data": {"message_id": "om_sent_001"}}, url
        )

    def factory():
        return _FakeClient(handler)

    adapter = FeishuAdapter(
        token_provider=FeishuTokenProvider(client_factory=factory),
        client_factory=factory,
    )
    target = {"receive_id_type": "open_id", "receive_id": "ou_target"}
    message_id = adapter.send(_feishu_binding(), target, "通知内容", idempotency_key="dk1")
    assert message_id == "om_sent_001"


def test_feishu_send_returns_none_when_response_lacks_message_id() -> None:
    def handler(url, kwargs):
        if "/auth/" in url:
            return _httpx_response(
                200, {"code": 0, "tenant_access_token": "token-a", "expire": 7200}, url
            )
        return _httpx_response(200, {"code": 0, "msg": "success", "data": {}}, url)

    def factory():
        return _FakeClient(handler)

    adapter = FeishuAdapter(
        token_provider=FeishuTokenProvider(client_factory=factory),
        client_factory=factory,
    )
    target = {"receive_id_type": "open_id", "receive_id": "ou_target"}
    assert adapter.send(_feishu_binding(), target, "x", idempotency_key="dk2") is None


# ---------------------------------------------------------------------------
# 飞书归一化捕获 parent_id
# ---------------------------------------------------------------------------


def _build_feishu_event(
    *,
    message_id: str = "om_inbound_1",
    parent_id: str = "",
    root_id: str = "",
    chat_type: str = "p2p",
    text: str = "已处理",
    open_id: str = "ou_assignee",
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=message_id,
        chat_id="oc_chat1" if chat_type != "p2p" else "",
        chat_type=chat_type,
        message_type="text",
        content=f'{{"text":"{text}"}}',
        thread_id="",
        parent_id=parent_id,
        root_id=root_id,
        mentions=[],
    )
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id=open_id),
    )
    body = SimpleNamespace(message=message, sender=sender)
    header = SimpleNamespace(app_id="cli_app", tenant_key="tenant_key")
    return SimpleNamespace(header=header, event=body)


def test_normalize_event_captures_parent_id_for_reply() -> None:
    event = _build_feishu_event(parent_id="om_notify_999")
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.parent_id == "om_notify_999"


def test_normalize_event_falls_back_to_root_id_when_parent_id_absent() -> None:
    event = _build_feishu_event(root_id="om_root_999")
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.parent_id == "om_root_999"


def test_normalize_event_leaves_parent_id_empty_for_non_reply() -> None:
    event = _build_feishu_event()
    result = _normalize_event(event, bot_open_id="ou_bot")
    assert result is not None
    inbound, _target = result
    assert inbound.parent_id == ""


def test_channel_inbound_has_parent_id_field() -> None:
    inbound = ChannelInbound(
        channel="feishu",
        event_id="e1",
        from_user_id="ou_x",
        to_user_id="ou_bot",
        session_id="s1",
        group_id="",
        context_token="e1",
        text="hi",
        is_group=False,
        raw={},
        parent_id="om_parent",
    )
    assert inbound.parent_id == "om_parent"
    default = ChannelInbound(
        channel="feishu",
        event_id="e2",
        from_user_id="ou_x",
        to_user_id="ou_bot",
        session_id="s2",
        group_id="",
        context_token="e2",
        text="hi",
        is_group=False,
        raw={},
    )
    assert default.parent_id == ""


# ---------------------------------------------------------------------------
# 飞书直接回复 → handoff 关联(严格校验发送者 == 通知目标)
# ---------------------------------------------------------------------------


def _seed_handoff_reply_scenario(
    db: Session,
    *,
    notify_message_id: str = "om_notify_1",
    notice_receive_id: str = "ou_assignee",
    handoff_assignee: str = "assignee_user",
    sender_open_id: str = "ou_assignee",
    sender_staffdeck_user_id: str = "assignee_user",
) -> tuple[ChannelBinding, HumanHandoffRequest, ChannelInbound, object]:
    binding = _feishu_binding()
    db.add(binding)
    db.add(_channel_identity(
        external_user_id=sender_open_id,
        staffdeck_user_id=sender_staffdeck_user_id,
    ))
    session = ChatSession(
        id="session_demo",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        status="handoff",
    )
    handoff = _pending_handoff(
        notify_message_id=notify_message_id,
        assignee_user_id=handoff_assignee,
    )
    db.add(session)
    db.add(handoff)
    # 模拟 handoff_notice 已投递成功,有对应 ChannelDelivery
    db.add(ChannelDelivery(
        tenant_id="tenant_demo",
        binding_id=binding.id,
        session_id=f"handoff:{handoff.id}",
        message_id=notify_message_id,
        kind="handoff_notice",
        text="通知",
        target_json={
            "receive_id_type": "open_id",
            "receive_id": notice_receive_id,
            "handoff_id": handoff.id,
        },
        status="delivered",
        idempotency_key="notice_k",
    ))
    db.commit()
    inbound = _inbound(
        event_id="om_reply_1",
        from_user_id=sender_open_id,
        text="已修复网络",
        parent_id=notify_message_id,
    )
    event = _inbound_event(event_id="om_reply_1", binding_id=binding.id)
    db.add(event)
    db.commit()
    return binding, handoff, inbound, event


def test_try_handle_feishu_handoff_reply_matches_and_answers_handoff(monkeypatch) -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding, handoff, inbound, event = _seed_handoff_reply_scenario(db)

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            resumed: list[str] = []

            def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
                row.status = "answered"
                row.human_reply = reply
                row.answered_at = utc_now()
                db_arg.add(row)
                db_arg.commit()
                resumed.append((row.id, source))

            import app.api.chat as chat_api

            monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

            handled = _try_handle_feishu_handoff_reply(
                db, binding, inbound, event,
                {"receive_id_type": "open_id", "receive_id": "ou_assignee"},
            )
            assert handled is True
            assert resumed == [(handoff.id, "feishu")]
            refreshed_event = db.get(type(event), event.id)
            assert refreshed_event.status == "done"
            ack = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).first()
            assert ack is not None
            assert "已收到你的回复" in ack.text
        finally:
            intake_mod.external_account_scope = original


def test_try_handle_feishu_handoff_reply_rejects_non_assignee_sender(monkeypatch) -> None:
    """发送者 open_id != 通知目标 receive_id 时拒绝(严格校验)。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        # 通知发给 ou_assignee,但回复发送者是 ou_stranger
        binding, handoff, inbound, event = _seed_handoff_reply_scenario(
            db,
            notice_receive_id="ou_assignee",
            sender_open_id="ou_stranger",
            sender_staffdeck_user_id="stranger_user",
        )

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            handled = _try_handle_feishu_handoff_reply(db, binding, inbound, event, {})
            assert handled is False
            assert db.get(HumanHandoffRequest, handoff.id).status == "pending"
        finally:
            intake_mod.external_account_scope = original


def test_try_handle_feishu_handoff_reply_returns_false_without_parent_id() -> None:
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()
        inbound = _inbound(parent_id="")
        event = _inbound_event(binding_id=binding.id)
        db.add(event)
        db.commit()
        assert _try_handle_feishu_handoff_reply(db, binding, inbound, event, {}) is False


def test_try_handle_feishu_handoff_reply_rejects_when_no_notice_delivery() -> None:
    """handoff 有 notify_message_id 但无对应 ChannelDelivery(通知未投递)时拒绝。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity())
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(notify_message_id="om_notify_orphan")
        db.add(session)
        db.add(handoff)
        # 不创建 ChannelDelivery(通知未投递)
        db.commit()
        inbound = _inbound(parent_id="om_notify_orphan")
        event = _inbound_event(binding_id=binding.id)
        db.add(event)
        db.commit()

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            assert _try_handle_feishu_handoff_reply(db, binding, inbound, event, {}) is False
        finally:
            intake_mod.external_account_scope = original


# ---------------------------------------------------------------------------
# /回复反馈 指令解析与处理
# ---------------------------------------------------------------------------


def test_parse_command_recognizes_handoff_reply_chinese() -> None:
    from app.channels.service_routing import parse_command

    cmd = parse_command("/回复反馈 已修复网络故障")
    assert cmd is not None
    assert cmd.kind == "handoff_reply"
    assert cmd.query == "已修复网络故障"


def test_parse_command_recognizes_handoff_reply_english() -> None:
    from app.channels.service_routing import parse_command

    cmd = parse_command("/handoff_reply fixed the router")
    assert cmd is not None
    assert cmd.kind == "handoff_reply"
    assert cmd.query == "fixed the router"


def test_parse_command_handoff_reply_empty_query() -> None:
    from app.channels.service_routing import parse_command

    cmd = parse_command("/回复反馈")
    assert cmd is not None
    assert cmd.kind == "handoff_reply"
    assert cmd.query == ""


def test_parse_command_handoff_reply_with_leading_spaces() -> None:
    from app.channels.service_routing import parse_command

    cmd = parse_command("  /回复反馈   重启了服务器  ")
    assert cmd is not None
    assert cmd.kind == "handoff_reply"
    assert cmd.query == "重启了服务器"


def test_run_handoff_reply_command_matches_by_identity(monkeypatch) -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity())
        session = ChatSession(
            id="session_hr1",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(handoff_id="handoff_hr1", session_id="session_hr1")
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = _inbound(event_id="om_hr_1", text="/回复反馈 已修复网络")
        command = ChannelCommand(kind="handoff_reply", query="已修复网络")

        resumed: list[tuple[str, str]] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append((row.id, source))

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == [("handoff_hr1", "feishu")]
            assert result is intake_mod._HANDOFF_REPLY_HANDLED
            assert db.get(HumanHandoffRequest, "handoff_hr1").status == "answered"
            ack = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).first()
            assert ack is not None
            assert "已收到你的回复" in ack.text
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_command_rejects_without_identity() -> None:
    """发送者无 ChannelIdentity(未绑定 StaffDeck 身份)时拒绝,不再用 contact_target 模糊匹配。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        # 不创建 ChannelIdentity
        session = ChatSession(
            id="session_hr2",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(
            handoff_id="handoff_hr2",
            session_id="session_hr2",
            assignee_user_id="admin_user",
        )
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = _inbound(event_id="om_hr_2", from_user_id="ou_admin", text="/回复反馈 已修复")
        command = ChannelCommand(kind="handoff_reply", query="已修复")

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert "未找到" in result or "未绑定" in result
            assert db.get(HumanHandoffRequest, "handoff_hr2").status == "pending"
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_command_no_pending_handoff_returns_error() -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity())
        db.commit()

        inbound = _inbound(event_id="om_hr_3", text="/回复反馈 已修复")
        command = ChannelCommand(kind="handoff_reply", query="已修复")

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert "未找到" in result
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_command_empty_query_returns_usage() -> None:
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()

        inbound = _inbound(event_id="om_hr_4", text="/回复反馈")
        command = ChannelCommand(kind="handoff_reply", query="")

        result = _run_handoff_reply_command(db, binding, inbound, command)
        assert "用法" in result


def test_run_handoff_reply_command_rejects_multiple_pending(monkeypatch) -> None:
    """多个 pending handoff 且未引用通知时,拒绝模糊处理。"""
    from datetime import timedelta

    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity())
        db.add(ChatSession(
            id="session_old",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        db.add(ChatSession(
            id="session_new",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        old_time = utc_now()
        new_time = old_time + timedelta(seconds=10)
        db.add(_pending_handoff(
            handoff_id="handoff_old",
            session_id="session_old",
        ).__class__(  # 重建以设 created_at
            id="handoff_old",
            tenant_id="tenant_demo",
            session_id="session_old",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="旧问题",
            status="pending",
            created_at=old_time,
        ))
        db.add(HumanHandoffRequest(
            id="handoff_new",
            tenant_id="tenant_demo",
            session_id="session_new",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="新问题",
            status="pending",
            created_at=new_time,
        ))
        db.commit()

        inbound = _inbound(event_id="om_hr_5", text="/回复反馈 解决了")
        command = ChannelCommand(kind="handoff_reply", query="解决了")

        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
            row.status = "answered"
            row.human_reply = reply
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == []
            assert "多个待处理" in result
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_command_rejects_unknown_parent_id() -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        handoff = _pending_handoff(notify_message_id="om_notice_expected")
        db.add(binding)
        db.add(_channel_identity())
        db.add(handoff)
        db.commit()

        inbound = _inbound(event_id="om_hr_unknown", text="/回复反馈 修好了")
        inbound.parent_id = "om_unrelated_message"
        command = ChannelCommand(kind="handoff_reply", query="修好了")

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert "未找到" in result
            assert db.get(HumanHandoffRequest, handoff.id).status == "pending"
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_command_matches_by_parent_id(monkeypatch) -> None:
    """引用通知(parent_id)时,按 notify_message_id 精确匹配 handoff。"""
    from datetime import timedelta

    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity())
        db.add(ChatSession(
            id="session_p1",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        db.add(ChatSession(
            id="session_p2",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        old_time = utc_now()
        new_time = old_time + timedelta(seconds=10)
        db.add(HumanHandoffRequest(
            id="handoff_p1",
            tenant_id="tenant_demo",
            session_id="session_p1",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="旧问题",
            status="pending",
            notify_message_id="om_notice_1",
            created_at=old_time,
        ))
        db.add(HumanHandoffRequest(
            id="handoff_p2",
            tenant_id="tenant_demo",
            session_id="session_p2",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="新问题",
            status="pending",
            notify_message_id="om_notice_2",
            created_at=new_time,
        ))
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                session_id="handoff:handoff_p1",
                message_id="om_notice_1",
                kind="handoff_notice",
                text="旧通知",
                target_json={
                    "receive_id_type": "open_id",
                    "receive_id": "ou_assignee",
                    "handoff_id": "handoff_p1",
                },
                status="delivered",
                idempotency_key="notice_p1",
            )
        )
        db.commit()

        # 话题内回复的 parent_id 指向中间消息,应通过 root_id 命中原通知。
        inbound = _inbound(event_id="om_hr_6", text="/回复反馈 修好了")
        inbound.parent_id = "om_reply_child"
        inbound.raw = {"message": {"root_id": "om_notice_1"}}
        command = ChannelCommand(kind="handoff_reply", query="修好了")

        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
            row.status = "answered"
            row.human_reply = reply
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == ["handoff_p1"]
            assert result is intake_mod._HANDOFF_REPLY_HANDLED
        finally:
            intake_mod.external_account_scope = original


# ---------------------------------------------------------------------------
# _apply_handoff_reply 的 source 参数
# ---------------------------------------------------------------------------


def test_apply_handoff_reply_records_source_feishu() -> None:
    from app.api.chat import _apply_handoff_reply
    from app.db.models import AgentEvent

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(ChatSession(
            id="session_src",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        db.add(_pending_handoff(handoff_id="handoff_src", session_id="session_src"))
        db.commit()

        _apply_handoff_reply(
            db,
            db.get(HumanHandoffRequest, "handoff_src"),
            "已处理",
            answered_by_user_id="assignee_user",
            source="feishu",
        )
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "human_handoff_answered")
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["source"] == "feishu"


def test_apply_handoff_reply_records_source_web_by_default() -> None:
    from app.api.chat import _apply_handoff_reply
    from app.db.models import AgentEvent

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(ChatSession(
            id="session_src2",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        ))
        db.add(_pending_handoff(handoff_id="handoff_src2", session_id="session_src2"))
        db.commit()

        _apply_handoff_reply(
            db,
            db.get(HumanHandoffRequest, "handoff_src2"),
            "已处理",
            answered_by_user_id="admin_user",
        )
        events = db.exec(
            select(AgentEvent).where(AgentEvent.event_type == "human_handoff_answered")
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["source"] == "web"
