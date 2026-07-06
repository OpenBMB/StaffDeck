import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.api.chat as chat_api
from app.core.agent_loop import AgentLoop
from app.db.models import AgentEvent, ChatSession, HumanHandoffRequest, Message, Skill, Tenant, User, utc_now
from app.session.slot_policy import strip_router_generated_message_slots
from app.session.session_schema import ChatTurnRequest, RouterDecision, StepAgentResult


class FakeEvents:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict) -> None:
        self.records.append((tenant_id, session_id, event_type, payload))


class FakeExecResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[object]:
        return self.rows


class FakeDb:
    def __init__(
        self,
        exec_results: list[list[object]] | None = None,
        get_rows: dict[tuple[type[object], str], object] | None = None,
    ) -> None:
        self.exec_results = list(exec_results or [])
        self.get_rows = get_rows or {}
        self.added: list[object] = []
        self.commits = 0
        self.refreshed: list[object] = []

    def exec(self, _statement: object) -> FakeExecResult:
        if self.exec_results:
            return FakeExecResult(self.exec_results.pop(0))
        return FakeExecResult()

    def get(self, model: type[object], row_id: str) -> object | None:
        return self.get_rows.get((model, row_id))

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, row: object) -> None:
        self.refreshed.append(row)


def _handoff_skill(step: dict | None = None) -> Skill:
    node = step or {
        "node_id": "manual_review",
        "type": "handoff",
        "name": "人工复核",
        "allowed_actions": ["handoff_human"],
        "handoff_question": "请人工确认后继续处理。",
    }
    return Skill(
        tenant_id="tenant_demo",
        skill_id="manual_skill",
        name="人工复核流程",
        status="published",
        content_json={
            "nodes": [node],
            "edges": [],
            "start_node_id": node["node_id"],
            "terminal_node_ids": [node["node_id"]],
        },
    )


def _handoff_session() -> ChatSession:
    return ChatSession(
        id="session_handoff",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        active_skill_id="manual_skill",
        active_step_id="manual_review",
        slots_json={"order_id": "A001"},
        pending_tasks_json=[{"id": "task_next"}],
        skill_stack_json=[{"skill_id": "manual_skill"}],
    )


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_handoff_users(db: Session) -> tuple[User, User, User]:
    admin = User(id="admin_user", tenant_id="tenant_demo", username="admin", password_hash="x")
    user = User(id="user_demo", tenant_id="tenant_demo", username="user_demo", password_hash="x")
    other = User(id="other_user", tenant_id="tenant_demo", username="other", password_hash="x")
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(admin)
    db.add(user)
    db.add(other)
    db.commit()
    return admin, user, other


def test_handoff_requires_structured_step_declaration():
    loop = AgentLoop.__new__(AgentLoop)

    assert loop._step_declares_human_handoff({"allowed_actions": ["answer_user", "handoff_human"]})
    assert loop._step_declares_human_handoff({"type": "handoff"})
    assert loop._step_declares_human_handoff({"handoff": {"enabled": True}})

    assert not loop._step_declares_human_handoff({"description": "用户要求转人工时请转人工"})
    assert not loop._step_declares_human_handoff({"name": "转人工确认"})
    assert not loop._step_declares_human_handoff({"allowed_actions": ["answer_user", "continue_flow"]})


def test_handoff_finalize_creates_pending_request_for_declared_step():
    loop = AgentLoop.__new__(AgentLoop)
    db = FakeDb(
        exec_results=[
            [],  # no existing pending handoff
            [],  # no agent owner metadata, fall back to requester
            [
                Message(
                    id="msg_user",
                    tenant_id="tenant_demo",
                    session_id="session_handoff",
                    role="user",
                    content="我要转人工处理订单 A001",
                )
            ],
        ]
    )
    loop.db = db
    loop.events = FakeEvents()
    loop._should_complete_skill = lambda *_args, **_kwargs: False
    session = _handoff_session()

    state = loop._finalize_execution_after_reply(
        "tenant_demo",
        session,
        _handoff_skill(),
        RouterDecision(decision="continue_current_skill"),
        StepAgentResult(reply="需要人工复核订单 A001", handoff=True),
        None,
    )

    assert state == "handoff"
    assert session.status == "handoff"
    assert session.awaiting_input_json
    assert session.awaiting_input_json["type"] == "human_handoff"
    handoffs = [row for row in db.added if isinstance(row, HumanHandoffRequest)]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert handoff.status == "pending"
    assert handoff.assignee_user_id == "user_demo"
    assert handoff.trigger_skill_id == "manual_skill"
    assert handoff.trigger_step_id == "manual_review"
    assert handoff.resume_payload_json["slots"] == {"order_id": "A001"}
    assert handoff.pending_question == "需要人工复核订单 A001"
    assert session.awaiting_input_json["handoff_id"] == handoff.id
    assert [record[2] for record in loop.events.records] == ["human_handoff_requested"]


def test_handoff_finalize_reuses_existing_pending_request():
    loop = AgentLoop.__new__(AgentLoop)
    existing = HumanHandoffRequest(
        id="handoff_existing",
        tenant_id="tenant_demo",
        session_id="session_handoff",
        pending_question="之前已经创建的人工请求",
    )
    loop.db = FakeDb(exec_results=[[existing]])
    loop.events = FakeEvents()
    session = _handoff_session()

    handoff = loop._create_human_handoff_request(
        "tenant_demo",
        session,
        _handoff_skill(),
        StepAgentResult(reply="重复触发", handoff=True),
    )

    assert handoff is existing
    assert session.status == "handoff"
    assert session.awaiting_input_json == {
        "type": "human_handoff",
        "handoff_id": "handoff_existing",
        "pending_question": "之前已经创建的人工请求",
    }
    assert not loop.db.added
    assert loop.events.records == []


def test_handoff_request_is_ignored_when_step_does_not_declare_handoff():
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = FakeDb()
    loop.events = FakeEvents()
    loop._should_complete_skill = lambda *_args, **_kwargs: False
    session = _handoff_session()
    skill = _handoff_skill(
        {
            "node_id": "manual_review",
            "name": "转人工确认",
            "description": "用户要求转人工时请转人工",
            "allowed_actions": ["answer_user", "continue_flow"],
        }
    )

    state = loop._finalize_execution_after_reply(
        "tenant_demo",
        session,
        skill,
        RouterDecision(decision="handoff_human"),
        StepAgentResult(reply="模型建议转人工", handoff=True),
        None,
    )

    assert state == "continued"
    assert session.status == "active"
    assert session.awaiting_input_json is None
    assert loop.db.added == []
    assert [record[2] for record in loop.events.records] == ["human_handoff_ignored"]
    assert loop.events.records[0][3]["reason"] == "current_step_does_not_declare_handoff"


def test_handoff_list_filters_by_status_and_user_then_reply_restores_session(monkeypatch):
    engine = _test_engine()
    with Session(engine) as db:
        admin, user, other = _seed_handoff_users(db)
        session = ChatSession(
            id="session_handoff",
            tenant_id="tenant_demo",
            user_id=user.id,
            agent_id="agent_demo",
            status="handoff",
            awaiting_input_json={"type": "human_handoff", "handoff_id": "handoff_assigned"},
        )
        db.add(session)
        db.add_all(
            [
                HumanHandoffRequest(
                    id="handoff_assigned",
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    agent_id="agent_demo",
                    requester_user_id=other.id,
                    assignee_user_id=user.id,
                    pending_question="请 user_demo 处理",
                    status="pending",
                    updated_at=utc_now(),
                ),
                HumanHandoffRequest(
                    id="handoff_requested",
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    agent_id="agent_demo",
                    requester_user_id=user.id,
                    assignee_user_id=other.id,
                    pending_question="user_demo 发起的请求",
                    status="pending",
                    updated_at=utc_now(),
                ),
                HumanHandoffRequest(
                    id="handoff_other",
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    agent_id="agent_demo",
                    requester_user_id=other.id,
                    assignee_user_id=other.id,
                    pending_question="其他人的请求",
                    status="pending",
                    updated_at=utc_now(),
                ),
                HumanHandoffRequest(
                    id="handoff_answered",
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    agent_id="agent_demo",
                    requester_user_id=user.id,
                    assignee_user_id=user.id,
                    pending_question="已经处理",
                    status="answered",
                    human_reply="已完成",
                    updated_at=utc_now(),
                ),
            ]
        )
        db.commit()

        admin_rows = chat_api.list_human_handoffs("tenant_demo", "pending", current_user=admin, db=db)
        assert {row.id for row in admin_rows} == {"handoff_assigned", "handoff_requested", "handoff_other"}

        user_rows = chat_api.list_human_handoffs("tenant_demo", "pending", current_user=user, db=db)
        assert {row.id for row in user_rows} == {"handoff_assigned", "handoff_requested"}

        user_all_rows = chat_api.list_human_handoffs("tenant_demo", "all", current_user=user, db=db)
        assert {row.id for row in user_all_rows} == {"handoff_assigned", "handoff_requested", "handoff_answered"}

        resumed: list[str] = []
        monkeypatch.setattr(chat_api, "_resume_human_handoff_async", resumed.append)
        result = chat_api.reply_human_handoff(
            "handoff_assigned",
            chat_api.HumanHandoffReplyRequest(tenant_id="tenant_demo", reply="人工已经确认，继续执行"),
            current_user=user,
            db=db,
        )

        assert result.status == "answered"
        assert result.human_reply == "人工已经确认，继续执行"
        stored_handoff = db.get(HumanHandoffRequest, "handoff_assigned")
        stored_session = db.get(ChatSession, "session_handoff")
        assert stored_handoff is not None
        assert stored_handoff.resume_payload_json["answered_by_user_id"] == user.id
        assert stored_session is not None
        assert stored_session.status == "active"
        assert stored_session.awaiting_input_json is None
        assert stored_session.summary == "最近回复：人工已经确认，继续执行"
        events = db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_answered")).all()
        assert len(events) == 1
        assert events[0].payload_json["handoff_id"] == "handoff_assigned"
        assert resumed == ["handoff_assigned"]


def test_reply_human_handoff_restores_session_and_schedules_resume(monkeypatch):
    handoff = HumanHandoffRequest(
        id="handoff_reply",
        tenant_id="tenant_demo",
        session_id="session_handoff",
        agent_id="agent_demo",
        requester_user_id="user_demo",
        assignee_user_id="admin_user",
        trigger_skill_id="manual_skill",
        trigger_step_id="manual_review",
        context_summary="user: 请人工处理",
        pending_question="请人工确认",
        status="pending",
    )
    session = ChatSession(
        id="session_handoff",
        tenant_id="tenant_demo",
        user_id="user_demo",
        agent_id="agent_demo",
        status="handoff",
        awaiting_input_json={"type": "human_handoff", "handoff_id": "handoff_reply"},
    )
    db = FakeDb(
        get_rows={
            (HumanHandoffRequest, "handoff_reply"): handoff,
            (ChatSession, "session_handoff"): session,
        }
    )
    resumed: list[str] = []
    monkeypatch.setattr(chat_api, "_resume_human_handoff_async", resumed.append)

    result = chat_api.reply_human_handoff(
        "handoff_reply",
        chat_api.HumanHandoffReplyRequest(tenant_id="tenant_demo", reply="人工确认通过，继续执行"),
        current_user=User(
            id="admin_user",
            tenant_id="tenant_demo",
            username="admin",
            password_hash="x",
        ),
        db=db,
    )

    assert result.status == "answered"
    assert result.human_reply == "人工确认通过，继续执行"
    assert handoff.status == "answered"
    assert handoff.human_reply == "人工确认通过，继续执行"
    assert handoff.resume_payload_json["answered_by_user_id"] == "admin_user"
    assert session.status == "active"
    assert session.awaiting_input_json is None
    assert session.summary == "最近回复：人工确认通过，继续执行"
    assert any(isinstance(row, AgentEvent) and row.event_type == "human_handoff_answered" for row in db.added)
    assert db.commits == 1
    assert resumed == ["handoff_reply"]


def test_reply_human_handoff_rejects_non_pending_request(monkeypatch):
    handoff = HumanHandoffRequest(
        id="handoff_done",
        tenant_id="tenant_demo",
        session_id="session_handoff",
        status="answered",
        human_reply="已处理",
    )
    db = FakeDb(get_rows={(HumanHandoffRequest, "handoff_done"): handoff})
    monkeypatch.setattr(chat_api, "_resume_human_handoff_async", lambda _handoff_id: None)

    with pytest.raises(HTTPException) as exc:
        chat_api.reply_human_handoff(
            "handoff_done",
            chat_api.HumanHandoffReplyRequest(tenant_id="tenant_demo", reply="再次回复"),
            current_user=User(
                id="admin_user",
                tenant_id="tenant_demo",
                username="admin",
                password_hash="x",
            ),
            db=db,
        )

    assert exc.value.status_code == 409
    assert db.commits == 0


def test_handoff_resume_worker_continues_original_session_once(monkeypatch):
    engine = _test_engine()
    handled_requests: list[ChatTurnRequest] = []

    class FakeAgentLoop:
        def __init__(self, db: Session) -> None:
            self.db = db

        def handle_turn(self, request: ChatTurnRequest) -> None:
            handled_requests.append(request)

    monkeypatch.setattr(chat_api, "engine", engine)
    monkeypatch.setattr(chat_api, "AgentLoop", FakeAgentLoop)
    with Session(engine) as db:
        _admin, user, _other = _seed_handoff_users(db)
        db.add(
            ChatSession(
                id="session_handoff",
                tenant_id="tenant_demo",
                user_id=user.id,
                agent_id="agent_demo",
                status="active",
            )
        )
        db.add(
            HumanHandoffRequest(
                id="handoff_worker",
                tenant_id="tenant_demo",
                session_id="session_handoff",
                agent_id="agent_demo",
                requester_user_id=user.id,
                assignee_user_id="admin_user",
                trigger_skill_id="manual_skill",
                trigger_step_id="manual_review",
                pending_question="请人工确认",
                status="answered",
                human_reply="人工答复：继续执行后续流程",
            )
        )
        db.commit()

    chat_api._resume_human_handoff_worker("handoff_worker")
    chat_api._resume_human_handoff_worker("handoff_worker")

    assert len(handled_requests) == 1
    request = handled_requests[0]
    assert request.tenant_id == "tenant_demo"
    assert request.session_id == "session_handoff"
    assert request.agent_id == "agent_demo"
    assert request.user_id == "user_demo"
    assert request.message == "人工答复：继续执行后续流程"
    assert request.channel == "human_handoff_resume"

    with Session(engine) as db:
        handoff = db.get(HumanHandoffRequest, "handoff_worker")
        assert handoff is not None
        assert handoff.status == "answered"
        assert handoff.metadata_json["resume_started_at"]
        assert handoff.metadata_json["resume_finished_at"]
        events = db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_resume_started")).all()
        assert len(events) == 1
        assert events[0].payload_json["handoff_id"] == "handoff_worker"


def test_router_generated_message_slots_are_not_persisted():
    cleaned = strip_router_generated_message_slots(
        {
            "message_content": "模型改写后的用户消息",
            "user_message": "另一个改写版本",
            "current_message": "当前输入摘要",
            "product_id": "A1",
            "quantity": 1,
        }
    )

    assert cleaned == {"product_id": "A1", "quantity": 1}
