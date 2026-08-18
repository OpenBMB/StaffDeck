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
    KnowledgeBaseVersion,
    KnowledgeConcept,
    Tenant,
    User,
    utc_now,
)
from app.knowledge.okf import (
    CONCEPT_TYPES,
    CONTACT_FRONTMATTER_KEYS,
    extract_contact_target,
    search_concepts,
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
        password_hash="x",
    )
    db.add(tenant)
    db.add(admin)
    db.add(assignee)
    db.commit()
    return tenant, admin, assignee


def _contact_concept(
    *,
    concept_id: str = "contacts/it_staff",
    title: str = "真人员工 IT",
    staffdeck_user_id: str = "assignee_user",
    feishu_open_id: str = "",
    feishu_mobile: str = "",
    feishu_email: str = "",
    keywords: list[str] | None = None,
) -> KnowledgeConcept:
    frontmatter: dict = {
        "type": "Contact",
        "title": title,
        "name": title,
        "role": "IT 问题处理",
        "responsibilities": "负责网络故障、系统报错、账号问题",
    }
    if keywords:
        frontmatter["keywords"] = keywords
    else:
        frontmatter["keywords"] = ["网络故障", "系统报错", "账号问题"]
    if staffdeck_user_id:
        frontmatter["staffdeck_user_id"] = staffdeck_user_id
    if feishu_open_id:
        frontmatter["feishu_open_id"] = feishu_open_id
    if feishu_mobile:
        frontmatter["feishu_mobile"] = feishu_mobile
    if feishu_email:
        frontmatter["feishu_email"] = feishu_email
    content = "---\n" + "\n".join(f"{k}: {v}" for k, v in frontmatter.items() if not isinstance(v, list)) + "\n---\n\n# 联系人说明\n\n处理 IT 类问题。"
    return KnowledgeConcept(
        id="kconcept_contact",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_demo",
        knowledge_base_version_id="kbv_demo",
        concept_id=concept_id,
        concept_type="Contact",
        title=title,
        description="IT 问题联系人",
        content_md=content,
        frontmatter_json=frontmatter,
    )


# ---------------------------------------------------------------------------
# 阶段 1:Contact 概念类型与检索
# ---------------------------------------------------------------------------


def test_contact_is_registered_in_concept_types() -> None:
    assert "Contact" in CONCEPT_TYPES
    # 关键字段键已声明,供解析/检索统一引用
    for key in ("feishu_open_id", "feishu_mobile", "feishu_email", "staffdeck_user_id"):
        assert key in CONTACT_FRONTMATTER_KEYS


def test_extract_contact_target_reads_frontmatter_fields() -> None:
    concept = _contact_concept(
        staffdeck_user_id="assignee_user",
        feishu_open_id="ou_open123",
        feishu_mobile="13800000000",
    )
    target = extract_contact_target(concept)
    assert target["staffdeck_user_id"] == "assignee_user"
    assert target["feishu_open_id"] == "ou_open123"
    assert target["feishu_mobile"] == "13800000000"
    assert target["name"] == "真人员工 IT"
    assert "concept_id" not in target  # extract 不带 concept_id


def test_extract_contact_target_returns_empty_for_non_contact() -> None:
    concept = KnowledgeConcept(
        tenant_id="t",
        knowledge_base_id="kb",
        concept_id="topics/x",
        concept_type="Topic",
        title="x",
        content_md="x",
        frontmatter_json={},
    )
    assert extract_contact_target(concept) == {}


def test_search_concepts_matches_contact_by_keyword() -> None:
    contact = _contact_concept(keywords=["网络故障", "系统报错"])
    # 一个不相关的 Topic 概念,正文也提到"网络"但不那么强
    topic = KnowledgeConcept(
        tenant_id="t",
        knowledge_base_id="kb",
        concept_id="topics/network",
        concept_type="Topic",
        title="网络概览",
        description="介绍网络拓扑",
        content_md="网络拓扑说明",
        frontmatter_json={},
    )
    matched = search_concepts("网络故障", [contact, topic], limit=5)
    assert contact in matched
    # Contact 命中应优先于通用 Topic(加权)
    assert matched[0].concept_type == "Contact"


def test_search_concepts_ignores_contact_without_matching_keywords() -> None:
    contact = _contact_concept(keywords=["网络故障"])
    matched = search_concepts("财务报表", [contact], limit=5)
    assert matched == []


# ---------------------------------------------------------------------------
# 阶段 2:知识库驱动 assignee
# ---------------------------------------------------------------------------


def test_resolve_contact_assignee_returns_staffdeck_user_id_on_hit(monkeypatch) -> None:
    from app.core import human_handoff_service as hhs_mod
    from app.core.human_handoff_service import HumanHandoffService

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT 员工"))
        version = KnowledgeBaseVersion(
            id="kbv_demo",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            version="1.0.0",
            name="IT 知识库",
            status="active",
        )
        db.add(version)
        db.add(_contact_concept(staffdeck_user_id="assignee_user"))
        db.commit()

        monkeypatch.setattr(
            hhs_mod,
            "visible_knowledge_base_versions",
            lambda db_arg, tenant_id, agent_id: {"kb_demo": version},
        )

        service = HumanHandoffService(db, FakeEvents())
        target = service.resolve_contact_assignee(
            "tenant_demo", "agent_demo", "网络故障导致无法登录"
        )
        assert target["staffdeck_user_id"] == "assignee_user"
        assert target["concept_id"] == "contacts/it_staff"


def test_resolve_contact_assignee_returns_empty_when_no_contact(monkeypatch) -> None:
    from app.core import human_handoff_service as hhs_mod
    from app.core.human_handoff_service import HumanHandoffService

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        db.add(AgentProfile(id="agent_demo", tenant_id="tenant_demo", name="IT 员工"))
        version = KnowledgeBaseVersion(
            id="kbv_demo",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            version="1.0.0",
            name="IT 知识库",
            status="active",
        )
        db.add(version)
        # 没有 Contact 概念,只有 Topic
        db.add(
            KnowledgeConcept(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                knowledge_base_version_id="kbv_demo",
                concept_id="topics/network",
                concept_type="Topic",
                title="网络",
                content_md="网络说明",
                frontmatter_json={},
            )
        )
        db.commit()

        monkeypatch.setattr(
            hhs_mod,
            "visible_knowledge_base_versions",
            lambda db_arg, tenant_id, agent_id: {"kb_demo": version},
        )

        service = HumanHandoffService(db, FakeEvents())
        assert service.resolve_contact_assignee("tenant_demo", "agent_demo", "网络故障") == {}


def test_resolve_contact_assignee_returns_empty_without_agent_id() -> None:
    from app.core.human_handoff_service import HumanHandoffService

    engine = _test_engine()
    with Session(engine) as db:
        service = HumanHandoffService(db, FakeEvents())
        assert service.resolve_contact_assignee("tenant_demo", None, "网络故障") == {}


# ---------------------------------------------------------------------------
# 阶段 3:handoff_notice 投递登记 + open_id 获取 + 回写 notify_message_id
# ---------------------------------------------------------------------------


def _feishu_binding() -> ChannelBinding:
    return ChannelBinding(
        id="binding_feishu",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        channel="feishu",
        status="active",
        config_json={"app_id": "cli_app"},
        credentials_enc=encrypt_channel_secret("secret-value"),
        external_account_key="feishu:app:7:cli_app",
        provider_tenant_key="tenant_key",
        config_revision=1,
    )


def test_resolve_open_id_prefers_direct_feishu_open_id() -> None:
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()
        contact_target = {"feishu_open_id": "ou_direct_open"}
        open_id = _resolve_assignee_feishu_open_id(db, binding, "assignee_user", contact_target)
        assert open_id == "ou_direct_open"


def test_resolve_open_id_falls_back_to_channel_identity() -> None:
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_from_identity",
                staffdeck_user_id="assignee_user",
            )
        )
        db.commit()
        # Contact 里没填 open_id,应从 ChannelIdentity 取
        open_id = _resolve_assignee_feishu_open_id(db, binding, "assignee_user", {})
        assert open_id == "ou_from_identity"


def test_resolve_open_id_returns_none_without_any_source() -> None:
    from app.channels.service_outbox import _resolve_assignee_feishu_open_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()
        # 无 open_id、无 identity、无手机号/邮箱
        assert _resolve_assignee_feishu_open_id(db, binding, "assignee_user", {}) is None


def test_notify_handoff_assignee_stages_handoff_notice_delivery() -> None:
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_assignee",
                staffdeck_user_id="assignee_user",
            )
        )
        handoff = HumanHandoffRequest(
            id="handoff_demo",
            tenant_id="tenant_demo",
            session_id="session_demo",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="网络故障",
            context_summary="user: 网络断了",
            status="pending",
        )
        db.add(handoff)
        db.commit()

        notify_handoff_assignee(
            db,
            binding,
            handoff,
            {"name": "真人员工 IT"},
            "网络故障",
            "user: 网络断了",
        )
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.target_json["receive_id_type"] == "open_id"
        assert delivery.target_json["receive_id"] == "ou_assignee"
        assert delivery.target_json["handoff_id"] == "handoff_demo"
        assert "真人员工 IT" in delivery.text
        assert "网络故障" in delivery.text
        assert delivery.status == "pending"


def test_notify_handoff_assignee_skips_when_no_open_id() -> None:
    from app.channels.service_outbox import notify_handoff_assignee

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        handoff = HumanHandoffRequest(
            id="handoff_no_open",
            tenant_id="tenant_demo",
            session_id="session_demo",
            assignee_user_id="assignee_user",
            pending_question="问题",
            status="pending",
        )
        db.add(handoff)
        db.commit()

        # 无 identity、无 contact open_id、无手机号 → 跳过,不登记 delivery
        notify_handoff_assignee(db, binding, handoff, {}, "问题", "")
        deliveries = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")
        ).all()
        assert deliveries == []


def test_write_handoff_notify_message_id_persists_message_id() -> None:
    from app.channels.service_outbox import _write_handoff_notify_message_id

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        handoff = HumanHandoffRequest(
            id="handoff_write",
            tenant_id="tenant_demo",
            session_id="session_demo",
            status="pending",
        )
        db.add(handoff)
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
# 阶段 3:FeishuAdapter.send 透传 message_id
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
# 阶段 4:飞书归一化捕获 parent_id + 回复→handoff 关联
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
    # 默认空字符串
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
# 阶段 4:service_intake 飞书回复→handoff 关联
# ---------------------------------------------------------------------------


def test_try_handle_feishu_handoff_reply_matches_and_answers_handoff(monkeypatch) -> None:
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_assignee",
                staffdeck_user_id="assignee_user",
            )
        )
        session = ChatSession(
            id="session_demo",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = HumanHandoffRequest(
            id="handoff_reply",
            tenant_id="tenant_demo",
            session_id="session_demo",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="网络故障",
            notify_message_id="om_notify_1",
            status="pending",
        )
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_reply_1",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_reply_1",
            text="已修复网络",
            is_group=False,
            raw={},
            parent_id="om_notify_1",
        )
        # 复用一个最小 event 行
        from app.db.models import ChannelInboundEvent

        event = ChannelInboundEvent(
            id="chevt_1",
            tenant_id="tenant_demo",
            binding_id=binding.id,
            channel="feishu",
            event_id="om_reply_1",
            payload_json={},
            status="processing",
            target_json={},
        )
        db.add(event)
        db.commit()

        # 避免真正启动 resume 线程
        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        handled = _try_handle_feishu_handoff_reply(
            db, binding, inbound, event, {"receive_id_type": "open_id", "receive_id": "ou_assignee"}
        )
        assert handled is True
        assert resumed == ["handoff_reply"]
        # event 标 done
        refreshed_event = db.get(ChannelInboundEvent, "chevt_1")
        assert refreshed_event.status == "done"
        # 回执 delivery 已登记
        ack = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).first()
        assert ack is not None
        assert "已收到你的回复" in ack.text


def test_try_handle_feishu_handoff_reply_rejects_non_assignee_sender() -> None:
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        # 发送者是另一个用户,不是 assignee
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_stranger",
                staffdeck_user_id="stranger_user",
            )
        )
        handoff = HumanHandoffRequest(
            id="handoff_guard",
            tenant_id="tenant_demo",
            session_id="session_demo",
            assignee_user_id="assignee_user",
            notify_message_id="om_notify_2",
            status="pending",
        )
        db.add(handoff)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_reply_2",
            from_user_id="ou_stranger",
            to_user_id="ou_bot",
            session_id="ou_stranger",
            group_id="",
            context_token="om_reply_2",
            text="冒充回复",
            is_group=False,
            raw={},
            parent_id="om_notify_2",
        )
        from app.db.models import ChannelInboundEvent

        event = ChannelInboundEvent(
            id="chevt_2",
            tenant_id="tenant_demo",
            binding_id=binding.id,
            channel="feishu",
            event_id="om_reply_2",
            payload_json={},
            status="processing",
            target_json={},
        )
        db.add(event)
        db.commit()

        handled = _try_handle_feishu_handoff_reply(
            db, binding, inbound, event, {}
        )
        assert handled is False
        # handoff 仍 pending
        assert db.get(HumanHandoffRequest, "handoff_guard").status == "pending"


def test_try_handle_feishu_handoff_reply_returns_false_without_parent_id() -> None:
    from app.channels.service_intake import _try_handle_feishu_handoff_reply

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()
        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_plain",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_plain",
            text="普通消息",
            is_group=False,
            raw={},
            parent_id="",
        )
        from app.db.models import ChannelInboundEvent

        event = ChannelInboundEvent(
            id="chevt_plain",
            tenant_id="tenant_demo",
            binding_id=binding.id,
            channel="feishu",
            event_id="om_plain",
            payload_json={},
            status="processing",
            target_json={},
        )
        db.add(event)
        db.commit()
        assert _try_handle_feishu_handoff_reply(db, binding, inbound, event, {}) is False


# ---------------------------------------------------------------------------
# 阶段 4b:/回复反馈 指令解析与处理
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
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_assignee",
                staffdeck_user_id="assignee_user",
            )
        )
        session = ChatSession(
            id="session_hr1",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = HumanHandoffRequest(
            id="handoff_hr1",
            tenant_id="tenant_demo",
            session_id="session_hr1",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="网络故障",
            status="pending",
        )
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_hr_1",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_hr_1",
            text="/回复反馈 已修复网络",
            is_group=False,
            raw={},
            parent_id="",
        )
        command = ChannelCommand(kind="handoff_reply", query="已修复网络")

        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        result = _run_handoff_reply_command(db, binding, inbound, command)
        assert resumed == ["handoff_hr1"]
        assert "已收到回复" in result
        # handoff 已 answered
        assert db.get(HumanHandoffRequest, "handoff_hr1").status == "answered"
        # 确认 delivery 已登记
        ack = db.exec(
            select(ChannelDelivery).where(ChannelDelivery.kind == "error_notice")
        ).first()
        assert ack is not None
        assert "已收到你的回复" in ack.text


def test_run_handoff_reply_command_fallback_matches_by_contact_target_open_id(
    monkeypatch,
) -> None:
    """assignee 是 admin fallback(无 ChannelIdentity),通过 contact_target.feishu_open_id 匹配。"""
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        # 不创建 ChannelIdentity — assignee_user_id=admin_user, 发送者 open_id=ou_admin
        # 但 contact_target.feishu_open_id == ou_admin
        session = ChatSession(
            id="session_hr2",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
        handoff = HumanHandoffRequest(
            id="handoff_hr2",
            tenant_id="tenant_demo",
            session_id="session_hr2",
            agent_id="agent_demo",
            assignee_user_id="admin_user",
            pending_question="系统报错",
            status="pending",
            metadata_json={
                "contact_target": {
                    "feishu_open_id": "ou_admin",
                    "name": "管理员",
                }
            },
        )
        db.add(session)
        db.add(handoff)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_hr_2",
            from_user_id="ou_admin",
            to_user_id="ou_bot",
            session_id="ou_admin",
            group_id="",
            context_token="om_hr_2",
            text="/回复反馈 系统已恢复",
            is_group=False,
            raw={},
            parent_id="",
        )
        command = ChannelCommand(kind="handoff_reply", query="系统已恢复")

        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        result = _run_handoff_reply_command(db, binding, inbound, command)
        assert resumed == ["handoff_hr2"]
        assert "已收到回复" in result


def test_run_handoff_reply_command_no_pending_handoff_returns_error() -> None:
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_assignee",
                staffdeck_user_id="assignee_user",
            )
        )
        db.commit()
        # 没有 pending handoff

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_hr_3",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_hr_3",
            text="/回复反馈 已修复",
            is_group=False,
            raw={},
            parent_id="",
        )
        command = ChannelCommand(kind="handoff_reply", query="已修复")

        result = _run_handoff_reply_command(db, binding, inbound, command)
        assert "未找到" in result


def test_run_handoff_reply_command_empty_query_returns_usage() -> None:
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_hr_4",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_hr_4",
            text="/回复反馈",
            is_group=False,
            raw={},
            parent_id="",
        )
        command = ChannelCommand(kind="handoff_reply", query="")

        result = _run_handoff_reply_command(db, binding, inbound, command)
        assert "用法" in result


def test_run_handoff_reply_command_picks_latest_pending(monkeypatch) -> None:
    """多个 pending handoff 时,取 created_at 最新的那个。"""
    from app.channels.service_intake import _run_handoff_reply_command
    from app.channels.service_routing import ChannelCommand

    engine = _test_engine()
    with Session(engine) as db:
        _seed_tenant(db)
        binding = _feishu_binding()
        db.add(binding)
        db.add(
            ChannelIdentity(
                tenant_id="tenant_demo",
                channel="feishu",
                external_account_scope="",
                external_user_id="ou_assignee",
                staffdeck_user_id="assignee_user",
            )
        )
        db.add(
            ChatSession(
                id="session_hr_old",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                status="handoff",
            )
        )
        db.add(
            ChatSession(
                id="session_hr_new",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                status="handoff",
            )
        )
        old_time = utc_now()
        from datetime import timedelta

        new_time = old_time + timedelta(seconds=10)
        old_handoff = HumanHandoffRequest(
            id="handoff_old",
            tenant_id="tenant_demo",
            session_id="session_hr_old",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="旧问题",
            status="pending",
            created_at=old_time,
        )
        new_handoff = HumanHandoffRequest(
            id="handoff_new",
            tenant_id="tenant_demo",
            session_id="session_hr_new",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="新问题",
            status="pending",
            created_at=new_time,
        )
        db.add(old_handoff)
        db.add(new_handoff)
        db.commit()

        inbound = ChannelInbound(
            channel="feishu",
            event_id="om_hr_5",
            from_user_id="ou_assignee",
            to_user_id="ou_bot",
            session_id="ou_assignee",
            group_id="",
            context_token="om_hr_5",
            text="/回复反馈 解决了",
            is_group=False,
            raw={},
            parent_id="",
        )
        command = ChannelCommand(kind="handoff_reply", query="解决了")

        resumed: list[str] = []

        def fake_apply(db_arg, row, reply, *, answered_by_user_id):
            row.status = "answered"
            row.human_reply = reply
            row.answered_at = utc_now()
            db_arg.add(row)
            db_arg.commit()
            resumed.append(row.id)

        import app.api.chat as chat_api

        monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)

        _run_handoff_reply_command(db, binding, inbound, command)
        assert resumed == ["handoff_new"]
