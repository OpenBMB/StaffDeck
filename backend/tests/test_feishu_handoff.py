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


def test_assignee_notify_channel_follows_selected_assignee() -> None:
    """投递渠道随命中的处理人配置走,并写入 handoff metadata 供通知网关判断。"""
    from app.core.human_handoff_service import HumanHandoffService
    from app.session.session_schema import StepAgentResult

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT"))
        db.commit()

        service = HumanHandoffService(db, FakeEvents())

        def _make_session(session_id: str) -> ChatSession:
            row = ChatSession(
                id=session_id,
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                status="active",
            )
            db.add(row)
            db.commit()
            return row

        # 节点指定网页端:渠道偏好应为 "web"
        web_handoff = service.create(
            "tenant_demo",
            _make_session("session_notify_web"),
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
            step_assignee_user_id="assignee_user",
            step_notify_channel="web",
            binding_default_assignee_user_id="admin_user",
            binding_default_notify_channel="feishu",
        )
        assert web_handoff.assignee_user_id == "assignee_user"
        assert web_handoff.metadata_json["assignee_notify_channel"] == "web"

        # 节点失效时回退渠道默认:渠道偏好取渠道默认的配置
        binding_handoff = service.create(
            "tenant_demo",
            _make_session("session_notify_binding"),
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
            step_assignee_user_id="deleted_user",
            step_notify_channel="feishu",
            binding_default_assignee_user_id="assignee_user",
            binding_default_notify_channel="feishu",
        )
        assert binding_handoff.assignee_user_id == "assignee_user"
        assert binding_handoff.metadata_json["assignee_notify_channel"] == "feishu"

        # 全部未配置时无渠道偏好(默认投递)
        fallback_handoff = service.create(
            "tenant_demo",
            _make_session("session_notify_fallback"),
            StepAgentResult(),
            current_step_resolver=lambda: {"name": "转人工"},
            assignee_resolver=lambda *_: "admin_user",
            context_summary=lambda _: "",
            pending_question=lambda *_: "问题",
        )
        assert fallback_handoff.assignee_user_id == "admin_user"
        assert fallback_handoff.metadata_json["assignee_notify_channel"] is None


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
        # 通知优先引导直接引用回复,不再强制 /回复反馈 前缀
        assert "直接回复本条消息" in delivery.text
        assert "/回复反馈" in delivery.text  # 前缀指令仍作为备选保留
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


def test_delivery_daemon_persists_message_id_for_notice_and_ack() -> None:
    """handoff_notice/handoff_ack 投递成功后回写 message_id,供引用回复关联。"""
    from app.channels.adapters.base import get_channel_adapter, register_channel_adapter
    from app.channels.service_outbox import run_delivery_daemon

    class ReplyableFakeAdapter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, binding, target, text, *, idempotency_key=None):
            message_id = f"om_sent_{len(self.sent) + 1}"
            self.sent.append(message_id)
            return message_id

        def start_ingress(self, binding_id: str) -> None: ...

        def stop_ingress(self, binding_id: str) -> None: ...

    import app.channels.adapters.feishu  # noqa: F401  确保真实适配器已注册

    original = get_channel_adapter("feishu")
    fake = ReplyableFakeAdapter()
    register_channel_adapter("feishu", fake)
    try:
        engine = _test_engine()
        with Session(engine) as db:
            _seed_tenant(db)
            binding = _feishu_binding()
            handoff = _pending_handoff(handoff_id="handoff_wb")
            db.add(binding)
            db.add(handoff)
            db.add(
                ChannelDelivery(
                    tenant_id="tenant_demo",
                    binding_id=binding.id,
                    session_id="handoff:handoff_wb",
                    kind="handoff_notice",
                    text="通知",
                    target_json={
                        "receive_id_type": "open_id",
                        "receive_id": "ou_assignee",
                        "handoff_id": "handoff_wb",
                    },
                    status="pending",
                    next_attempt_at=utc_now(),
                    idempotency_key="wb_notice",
                )
            )
            db.add(
                ChannelDelivery(
                    tenant_id="tenant_demo",
                    binding_id=binding.id,
                    session_id="handoff:handoff_wb",
                    kind="handoff_ack",
                    text="已收到你的回复",
                    target_json={
                        "receive_id_type": "open_id",
                        "receive_id": "ou_assignee",
                    },
                    status="pending",
                    next_attempt_at=utc_now(),
                    idempotency_key="wb_ack",
                )
            )
            db.commit()

        run_delivery_daemon(once=True, db_engine=engine)

        with Session(engine) as db:
            deliveries = db.exec(select(ChannelDelivery)).all()
            by_kind = {row.kind: row for row in deliveries}
            assert by_kind["handoff_notice"].status == "delivered"
            assert by_kind["handoff_notice"].message_id == "om_sent_1"
            assert by_kind["handoff_ack"].status == "delivered"
            assert by_kind["handoff_ack"].message_id == "om_sent_2"
            refreshed = db.get(HumanHandoffRequest, "handoff_wb")
            assert refreshed is not None
            assert refreshed.notify_message_id == "om_sent_1"
    finally:
        register_channel_adapter("feishu", original)


# ---------------------------------------------------------------------------
# 通用渠道 handoff 通知:wecom 投递 + binding 解析
# ---------------------------------------------------------------------------


def _wecom_binding(
    *,
    binding_id: str = "binding_wecom",
    scope: str = "corp_1",
) -> ChannelBinding:
    return ChannelBinding(
        id=binding_id,
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        channel="wecom",
        status="active",
        config_json={"corp_id": scope, "bot_id": "bot_1"},
        external_account_key="wecom:corp:6:corp_1:bot:5:bot_1",
        identity_scope_key=scope,
    )


def test_notify_handoff_assignee_stages_wecom_delivery_with_chat_id() -> None:
    """企微绑定:按 binding scope 解析非群聊身份,target 用 to_user_id(chatid)。"""
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_assignee",
                scope="corp_1",
            )
        )
        db.add(_pending_handoff())
        db.commit()

        notify_handoff_assignee(db, binding, _pending_handoff(), "网络故障", "user: 网络断了")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.binding_id == "binding_wecom"
        assert delivery.target_json["to_user_id"] == "staff_assignee"
        assert delivery.target_json["handoff_id"] == "handoff_demo"
        assert "指派人" in delivery.text
        assert delivery.status == "pending"
        assert "发送 /回复反馈" in delivery.text
        assert "直接回复本条消息" not in delivery.text


def test_notify_handoff_assignee_retries_old_delivered_wecom_notice() -> None:
    from datetime import timedelta

    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_assignee",
                scope="corp_1",
            )
        )
        handoff = _pending_handoff()
        db.add(handoff)
        old_notice = ChannelDelivery(
            tenant_id="tenant_demo",
            binding_id=binding.id,
            session_id=f"handoff:{handoff.id}",
            kind="handoff_notice",
            text="旧通知",
            target_json={"to_user_id": "staff_assignee"},
            status="delivered",
            idempotency_key="old-handoff-notice",
            created_at=utc_now() - timedelta(seconds=3600),
        )
        db.add(old_notice)
        db.commit()

        notify_handoff_assignee(db, binding, handoff, "新问题", "")
        notices = db.exec(
            select(ChannelDelivery).where(
                ChannelDelivery.kind == "handoff_notice",
                ChannelDelivery.session_id == f"handoff:{handoff.id}",
            )
        ).all()
        assert len(notices) == 2
        assert any(row.status == "pending" and row.text != "旧通知" for row in notices)


def test_run_wecom_handoff_reply_records_wecom_source(monkeypatch) -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_assignee",
                scope="corp_1",
            )
        )
        session = ChatSession(
            id="session_wecom_reply",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(
            handoff_id="handoff_wecom_reply",
            session_id=session.id,
        )
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = _inbound(
            event_id="wecom_reply_1",
            from_user_id="staff_assignee",
            text="/回复反馈 已处理",
        )
        command = ChannelCommand(kind="handoff_reply", query="已处理")
        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append(source)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)
        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: "corp_1"
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == ["wecom"]
            assert result is intake_mod._HANDOFF_REPLY_HANDLED
            assert db.get(HumanHandoffRequest, handoff.id).status == "answered"
        finally:
            intake_mod.external_account_scope = original


def test_run_wecom_handoff_reply_uses_latest_delivered_notice(monkeypatch) -> None:
    from datetime import timedelta

    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_assignee",
                scope="corp_1",
            )
        )
        older = _pending_handoff(handoff_id="handoff_old")
        newer = _pending_handoff(handoff_id="handoff_new")
        newer.created_at = older.created_at + timedelta(seconds=10)
        db.add(older)
        db.add(newer)
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                session_id=f"handoff:{older.id}",
                kind="handoff_notice",
                text="旧通知",
                target_json={"to_user_id": "staff_assignee", "handoff_id": older.id},
                status="delivered",
                created_at=older.created_at,
                idempotency_key="notice-old",
            )
        )
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                session_id=f"handoff:{newer.id}",
                kind="handoff_notice",
                text="新通知",
                target_json={"to_user_id": "staff_assignee", "handoff_id": newer.id},
                status="delivered",
                created_at=newer.created_at,
                idempotency_key="notice-new",
            )
        )
        db.commit()

        inbound = _inbound(
            event_id="wecom_reply_latest",
            from_user_id="staff_assignee",
            text="/回复反馈 已处理最新请求",
        )
        inbound.channel = "wecom"
        command = ChannelCommand(kind="handoff_reply", query="已处理最新请求")
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
        intake_mod.external_account_scope = lambda _db, _b: "corp_1"
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == [newer.id]
            assert result is intake_mod._HANDOFF_REPLY_HANDLED
            assert db.get(HumanHandoffRequest, older.id).status == "pending"
        finally:
            intake_mod.external_account_scope = original


def test_run_wecom_handoff_reply_accepts_explicit_handoff_id(monkeypatch) -> None:
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_assignee",
                scope="corp_1",
            )
        )
        first = _pending_handoff(handoff_id="handoff_explicit_1")
        second = _pending_handoff(handoff_id="handoff_explicit_2")
        db.add(first)
        db.add(second)
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                session_id=f"handoff:{first.id}",
                kind="handoff_notice",
                text="通知 1",
                target_json={"to_user_id": "staff_assignee", "handoff_id": first.id},
                status="delivered",
                idempotency_key="notice-explicit-1",
            )
        )
        db.add(
            ChannelDelivery(
                tenant_id="tenant_demo",
                binding_id=binding.id,
                session_id=f"handoff:{second.id}",
                kind="handoff_notice",
                text="通知 2",
                target_json={"to_user_id": "staff_assignee", "handoff_id": second.id},
                status="delivered",
                idempotency_key="notice-explicit-2",
            )
        )
        db.commit()

        inbound = _inbound(
            event_id="wecom_reply_explicit",
            from_user_id="staff_assignee",
            text=f"/回复反馈 {first.id} 回复第一个请求",
        )
        inbound.channel = "wecom"
        command = ChannelCommand(kind="handoff_reply", query=f"{first.id} 回复第一个请求")
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
        intake_mod.external_account_scope = lambda _db, _b: "corp_1"
        try:
            result = _run_handoff_reply_command(db, binding, inbound, command)
            assert resumed == [first.id]
            assert result is intake_mod._HANDOFF_REPLY_HANDLED
            assert db.get(HumanHandoffRequest, second.id).status == "pending"
        finally:
            intake_mod.external_account_scope = original


def test_run_handoff_reply_rejects_notification_placeholder() -> None:
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding()
        db.add(binding)
        result = _run_handoff_reply_command(
            db,
            binding,
            _inbound(
                event_id="wecom_placeholder",
                from_user_id="staff_assignee",
                text="/回复反馈 handoff_demo <答复内容> 精确回复此请求。",
            ),
            ChannelCommand(
                kind="handoff_reply",
                query="handoff_demo <答复内容> 精确回复此请求。",
            ),
        )
        assert "替换成实际回复文本" in result


def test_notify_handoff_assignee_skips_when_identity_scope_mismatches() -> None:
    """scope 级隔离:assignee 身份挂在其他企微企业 scope 下时跳过(网页收件箱兜底)。"""
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _wecom_binding(scope="corp_1")
        db.add(binding)
        # assignee 只在另一个企业(corp_2)绑定过身份
        db.add(
            _channel_identity(
                channel="wecom",
                external_user_id="staff_other_org",
                scope="corp_2",
            )
        )
        db.add(_pending_handoff())
        db.commit()

        notify_handoff_assignee(db, binding, _pending_handoff(), "问题", "")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert deliveries == []


def test_notify_handoff_assignee_skips_unsupported_private_message_channel() -> None:
    """钉钉不支持主动私聊:即使身份可达也不登记投递。"""
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = ChannelBinding(
            id="binding_dingtalk",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            channel="dingtalk",
            status="active",
            config_json={"client_id": "cli_d"},
        )
        db.add(binding)
        db.add(
            _channel_identity(
                channel="dingtalk",
                external_user_id="staff_assignee",
                scope="",
            )
        )
        db.add(_pending_handoff())
        db.commit()

        notify_handoff_assignee(db, binding, _pending_handoff(), "问题", "")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert deliveries == []


def test_resolve_handoff_notify_binding_finds_active_employee_binding() -> None:
    from app.channels.service_outbox import resolve_handoff_notify_binding

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        active = _feishu_binding(binding_id="binding_active", app_id="cli_active")
        disabled = _feishu_binding(binding_id="binding_disabled", app_id="cli_disabled")
        disabled.status = "disabled"
        team = _feishu_binding(binding_id="binding_team", app_id="cli_team")
        team.team_id = "team_1"
        db.add(active)
        db.add(disabled)
        db.add(team)
        db.commit()

        assert resolve_handoff_notify_binding(db, "tenant_demo", "feishu").id == "binding_active"
        assert resolve_handoff_notify_binding(db, "tenant_demo", "wecom") is None
        assert resolve_handoff_notify_binding(db, "tenant_demo", "web") is None
        assert resolve_handoff_notify_binding(db, "tenant_demo", "") is None


# ---------------------------------------------------------------------------
# AgentLoop 通知路由:按 notify_channel 解析 binding,不再 feishu 硬编码
# ---------------------------------------------------------------------------


class _RecordingOutbox:
    """替身 notify_handoff_assignee:记录被调用的 binding,便于断言路由结果。"""

    def __init__(self) -> None:
        self.calls: list[ChannelBinding] = []

    def __call__(self, db, binding, handoff, pending_question, context_summary) -> None:
        self.calls.append(binding)


def _loop_with_session(db: Session, chat_session: ChatSession):
    from app.core.agent_loop import AgentLoop

    return AgentLoop(db), chat_session


def test_agent_loop_notify_routes_declared_channel_to_matching_binding() -> None:
    """偏好 feishu 但会话在企微:回退到租户内 feishu active binding 投递。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        feishu = _feishu_binding(binding_id="binding_feishu", app_id="cli_a")
        wecom = _wecom_binding()
        db.add(feishu)
        db.add(wecom)
        agent = AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="demo", config_json={})
        db.add(agent)
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            user_id="admin_user",
            channel="wecom",
            channel_binding_id="binding_wecom",
        )
        db.add(session)
        db.add(
            _pending_handoff(metadata={"assignee_notify_channel": "feishu"})
        )
        db.commit()

        recorder = _RecordingOutbox()
        import app.channels.service_outbox as outbox_mod

        original = outbox_mod.notify_handoff_assignee
        outbox_mod.notify_handoff_assignee = recorder
        try:
            loop, chat_session = _loop_with_session(db, session)
            loop._maybe_notify_handoff_assignee("tenant_demo", chat_session, _pending_handoff(
                metadata={"assignee_notify_channel": "feishu"}
            ))
        finally:
            outbox_mod.notify_handoff_assignee = original

        assert [binding.id for binding in recorder.calls] == ["binding_feishu"]


def test_agent_loop_notify_prefers_session_binding_when_channel_matches() -> None:
    """偏好 wecom 且会话就在 wecom binding:直接用会话所属 binding。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        wecom_session = _wecom_binding(binding_id="binding_wecom_session", scope="corp_session")
        wecom_session.external_account_key = "wecom:corp:12:corp_session:bot:5:bot_1"
        wecom_other = _wecom_binding(binding_id="binding_wecom_other", scope="corp_other")
        wecom_other.external_account_key = "wecom:corp:10:corp_other:bot:5:bot_1"
        db.add(wecom_session)
        db.add(wecom_other)
        agent = AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="demo", config_json={})
        db.add(agent)
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            user_id="admin_user",
            channel="wecom",
            channel_binding_id="binding_wecom_session",
        )
        db.add(session)
        db.commit()

        recorder = _RecordingOutbox()
        import app.channels.service_outbox as outbox_mod

        original = outbox_mod.notify_handoff_assignee
        outbox_mod.notify_handoff_assignee = recorder
        try:
            loop, chat_session = _loop_with_session(db, session)
            loop._maybe_notify_handoff_assignee("tenant_demo", chat_session, _pending_handoff(
                metadata={"assignee_notify_channel": "wecom"}
            ))
        finally:
            outbox_mod.notify_handoff_assignee = original

        assert [binding.id for binding in recorder.calls] == ["binding_wecom_session"]


def test_agent_loop_notify_skips_web_preference_and_missing_binding() -> None:
    """"web" 偏好仅网页收件箱;指定渠道租户内无 active 绑定时跳过且不抛错。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        agent = AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="demo", config_json={})
        db.add(agent)
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            user_id="admin_user",
        )
        db.add(session)
        db.commit()

        recorder = _RecordingOutbox()
        import app.channels.service_outbox as outbox_mod

        original = outbox_mod.notify_handoff_assignee
        outbox_mod.notify_handoff_assignee = recorder
        try:
            loop, chat_session = _loop_with_session(db, session)
            loop._maybe_notify_handoff_assignee(
                "tenant_demo", chat_session, _pending_handoff(metadata={"assignee_notify_channel": "web"})
            )
            # 租户内无 feishu active 绑定:静默跳过,不调用 notify
            loop._maybe_notify_handoff_assignee(
                "tenant_demo", chat_session, _pending_handoff(metadata={"assignee_notify_channel": "feishu"})
            )
        finally:
            outbox_mod.notify_handoff_assignee = original

        assert recorder.calls == []


def test_agent_loop_notify_default_uses_session_binding_when_supported() -> None:
    """无偏好(默认):会话所属 binding 渠道支持私聊通知即投递(企微也走)。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        wecom = _wecom_binding()
        db.add(wecom)
        agent = AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="demo", config_json={})
        db.add(agent)
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            user_id="admin_user",
            channel="wecom",
            channel_binding_id="binding_wecom",
        )
        db.add(session)
        db.commit()

        recorder = _RecordingOutbox()
        import app.channels.service_outbox as outbox_mod

        original = outbox_mod.notify_handoff_assignee
        outbox_mod.notify_handoff_assignee = recorder
        try:
            loop, chat_session = _loop_with_session(db, session)
            loop._maybe_notify_handoff_assignee("tenant_demo", chat_session, _pending_handoff(metadata={}))
        finally:
            outbox_mod.notify_handoff_assignee = original

        assert [binding.id for binding in recorder.calls] == ["binding_wecom"]


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


def test_try_handle_feishu_handoff_reply_consumes_already_answered_notice(monkeypatch) -> None:
    """引用已答复的通知再回复:消费并提示已处理,不落入数字员工会话。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding, handoff, inbound, event = _seed_handoff_reply_scenario(db)
        handoff.status = "answered"
        handoff.human_reply = "首次答复"
        db.add(handoff)
        db.commit()

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            applied: list[tuple] = []

            def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
                applied.append((row.id, reply))

            import app.api.chat as chat_api

            monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

            handled = _try_handle_feishu_handoff_reply(db, binding, inbound, event, {})
            assert handled is True
            assert applied == []
            refreshed = db.get(HumanHandoffRequest, handoff.id)
            assert refreshed.status == "answered"
            assert refreshed.human_reply == "首次答复"
            assert event.status == "done"
            acks = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).all()
            assert len(acks) == 1
            assert "已处理" in acks[0].text
        finally:
            intake_mod.external_account_scope = original


def test_try_handle_feishu_handoff_reply_consumes_reply_to_ack_message(monkeypatch) -> None:
    """引用 handoff_ack 确认消息再回复:提示已处理,不落入数字员工会话。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding, handoff, _inbound_notice_reply, _event_notice = _seed_handoff_reply_scenario(db)
        handoff.status = "answered"
        handoff.human_reply = "首次答复"
        db.add(handoff)
        # 确认消息已投递,处理人转而引用它继续补充
        db.add(ChannelDelivery(
            tenant_id="tenant_demo",
            binding_id=binding.id,
            session_id=f"handoff:{handoff.id}",
            message_id="om_ack_1",
            kind="handoff_ack",
            text="已收到你的回复",
            target_json={
                "receive_id_type": "open_id",
                "receive_id": "ou_assignee",
            },
            status="delivered",
            idempotency_key="ack_k",
        ))
        db.commit()
        inbound = _inbound(
            event_id="om_reply_ack",
            from_user_id="ou_assignee",
            text="再补充一点",
            parent_id="om_ack_1",
        )
        event = _inbound_event(event_id="om_reply_ack", binding_id=binding.id)
        db.add(event)
        db.commit()

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            applied: list[tuple] = []

            def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
                applied.append((row.id, reply))

            import app.api.chat as chat_api

            monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

            handled = _try_handle_feishu_handoff_reply(db, binding, inbound, event, {})
            assert handled is True
            assert applied == []
            assert db.get(HumanHandoffRequest, handoff.id).human_reply == "首次答复"
            assert event.status == "done"
            acks = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).all()
            assert len(acks) == 2  # 已投递的确认 + 新提示
            assert any("已处理" in ack.text for ack in acks if ack.message_id is None)
        finally:
            intake_mod.external_account_scope = original


def test_process_inbound_quote_reply_without_prefix_answers_handoff(monkeypatch) -> None:
    """端到端:处理人引用通知消息直接回复(无前缀)即完成答复,不创建新会话。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import process_inbound

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity(external_user_id="ou_assignee"))
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="数字员工A"))
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(
            notify_message_id="om_notice_e2e",
            assignee_user_id="assignee_user",
        )
        db.add(session)
        db.add(handoff)
        db.add(ChannelDelivery(
            tenant_id="tenant_demo",
            binding_id=binding.id,
            session_id=f"handoff:{handoff.id}",
            message_id="om_notice_e2e",
            kind="handoff_notice",
            text="通知",
            target_json={
                "receive_id_type": "open_id",
                "receive_id": "ou_assignee",
                "handoff_id": handoff.id,
            },
            status="delivered",
            idempotency_key="notice_e2e",
        ))
        db.commit()

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            applied: list[tuple] = []

            def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
                row.status = "answered"
                row.human_reply = reply
                row.answered_at = utc_now()
                db_arg.add(row)
                db_arg.commit()
                applied.append((row.id, reply, source))

            import app.api.chat as chat_api

            monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

            inbound = ChannelInbound(
                channel="feishu",
                event_id="om_reply_e2e",
                from_user_id="ou_assignee",
                to_user_id="ou_bot",
                session_id="ou_assignee",
                group_id="",
                context_token="om_reply_e2e",
                text="已修复网络",
                is_group=False,
                raw={"message": {"parent_id": "om_notice_e2e", "root_id": ""}},
                parent_id="om_notice_e2e",
            )
            executed = process_inbound(binding, inbound, db_engine=engine)
            assert executed is False  # 未进入 AgentLoop 对话轮
            assert applied == [(handoff.id, "已修复网络", "feishu")]

            db.expire_all()
            refreshed = db.get(HumanHandoffRequest, handoff.id)
            assert refreshed.status == "answered"
            assert refreshed.human_reply == "已修复网络"
            acks = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).all()
            assert len(acks) == 1
            assert "已收到你的回复" in acks[0].text
            # 关键回归:不触发处理人与数字员工的新对话(仅存在原 handoff 会话)
            sessions = db.exec(select(ChatSession)).all()
            assert [s.id for s in sessions] == ["session_demo"]
            from app.db.models import ChannelInboundEvent

            events = db.exec(
                select(ChannelInboundEvent).where(
                    ChannelInboundEvent.event_id == "om_reply_e2e"
                )
            ).all()
            assert [e.status for e in events] == ["done"]
        finally:
            intake_mod.external_account_scope = original


def test_process_inbound_quote_reply_to_answered_notice_consumes_without_new_session(
    monkeypatch,
) -> None:
    """端到端:引用已答复的通知回复被消费,不创建处理人与数字员工的新会话。"""
    import app.channels.service_intake as intake_mod
    from app.channels.service_intake import process_inbound

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(_channel_identity(external_user_id="ou_assignee"))
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="数字员工A"))
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = _pending_handoff(
            notify_message_id="om_notice_e2e",
            assignee_user_id="assignee_user",
        )
        handoff.status = "answered"
        handoff.human_reply = "首次答复"
        db.add(session)
        db.add(handoff)
        db.add(ChannelDelivery(
            tenant_id="tenant_demo",
            binding_id=binding.id,
            session_id=f"handoff:{handoff.id}",
            message_id="om_notice_e2e",
            kind="handoff_notice",
            text="通知",
            target_json={
                "receive_id_type": "open_id",
                "receive_id": "ou_assignee",
                "handoff_id": handoff.id,
            },
            status="delivered",
            idempotency_key="notice_e2e",
        ))
        db.commit()

        original = intake_mod.external_account_scope
        intake_mod.external_account_scope = lambda _db, _b: ""
        try:
            applied: list[tuple] = []

            def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
                applied.append((row.id, reply))

            import app.api.chat as chat_api

            monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

            inbound = ChannelInbound(
                channel="feishu",
                event_id="om_reply_late",
                from_user_id="ou_assignee",
                to_user_id="ou_bot",
                session_id="ou_assignee",
                group_id="",
                context_token="om_reply_late",
                text="又补充了一点",
                is_group=False,
                raw={"message": {"parent_id": "om_notice_e2e", "root_id": ""}},
                parent_id="om_notice_e2e",
            )
            executed = process_inbound(binding, inbound, db_engine=engine)
            assert executed is False
            assert applied == []
            db.expire_all()
            refreshed = db.get(HumanHandoffRequest, handoff.id)
            assert refreshed.human_reply == "首次答复"
            acks = db.exec(
                select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
            ).all()
            assert len(acks) == 1
            assert "已处理" in acks[0].text
            sessions = db.exec(select(ChatSession)).all()
            assert [s.id for s in sessions] == ["session_demo"]
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
