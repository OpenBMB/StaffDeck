import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.feedback import (
    get_feedback_session_detail,
    get_feedback_summary,
    list_feedback_sessions,
    reanalyze_feedback,
)
from app.api.sessions import (
    SESSION_LOG_EXPORT_SCHEMA,
    SessionLogExportRequest,
    export_session_log,
    export_session_logs,
    get_session_detail,
    list_sessions,
    reset_session,
)
from app.core.task_frame_store import TaskFrameStore
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    ExternalSessionBinding,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
    MessageFeedback,
    Tenant,
    User,
    utc_now,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> dict[str, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    admin = User(
        id="admin_user", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x"
    )
    owner = User(id="owner_user", tenant_id="tenant_demo", username="owner", password_hash="x")
    member = User(id="member_user", tenant_id="tenant_demo", username="member", password_hash="x")
    wechat_user = User(
        id="wechat_user_1",
        tenant_id="tenant_demo",
        username="wechat_u1",
        display_name="微信用户 ab12cd34",
        password_hash="x",
    )
    db.add_all([admin, owner, member, wechat_user])
    db.add(
        AgentProfile(
            id="agent_emp",
            tenant_id="tenant_demo",
            name="客服员工",
            metadata_json={"owner_user_id": owner.id},
        )
    )
    db.add(
        AgentProfile(
            id="agent_overall",
            tenant_id="tenant_demo",
            name="整体智能体",
            is_overall=True,
            metadata_json={},
        )
    )
    db.add_all(
        [
            ChatSession(
                id="session_owner",
                tenant_id="tenant_demo",
                user_id=owner.id,
                agent_id="agent_emp",
                title="owner 的会话",
            ),
            ChatSession(
                id="session_member",
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_emp",
                title="member 的会话",
            ),
            ChatSession(
                id="session_channel",
                tenant_id="tenant_demo",
                user_id=wechat_user.id,
                agent_id="agent_emp",
                channel="wechat",
                external_conv_id="wechat_p2p_u1",
                channel_binding_id="chan_1",
                title="渠道会话",
                active_skill_id="skill_x",
                slots_json={"step": "1"},
            ),
            ChatSession(
                id="session_overall",
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_overall",
                title="整体员工会话",
            ),
        ]
    )
    db.commit()
    return {"admin": admin, "owner": owner, "member": member, "wechat_user": wechat_user}


def test_agent_creator_sees_all_sessions_of_the_agent() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions(
            "tenant_demo", agent_id="agent_emp", current_user=users["owner"], db=db
        )
        session_ids = {row["id"] for row in rows}
        assert session_ids == {"session_owner", "session_member", "session_channel"}


def test_admin_sees_all_sessions_with_agent_id() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions(
            "tenant_demo", agent_id="agent_emp", current_user=users["admin"], db=db
        )
        assert {row["id"] for row in rows} == {"session_owner", "session_member", "session_channel"}


def test_member_only_sees_own_sessions() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions(
            "tenant_demo", agent_id="agent_emp", current_user=users["member"], db=db
        )
        assert [row["id"] for row in rows] == ["session_member"]

        # 无 agent_id 时 admin 也只看自己
        admin_rows = list_sessions("tenant_demo", agent_id=None, current_user=users["admin"], db=db)
        assert admin_rows == []


def test_enterprise_logs_keep_pilotdeck_sessions_visible() -> None:
    with _test_session() as db:
        users = _seed(db)
        db.add(
            ChatSession(
                id="session_pilotdeck_log",
                tenant_id="tenant_demo",
                user_id=users["member"].id,
                agent_id="agent_emp",
                title="PilotDeck 内部协作",
                channel="public_api",
            )
        )
        db.add(
            ExternalSessionBinding(
                tenant_id="tenant_demo",
                credential_id="credential_pilotdeck",
                agent_id="agent_emp",
                external_session_id="pilotdeck-log-room",
                session_id="session_pilotdeck_log",
                metadata_json={"channel": "pilotdeck_group_chat"},
            )
        )
        db.commit()

        rows = list_sessions(
            "tenant_demo", agent_id="agent_emp", current_user=users["member"], db=db
        )

        assert {row["id"] for row in rows} == {"session_member", "session_pilotdeck_log"}


def test_overall_agent_never_opens_to_non_admin() -> None:
    with _test_session() as db:
        users = _seed(db)
        # member 即使提供 agent_id 也仅见自己的整体员工会话
        member_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["member"], db=db
        )
        assert [row["id"] for row in member_rows] == ["session_overall"]
        # owner 不是整体员工创建者(is_overall 创建者永不匹配),没有自己的会话则为空
        owner_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["owner"], db=db
        )
        assert owner_rows == []
        # admin 可见全部整体员工会话
        admin_rows = list_sessions(
            "tenant_demo", agent_id="agent_overall", current_user=users["admin"], db=db
        )
        assert [row["id"] for row in admin_rows] == ["session_overall"]


def test_detail_allowed_for_owner_admin_and_agent_creator() -> None:
    with _test_session() as db:
        users = _seed(db)
        # 会话属主
        own = get_session_detail(
            "session_member", "tenant_demo", current_user=users["member"], db=db
        )
        assert own["session"]["id"] == "session_member"
        # admin
        by_admin = get_session_detail(
            "session_member", "tenant_demo", current_user=users["admin"], db=db
        )
        assert by_admin["session"]["id"] == "session_member"
        # agent 创建者查看渠道会话
        by_creator = get_session_detail(
            "session_channel", "tenant_demo", current_user=users["owner"], db=db
        )
        assert by_creator["session"]["id"] == "session_channel"


def test_detail_404_for_other_members() -> None:
    with _test_session() as db:
        users = _seed(db)
        with pytest.raises(HTTPException) as exc_info:
            get_session_detail(
                "session_channel", "tenant_demo", current_user=users["member"], db=db
            )
        assert exc_info.value.status_code == 404
        with pytest.raises(HTTPException) as exc_info:
            get_session_detail("session_owner", "tenant_demo", current_user=users["member"], db=db)
        assert exc_info.value.status_code == 404


def test_single_session_json_export_contains_complete_log_envelope() -> None:
    with _test_session() as db:
        users = _seed(db)
        db.add_all(
            [
                Message(
                    id="message_user",
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    role="user",
                    content="查询报销制度",
                ),
                Message(
                    id="message_assistant",
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    role="assistant",
                    content="这是制度答复",
                ),
                MessageFeedback(
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    message_id="message_assistant",
                    user_id=users["owner"].id,
                    rating="up",
                ),
                AgentEvent(
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    event_type="task.completed",
                    payload_json={"task_id": "task_export"},
                ),
                HarnessInvocationRecord(
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    task_id="task_export",
                    run_id="run_export",
                    call_id="call_export",
                    tool_name="expense.lookup",
                    request_digest="sha256:test",
                    status="completed",
                    arguments_json={"month": "2026-08"},
                    result_json={"success": True, "data": {"remaining": 1200}},
                ),
            ]
        )
        db.commit()
        response = export_session_log(
            "session_channel",
            "tenant_demo",
            current_user=users["owner"],
            db=db,
        )

        payload = json.loads(response.body)
        assert response.media_type == "application/json"
        assert response.headers["content-disposition"] == (
            'attachment; filename="staffdeck-conversation-log-session_channel.json"'
        )
        assert payload["schema_version"] == SESSION_LOG_EXPORT_SCHEMA
        assert payload["item"]["session"]["id"] == "session_channel"
        assert [message["content"] for message in payload["item"]["messages"]] == [
            "查询报销制度",
            "这是制度答复",
        ]
        assert payload["item"]["feedback"][0]["rating"] == "up"
        assert payload["item"]["events"][0]["payload"] == {"task_id": "task_export"}
        assert payload["item"]["tool_invocations"][0]["tool_name"] == "expense.lookup"
        assert payload["item"]["tool_invocations"][0]["result"] == {
            "success": True,
            "data": {"remaining": 1200},
        }
        assert "traces" in payload["item"]


def test_batch_json_export_preserves_order_deduplicates_and_checks_visibility() -> None:
    with _test_session() as db:
        users = _seed(db)
        response = export_session_logs(
            SessionLogExportRequest(
                session_ids=["session_channel", "session_owner", "session_channel"]
            ),
            "tenant_demo",
            current_user=users["owner"],
            db=db,
        )

        payload = json.loads(response.body)
        assert payload["schema_version"] == SESSION_LOG_EXPORT_SCHEMA
        assert payload["count"] == 2
        assert [item["session"]["id"] for item in payload["items"]] == [
            "session_channel",
            "session_owner",
        ]

        with pytest.raises(HTTPException) as exc_info:
            export_session_logs(
                SessionLogExportRequest(session_ids=["session_member", "session_channel"]),
                "tenant_demo",
                current_user=users["member"],
                db=db,
            )
        assert exc_info.value.status_code == 404


def test_reset_allowed_for_agent_creator_and_admin_only() -> None:
    with _test_session() as db:
        users = _seed(db)
        with pytest.raises(HTTPException) as exc_info:
            reset_session("session_channel", "tenant_demo", current_user=users["member"], db=db)
        assert exc_info.value.status_code == 404

        # agent 创建者可重置渠道会话
        payload = reset_session(
            "session_channel", "tenant_demo", current_user=users["owner"], db=db
        )
        assert payload["id"] == "session_channel"
        assert payload["active_skill_id"] is None
        row = db.get(ChatSession, "session_channel")
        assert row.slots_json == {}
        assert row.status == "active"

        # admin 也可重置
        admin_payload = reset_session(
            "session_member", "tenant_demo", current_user=users["admin"], db=db
        )
        assert admin_payload["id"] == "session_member"


def test_reset_clears_harness_execution_state_and_preserves_turn_receipt() -> None:
    with _test_session() as db:
        users = _seed(db)
        row = db.get(ChatSession, "session_channel")
        assert row is not None
        row.context_state_json = {"harness_v2": {"active_task_frame_id": "task_old"}}
        row.awaiting_input_json = {"task_id": "task_old"}
        frame = HarnessTaskFrameRecord(
            tenant_id=row.tenant_id,
            session_id=row.id,
            source_turn_id="turn_old",
            task_id="task_old",
            status="queued",
        )
        db.add(frame)
        db.flush()
        db.add(
            HarnessRunRecord(
                tenant_id=row.tenant_id,
                session_id=row.id,
                task_frame_record_id=frame.id,
                task_id=frame.task_id,
                source_turn_id=frame.source_turn_id,
                status="running",
            )
        )
        db.add(
            HarnessSessionLeaseRecord(
                tenant_id=row.tenant_id,
                session_id=row.id,
                lease_owner="worker_old",
                lease_expires_at=utc_now(),
            )
        )
        db.add(
            HarnessTurnRecord(
                tenant_id=row.tenant_id,
                session_id=row.id,
                client_turn_id="turn_old",
                request_digest="digest",
                lease_owner="turn_worker_old",
                lease_expires_at=utc_now(),
            )
        )
        db.commit()

        reset_session(
            "session_channel",
            "tenant_demo",
            current_user=users["owner"],
            db=db,
        )

        db.refresh(row)
        assert row.context_state_json == {}
        assert row.awaiting_input_json is None
        assert TaskFrameStore(db).planner_state(row) == []
        assert db.exec(select(HarnessRunRecord)).all() == []
        assert db.exec(select(HarnessSessionLeaseRecord)).all() == []
        receipt = db.exec(
            select(HarnessTurnRecord).where(
                HarnessTurnRecord.session_id == row.id,
                HarnessTurnRecord.client_turn_id == "turn_old",
            )
        ).one()
        assert receipt.status == "cancelled"


def test_augment_fields_channel_and_identity() -> None:
    with _test_session() as db:
        users = _seed(db)
        rows = list_sessions(
            "tenant_demo", agent_id="agent_emp", current_user=users["owner"], db=db
        )
        by_id = {row["id"]: row for row in rows}

        channel_row = by_id["session_channel"]
        assert channel_row["channel"] == "wechat"
        assert channel_row["session_username"] == "wechat_u1"
        assert channel_row["session_display_name"] == "微信用户 ab12cd34"

        web_row = by_id["session_member"]
        assert web_row["channel"] is None
        assert web_row["session_username"] == "member"
        assert web_row["session_display_name"] is None

        # detail 同样带 augment 字段
        detail = get_session_detail(
            "session_channel", "tenant_demo", current_user=users["admin"], db=db
        )
        assert detail["session"]["channel"] == "wechat"
        assert detail["session"]["session_display_name"] == "微信用户 ab12cd34"


def test_feedback_scope_matches_agent_session_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        users = _seed(db)
        db.add_all(
            [
                Message(
                    id="msg_member_assistant",
                    tenant_id="tenant_demo",
                    session_id="session_member",
                    role="assistant",
                    content="member answer",
                ),
                Message(
                    id="msg_channel_assistant",
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    role="assistant",
                    content="channel answer",
                ),
                Message(
                    id="msg_overall_assistant",
                    tenant_id="tenant_demo",
                    session_id="session_overall",
                    role="assistant",
                    content="overall answer",
                ),
                MessageFeedback(
                    id="feedback_member",
                    tenant_id="tenant_demo",
                    session_id="session_member",
                    message_id="msg_member_assistant",
                    user_id=users["member"].id,
                    rating="down",
                ),
                MessageFeedback(
                    id="feedback_channel",
                    tenant_id="tenant_demo",
                    session_id="session_channel",
                    message_id="msg_channel_assistant",
                    user_id=users["wechat_user"].id,
                    rating="down",
                ),
                MessageFeedback(
                    id="feedback_overall",
                    tenant_id="tenant_demo",
                    session_id="session_overall",
                    message_id="msg_overall_assistant",
                    user_id=users["member"].id,
                    rating="down",
                ),
            ]
        )
        db.commit()

        creator_rows = list_feedback_sessions(
            "tenant_demo",
            "down",
            agent_id="agent_emp",
            limit=200,
            current_user=users["owner"],
            db=db,
        )
        assert {row["session_id"] for row in creator_rows} == {
            "session_member",
            "session_channel",
        }

        member_rows = list_feedback_sessions(
            "tenant_demo",
            "down",
            agent_id="agent_emp",
            limit=200,
            current_user=users["member"],
            db=db,
        )
        assert [row["session_id"] for row in member_rows] == ["session_member"]
        assert (
            get_feedback_session_detail(
                "session_channel",
                "tenant_demo",
                current_user=users["owner"],
                db=db,
            )["session"]["id"]
            == "session_channel"
        )

        with pytest.raises(HTTPException) as member_error:
            get_feedback_session_detail(
                "session_channel",
                "tenant_demo",
                current_user=users["member"],
                db=db,
            )
        assert member_error.value.status_code == 404
        with pytest.raises(HTTPException) as overall_error:
            get_feedback_session_detail(
                "session_overall",
                "tenant_demo",
                current_user=users["owner"],
                db=db,
            )
        assert overall_error.value.status_code == 404
        with pytest.raises(HTTPException) as tenant_error:
            get_feedback_summary(
                "tenant_other",
                agent_id="agent_emp",
                current_user=users["owner"],
                db=db,
            )
        assert tenant_error.value.status_code == 403

        monkeypatch.setattr(
            "app.api.feedback.enqueue_feedback_analysis",
            lambda *_args, **_kwargs: SimpleNamespace(id="job_feedback"),
        )
        result = reanalyze_feedback(
            "feedback_channel",
            "tenant_demo",
            current_user=users["owner"],
            db=db,
        )
        assert result["analysis_status"] == "pending"
        assert result["job_id"] == "job_feedback"
