from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment
from app.channels.service_feishu_inbox import (
    StageDisposition,
    decode_replay_envelope,
    encode_replay_envelope,
    feishu_account_key,
    feishu_identity_scope,
    stage_feishu_inbound,
)
from app.channels.service_intake import (
    claim_staged_inbound,
    process_staged_inbound,
    run_staged_inbound_daemon,
    sweep_stale_inbound_events,
)
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Tenant,
    User,
    new_id,
)


def _engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feishu-inbox.db'}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_binding(engine, *, revision: int = 7) -> str:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        binding = ChannelBinding(
            id="chan_feishu",
            tenant_id="tenant_a",
            agent_id="agent_a",
            channel="feishu",
            status="active",
            config_json={"app_id": "cli_app_a"},
            external_account_key=feishu_account_key("cli_app_a"),
            config_revision=revision,
        )
        db.add(binding)
        db.commit()
        return binding.id


def _inbound(event_id: str, text_value: str = "hello") -> ChannelInbound:
    return ChannelInbound(
        channel="feishu",
        event_id=event_id,
        from_user_id="ou_user_a",
        to_user_id="ou_bot_a",
        session_id="oc_chat_a",
        group_id="",
        context_token="om_message_a",
        text=text_value,
        is_group=False,
        raw={"event": {"message": {"message_id": event_id}}},
        sender_name="User A",
        account_scope="",
    )


def _stage(engine, event_id: str, *, tenant_key: str = "tenant_key_a", text_value="hello"):
    return stage_feishu_inbound(
        db_engine=engine,
        binding_id="chan_feishu",
        expected_revision=7,
        event_app_id="cli_app_a",
        tenant_key=tenant_key,
        inbound=_inbound(event_id, text_value),
        target={
            "message_id": event_id,
            "reply_in_thread": False,
            "receive_id_type": "open_id",
            "receive_id": "ou_user_a",
        },
    )


def test_stage_commits_received_event_and_provider_scope_without_side_effects(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)

    result = _stage(engine, "om_event_1")

    assert result.disposition is StageDisposition.STAGED
    assert result.should_ack is True
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        event_row = db.get(ChannelInboundEvent, result.event_pk)
        assert binding.provider_tenant_key == "tenant_key_a"
        assert binding.identity_scope_key == feishu_identity_scope(
            "cli_app_a", "tenant_key_a"
        )
        assert event_row.status == "received"
        assert event_row.config_revision == 7
        assert event_row.target_json == {
            "message_id": "om_event_1",
            "reply_in_thread": False,
            "receive_id_type": "open_id",
            "receive_id": "ou_user_a",
        }
        assert event_row.payload_json["schema_version"] == 1
        assert db.exec(select(User)).all() == []
        assert db.exec(select(ChatSession)).all() == []
        assert db.exec(select(Message)).all() == []


def test_duplicate_is_acknowledged_without_overwriting_first_envelope(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)

    first = _stage(engine, "om_same", text_value="first")
    duplicate = _stage(engine, "om_same", text_value="changed by retry")

    assert first.disposition is StageDisposition.STAGED
    assert duplicate.disposition is StageDisposition.DUPLICATE
    assert duplicate.event_pk == first.event_pk
    with Session(engine) as db:
        rows = db.exec(select(ChannelInboundEvent)).all()
        assert len(rows) == 1
        assert rows[0].payload_json["inbound"]["text"] == "first"


def test_competing_provider_tenant_is_security_dropped_without_new_event(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    assert _stage(engine, "om_first").disposition is StageDisposition.STAGED

    mismatch = _stage(engine, "om_other", tenant_key="tenant_key_b")

    assert mismatch.disposition is StageDisposition.SECURITY_DROP
    assert mismatch.should_ack is True
    assert mismatch.error_code == "provider_tenant_mismatch"
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        assert binding.provider_tenant_key == "tenant_key_a"
        assert len(db.exec(select(ChannelInboundEvent)).all()) == 1


def test_inbox_insert_failure_rolls_back_tenant_and_scope_pin(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)

    def fail_insert(_conn, _cursor, statement, *_args):
        if statement.lstrip().startswith("INSERT INTO channel_inbound_events"):
            raise OperationalError(statement, {}, RuntimeError("injected insert failure"))

    event.listen(engine, "before_cursor_execute", fail_insert)
    result = _stage(engine, "om_fail")
    event.remove(engine, "before_cursor_execute", fail_insert)

    assert result.disposition is StageDisposition.NACK
    assert result.should_ack is False
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        assert binding.provider_tenant_key is None
        assert binding.identity_scope_key is None
        assert db.exec(select(ChannelInboundEvent)).all() == []


def test_concurrent_stage_and_claim_each_have_one_winner(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _stage(engine, "om_race"), range(2)))
    assert sorted(result.disposition for result in results) == [
        StageDisposition.DUPLICATE,
        StageDisposition.STAGED,
    ]
    event_pk = next(result.event_pk for result in results if result.event_pk)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _index: claim_staged_inbound(event_pk, db_engine=engine),
                range(2),
            )
        )
    assert sorted(claims) == [False, True]
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT status FROM channel_inbound_events WHERE id=:id"),
            {"id": event_pk},
        ).scalar_one() == "processing"


def test_daemon_recovers_received_event_without_memory_notification(
    monkeypatch, tmp_path
) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_recover")
    calls = []

    class FakeAgentLoop:
        def __init__(self, db, *, event_sink=None):
            self.db = db

        def handle_turn(self, request):
            calls.append(request.client_turn_id)
            user_message = Message(
                id=new_id("msg"),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="user",
                content=request.message,
                metadata_json={"client_turn_id": request.client_turn_id},
            )
            self.db.add(user_message)
            self.db.flush()
            self.db.add(
                Message(
                    id=new_id("msg"),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    role="assistant",
                    content="reply",
                    metadata_json={"user_message_id": user_message.id},
                )
            )

    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)

    run_staged_inbound_daemon(once=True, db_engine=engine)
    run_staged_inbound_daemon(once=True, db_engine=engine)

    assert calls == ["om_recover"]
    with Session(engine) as db:
        row = db.get(ChannelInboundEvent, staged.event_pk)
        assert row.status == "done"
        assert row.processor_run_id
        assert len(db.exec(select(User)).all()) == 1
        assert len(db.exec(select(ChatSession)).all()) == 1


def test_group_event_creates_group_session_and_preserves_sender_context(
    monkeypatch,
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    inbound = ChannelInbound(
        channel="feishu",
        event_id="om_group",
        from_user_id="ou_sender",
        to_user_id="ou_bot_a",
        session_id="oc_group",
        group_id="oc_group",
        context_token="om_group",
        text="hello group",
        is_group=True,
        raw={"event": {"message": {"message_id": "om_group"}}},
        sender_name="Alice",
    )
    target = {
        "message_id": "om_group",
        "reply_in_thread": False,
        "receive_id_type": "chat_id",
        "receive_id": "oc_group",
    }
    staged = stage_feishu_inbound(
        db_engine=engine,
        binding_id="chan_feishu",
        expected_revision=7,
        event_app_id="cli_app_a",
        tenant_key="tenant_key_a",
        inbound=inbound,
        target=target,
    )
    requests = []

    class FakeAgentLoop:
        def __init__(self, db, *, event_sink=None):
            self.db = db

        def handle_turn(self, request):
            requests.append(request)

    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)

    run_staged_inbound_daemon(once=True, db_engine=engine)

    assert staged.disposition is StageDisposition.STAGED
    assert len(requests) == 1
    assert requests[0].message == "[发送者: Alice]\nhello group"
    with Session(engine) as db:
        chat_session = db.exec(select(ChatSession)).one()
        assert chat_session.external_conv_id == (
            "feishu_app:9:cli_app_a:tenant:12:tenant_key_a_group_oc_group"
        )
        assert chat_session.channel_target_json == target
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        assert event_row.status == "done"


def test_unknown_replay_envelope_is_failed_once(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    with Session(engine) as db:
        row = ChannelInboundEvent(
            tenant_id="tenant_a",
            binding_id="chan_feishu",
            channel="feishu",
            event_id="om_future",
            payload_json={"schema_version": 999},
            target_json={"message_id": "om_future"},
            status="received",
        )
        db.add(row)
        db.commit()
        event_pk = row.id

    run_staged_inbound_daemon(once=True, db_engine=engine)
    run_staged_inbound_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        row = db.get(ChannelInboundEvent, event_pk)
        assert row.status == "failed"
        assert row.error == "unsupported_envelope_version"


def test_daemon_does_not_claim_other_channel_received_rows(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    with Session(engine) as db:
        row = ChannelInboundEvent(
            tenant_id="tenant_a",
            binding_id="chan_wechat",
            channel="wechat",
            event_id="wx_received",
            payload_json={"legacy": True},
            target_json={},
            status="received",
        )
        db.add(row)
        db.commit()
        event_pk = row.id

    run_staged_inbound_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        row = db.get(ChannelInboundEvent, event_pk)
        assert row.status == "received"
        assert row.error is None


def test_feishu_command_stages_notice_with_immutable_target(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_help", text_value="/帮助")

    run_staged_inbound_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        deliveries = db.exec(select(ChannelDelivery)).all()
        assert event_row.status == "done"
        assert {row.kind for row in deliveries} == {"reaction_add", "notice"}
        notice = next(row for row in deliveries if row.kind == "notice")
        assert notice.target_json == {
            "message_id": "om_help",
            "reply_in_thread": False,
            "receive_id_type": "open_id",
            "receive_id": "ou_user_a",
            "reaction_final": True,
        }


def test_replay_account_change_is_rejected_before_identity_creation(tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_scope_change")
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_feishu")
        binding.provider_tenant_key = "different_tenant"
        db.add(binding)
        db.commit()

    run_staged_inbound_daemon(once=True, db_engine=engine)

    with Session(engine) as db:
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        assert event_row.status == "failed"
        assert event_row.error == "replay_account_mismatch"
        assert db.exec(select(User)).all() == []


def test_claimed_event_returns_to_received_after_pre_turn_failure(monkeypatch, tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_pre_turn_failure")

    def fail_identity(*_args, **_kwargs):
        raise RuntimeError("injected identity failure")

    monkeypatch.setattr(
        "app.channels.service_intake.resolve_or_provision_user",
        fail_identity,
    )
    with pytest.raises(RuntimeError, match="injected identity failure"):
        process_staged_inbound(staged.event_pk, db_engine=engine)

    with Session(engine) as db:
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        assert event_row.status == "received"
        assert event_row.processor_run_id is None


def test_startup_sweep_decodes_stale_feishu_processing_envelope(
    monkeypatch, tmp_path
) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_stale_processing")
    with Session(engine) as db:
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        event_row.status = "processing"
        event_row.processor_run_id = "old-process-generation"
        db.add(event_row)
        db.commit()
    calls = []

    class FakeAgentLoop:
        def __init__(self, db, *, event_sink=None):
            self.db = db

        def handle_turn(self, request):
            calls.append(request.client_turn_id)
            user_message = Message(
                id=new_id("msg"),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="user",
                content=request.message,
                metadata_json={"client_turn_id": request.client_turn_id},
            )
            self.db.add(user_message)
            self.db.flush()
            self.db.add(
                Message(
                    id=new_id("msg"),
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    role="assistant",
                    content="reply",
                    metadata_json={"user_message_id": user_message.id},
                )
            )

    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)
    assert sweep_stale_inbound_events(db_engine=engine) == 1
    assert calls == ["om_stale_processing"]
    with Session(engine) as db:
        event_row = db.exec(
            select(ChannelInboundEvent).where(
                ChannelInboundEvent.binding_id == "chan_feishu",
                ChannelInboundEvent.event_id == "om_stale_processing",
            )
        ).one()
        assert event_row.status == "done"
        assert event_row.id == staged.event_pk
        assert event_row.payload_json["schema_version"] == 1
        assert event_row.target_json == {
            "message_id": "om_stale_processing",
            "reply_in_thread": False,
            "receive_id_type": "open_id",
            "receive_id": "ou_user_a",
        }


def test_claim_is_released_when_claimed_loader_fails(monkeypatch, tmp_path) -> None:
    engine = _engine(tmp_path)
    _seed_binding(engine)
    staged = _stage(engine, "om_loader_failure")

    def fail_loader(*_args, **_kwargs):
        raise RuntimeError("injected claimed loader failure")

    monkeypatch.setattr(
        "app.channels.service_intake._process_claimed_staged_inbound",
        fail_loader,
    )
    with pytest.raises(RuntimeError, match="claimed loader failure"):
        process_staged_inbound(staged.event_pk, db_engine=engine)
    with Session(engine) as db:
        event_row = db.get(ChannelInboundEvent, staged.event_pk)
        assert event_row.status == "received"
        assert event_row.processor_run_id is None


def test_envelope_round_trips_attachments_as_dataclass() -> None:
    inbound = ChannelInbound(
        channel="feishu",
        event_id="om_att_1",
        from_user_id="ou_user_a",
        to_user_id="ou_bot_a",
        session_id="oc_chat_a",
        group_id="",
        context_token="om_message_a",
        text="",
        is_group=False,
        raw={"event": {"message": {"message_id": "om_att_1"}}},
        sender_name="User A",
        account_scope="",
        attachments=[
            ChannelInboundAttachment(
                media_id="img_v3_001",
                kind="image",
                filename="photo.png",
                content_type="image/png",
                size=1024,
                download_params={
                    "file_key": "img_v3_001",
                    "type": "image",
                    "message_id": "om_att_1",
                },
            ),
            ChannelInboundAttachment(
                media_id="file_v3_002",
                kind="file",
                filename="doc.pdf",
                content_type="application/pdf",
                size=2048,
                download_params={
                    "file_key": "file_v3_002",
                    "type": "file",
                    "message_id": "om_att_1",
                },
            ),
        ],
    )
    envelope = encode_replay_envelope(inbound, app_id="cli_app_a", tenant_key="tenant_key_a")
    decoded = decode_replay_envelope(envelope)
    assert len(decoded.attachments) == 2
    for att in decoded.attachments:
        assert isinstance(att, ChannelInboundAttachment)
    assert decoded.attachments[0].media_id == "img_v3_001"
    assert decoded.attachments[0].kind == "image"
    assert decoded.attachments[0].download_params["file_key"] == "img_v3_001"
    assert decoded.attachments[0].download_params["message_id"] == "om_att_1"
    assert decoded.attachments[1].media_id == "file_v3_002"
    assert decoded.attachments[1].kind == "file"
    assert decoded.attachments[1].filename == "doc.pdf"


def test_envelope_round_trips_empty_attachments() -> None:
    inbound = _inbound("om_no_att")
    assert inbound.attachments == []
    envelope = encode_replay_envelope(inbound, app_id="cli_app_a", tenant_key="tenant_key_a")
    decoded = decode_replay_envelope(envelope)
    assert decoded.attachments == []
