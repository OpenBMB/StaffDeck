from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from test_teams_api import (
    _admin_user,
    _make_task,
    _member_user,
    _seed_team,
    _stub_start_wakeup,
    _test_session,
)
from test_teams_bidding import (
    _award_reply,
    _bid_reply,
    _make_pool_task,
    _pending_wakes,
    _run_wake,
    _score_reply,
    _seed_pool_team,
)

from app.api import chat as chat_api
from app.api import teams as teams_api
from app.core import AgentLoop, harness_v2_engine
from app.core.harness_v2_engine import HarnessV2Engine
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    Message,
    TeamTask,
    TeamWakeEvent,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic
from app.teams import wakeup
from app.teams.schema import TeamTLChatRequest
from app.teams.service import add_member, create_team
from app.teams.wakeup import claim_wake_event, enqueue_wake_event


def _make_session(
    db: Session,
    *,
    session_id: str,
    team_id: str | None,
    agent_id: str,
    title: str,
    created_at: datetime,
) -> ChatSession:
    session = ChatSession(
        id=session_id,
        tenant_id="tenant_demo",
        user_id="user_admin",
        agent_id=agent_id,
        title=title,
        status="active",
        team_id=team_id,
        created_at=created_at,
    )
    db.add(session)
    return session


def _make_message(
    db: Session,
    *,
    session_id: str,
    message_id: str,
    role: str,
    content: str,
    created_at: datetime,
    metadata: dict | None = None,
) -> Message:
    row = Message(
        id=message_id,
        tenant_id="tenant_demo",
        session_id=session_id,
        role=role,
        content=content,
        metadata_json=dict(metadata or {}),
        created_at=created_at,
    )
    db.add(row)
    return row


# ---------- 四类团队会话 team_id 落库 ----------


def test_member_task_and_tl_review_sessions_carry_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """成员任务执行与 TL 验收创建的会话都绑定 team_id。"""
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        _stub_start_wakeup(monkeypatch)

        def fake_turn(*args, **kw):
            if kw["agent"].id == "agent_tl":
                return '验收通过\n```json\n{"team_review": {"verdict": "approve", "comment": "通过"}}\n```'
            return "执行报告:已完成"

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *a, **kw: "completed")
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        assert claim_wake_event(db, wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        review_wake = db.exec(
            select(TeamWakeEvent).where(
                TeamWakeEvent.trigger_type == "task_report",
                TeamWakeEvent.status == "pending",
            )
        ).first()
        assert review_wake is not None
        assert claim_wake_event(db, review_wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, review_wake.id))

        sessions = db.exec(select(ChatSession).where(ChatSession.team_id == team.id)).all()
        titles = {item.title for item in sessions}
        assert f"团队任务:{task.title}" in titles
        assert f"团队任务验收:{task.title}" in titles
        db.refresh(task)
        assert task.status == "done"


def test_bid_request_and_award_sessions_carry_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """竞标陈述会话与 TL 裁决会话都绑定 team_id(辩论关闭,陈述后直接裁决)。"""
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 0})
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)

        def fake_turn(*args, **kw):
            agent_id = kw["agent"].id
            if agent_id == "agent_tl":
                return _award_reply("agent_a")
            return _bid_reply(f"{agent_id} 的方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)
        wakeup.start_bidding(db, team, task)
        wakes = _pending_wakes(db, "bid_request")
        assert len(wakes) == 2
        for wake in wakes:
            _run_wake(db, wake.id)
        bid_sessions = db.exec(
            select(ChatSession).where(ChatSession.title.like("团队竞标:%"))
        ).all()
        assert len(bid_sessions) == 2
        assert all(item.team_id == team.id for item in bid_sessions)

        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "award"
        _run_wake(db, judge[0].id)
        award_sessions = db.exec(
            select(ChatSession).where(ChatSession.title.like("团队竞标裁决:%"))
        ).all()
        assert len(award_sessions) == 1
        assert award_sessions[0].team_id == team.id


def test_bid_score_session_carries_team_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 3 轮血条赛制下,TL 每轮打分会话也绑定 team_id。"""
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)

        def fake_turn(*args, **kw):
            agent_id = kw["agent"].id
            if agent_id == "agent_tl":
                return _score_reply(("agent_a", 9.0), ("agent_b", 8.0))
            return _bid_reply(f"{agent_id} 的方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)
        wakeup.start_bidding(db, team, task)
        for wake in _pending_wakes(db, "bid_request"):
            _run_wake(db, wake.id)
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "score"
        _run_wake(db, judge[0].id)
        score_sessions = db.exec(
            select(ChatSession).where(ChatSession.title.like("团队竞标打分:%"))
        ).all()
        assert len(score_sessions) == 1
        assert score_sessions[0].team_id == team.id


# ---------- 团队会话列表端点 ----------


def test_conversations_endpoint_kinds_preview_order_isolation() -> None:
    """kind 分类、preview 截取 80 字、updated_at 倒序;两个团队交叉验证不串。"""
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        s_tl = _make_session(
            db, session_id="sess_tl", team_id=team.id, agent_id="agent_tl",
            title=f"团队 {team.name} · TL 对话", created_at=base,
        )
        s_task = _make_session(
            db, session_id="sess_task", team_id=team.id, agent_id="agent_worker",
            title="团队任务:写方案", created_at=base,
        )
        s_review = _make_session(
            db, session_id="sess_review", team_id=team.id, agent_id="agent_tl",
            title="团队任务验收:写方案", created_at=base + timedelta(minutes=1),
        )
        s_bid = _make_session(
            db, session_id="sess_bid", team_id=team.id, agent_id="agent_worker",
            title="团队竞标:写方案", created_at=base,
        )
        # member_task 会话由 task.session_id 反向关联任务
        task = TeamTask(
            team_id=team.id,
            tenant_id="tenant_demo",
            title="写方案",
            status="in_progress",
            created_by_user_id="user_admin",
            created_by_tl=True,
            assignee_agent_id="agent_worker",
            session_id=s_task.id,
        )
        db.add(task)
        # 第二团队:交叉验证物理隔离
        team2 = create_team(
            db, tenant_id="tenant_demo", name="另一个团队",
            description=None, owner_user_id="user_admin",
        )
        add_member(db, team2, agent_id="agent_worker2", role="leader")
        s_other = _make_session(
            db, session_id="sess_other", team_id=team2.id, agent_id="agent_worker2",
            title="团队任务:别的任务", created_at=base + timedelta(minutes=10),
        )
        _make_message(
            db, session_id=s_other.id, message_id="msg_other",
            role="assistant", content="外团队消息", created_at=base + timedelta(minutes=10),
        )
        _make_message(
            db, session_id=s_tl.id, message_id="msg_tl", role="user",
            content="你好 TL", created_at=base + timedelta(minutes=4),
        )
        _make_message(
            db, session_id=s_task.id, message_id="msg_task", role="assistant",
            content="执" * 100, created_at=base + timedelta(minutes=2),
        )
        _make_message(
            db, session_id=s_bid.id, message_id="msg_bid", role="assistant",
            content="我的竞标方案", created_at=base + timedelta(minutes=3),
        )
        db.commit()

        # 本租户普通成员可读(对齐 GET tasks)
        response = teams_api.list_team_conversations(team.id, "tenant_demo", db, _member_user())
        assert response.team_id == team.id
        assert response.team_name == team.name
        assert response.tl is not None
        assert response.tl.agent_id == "agent_tl"
        assert response.tl.agent_name == "TL"
        assert response.tl.session_id == s_tl.id

        by_id = {item.session_id: item for item in response.conversations}
        # 只含本团队会话,不串 team2
        assert set(by_id) == {s_tl.id, s_task.id, s_review.id, s_bid.id}
        assert by_id[s_tl.id].kind == "tl_chat"
        assert by_id[s_task.id].kind == "member_task"
        assert by_id[s_review.id].kind == "tl_review"
        assert by_id[s_bid.id].kind == "member_bid"
        assert by_id[s_tl.id].agent_name == "TL"
        assert by_id[s_task.id].agent_name == "Worker"
        assert by_id[s_task.id].task_id == task.id
        assert by_id[s_task.id].task_status == "in_progress"
        assert by_id[s_task.id].needs_input is False
        assert by_id[s_task.id].pending_question is None
        assert by_id[s_bid.id].task_id is None
        assert by_id[s_task.id].created_at == base
        # preview 为末条消息截取 80 字;无消息则空串
        assert by_id[s_task.id].preview == "执" * 80
        assert by_id[s_review.id].preview == ""
        # 按 updated_at(末条消息时间或会话创建时间)倒序
        assert [item.session_id for item in response.conversations] == [
            s_tl.id,
            s_bid.id,
            s_task.id,
            s_review.id,
        ]

        # team2 视角:只有自己的会话;有 TL 但无 TL 会话时 session_id 为 None
        other = teams_api.list_team_conversations(team2.id, "tenant_demo", db, _member_user())
        assert [item.session_id for item in other.conversations] == [s_other.id]
        assert other.tl is not None
        assert other.tl.agent_id == "agent_worker2"
        assert other.tl.session_id is None


def test_conversations_expose_member_question_waiting_for_user_input() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        session = _make_session(
            db,
            session_id="sess_needs_input",
            team_id=team.id,
            agent_id="agent_worker",
            title="团队任务:采购物品",
            created_at=base,
        )
        task = TeamTask(
            team_id=team.id,
            tenant_id="tenant_demo",
            title="采购物品",
            status="escalated",
            created_by_user_id="user_admin",
            created_by_tl=True,
            assignee_agent_id="agent_worker",
            session_id=session.id,
            report_json={
                "needs_input": True,
                "full_reply": "请提供员工工号和物品清单。",
            },
        )
        db.add(task)
        _make_message(
            db,
            session_id=session.id,
            message_id="msg_needs_input_prompt",
            role="user",
            content="你是团队成员，请完成以下团队任务。任务标题:采购物品",
            created_at=base + timedelta(seconds=1),
        )
        db.commit()

        response = teams_api.list_team_conversations(
            team.id, "tenant_demo", db, _member_user()
        )
        row = next(item for item in response.conversations if item.session_id == session.id)

        assert row.task_status == "escalated"
        assert row.needs_input is True
        assert row.pending_question == "请提供员工工号和物品清单。"
        # 注入给成员的任务提示不是成员回复，不能出现在群聊摘要中。
        assert row.preview == ""


def test_conversation_stream_returns_incremental_member_reply() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        session = _make_session(
            db,
            session_id="sess_streaming_member",
            team_id=team.id,
            agent_id="agent_worker",
            title="团队任务:实时整理",
            created_at=base,
        )
        db.add_all(
            [
                AgentEvent(
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    event_type="user_message_received",
                    payload_json={"message_id": "turn_stream", "turn_id": "turn_stream"},
                    created_at=base + timedelta(seconds=1),
                ),
                AgentEvent(
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    event_type="stream_delta",
                    payload_json={"turn_id": "turn_stream", "content": "正在整理"},
                    created_at=base + timedelta(seconds=2),
                ),
                AgentEvent(
                    tenant_id="tenant_demo",
                    session_id=session.id,
                    event_type="stream_delta",
                    payload_json={"turn_id": "turn_stream", "content": "采购清单"},
                    created_at=base + timedelta(seconds=3),
                ),
            ]
        )
        db.commit()

        running = teams_api.get_team_conversation_stream(
            team.id, session.id, "tenant_demo", db, _member_user()
        )
        assert running.status == "running"
        assert running.content == "正在整理采购清单"

        db.add(
            AgentEvent(
                tenant_id="tenant_demo",
                session_id=session.id,
                event_type="stream_end",
                payload_json={"turn_id": "turn_stream"},
                created_at=base + timedelta(seconds=4),
            )
        )
        db.commit()

        completed = teams_api.get_team_conversation_stream(
            team.id, session.id, "tenant_demo", db, _member_user()
        )
        assert completed.status == "completed"
        assert completed.content == "正在整理采购清单"


# ---------- 团队会话消息端点 ----------


def test_conversation_messages_endpoint_order_and_isolation() -> None:
    """消息按 created_at 正序;跨团队/无 team_id 的 sessionId 一律 404。"""
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        session = _make_session(
            db, session_id="sess_msg", team_id=team.id, agent_id="agent_worker",
            title="团队任务:写方案", created_at=base,
        )
        # 乱序插入,验证端点按 created_at 正序返回
        _make_message(
            db, session_id=session.id, message_id="m2", role="assistant",
            content="第二条", created_at=base + timedelta(minutes=2),
        )
        _make_message(
            db, session_id=session.id, message_id="m1", role="user",
            content="第一条", created_at=base + timedelta(minutes=1),
        )
        team2 = create_team(
            db, tenant_id="tenant_demo", name="隔离团队",
            description=None, owner_user_id="user_admin",
        )
        other = _make_session(
            db, session_id="sess_x", team_id=team2.id, agent_id="agent_worker",
            title="团队任务:外部", created_at=base,
        )
        plain = _make_session(
            db, session_id="sess_plain", team_id=None, agent_id="agent_worker",
            title="普通会话", created_at=base,
        )
        db.commit()

        rows = teams_api.list_team_conversation_messages(
            team.id, session.id, "tenant_demo", db, _member_user()
        )
        assert [row.id for row in rows] == ["m1", "m2"]
        assert rows[0].role == "user"
        assert rows[0].content == "第一条"
        assert rows[1].role == "assistant"

        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_conversation_messages(
                team.id, other.id, "tenant_demo", db, _admin_user()
            )
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_conversation_messages(
                team.id, plain.id, "tenant_demo", db, _admin_user()
            )
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_conversation_messages(
                team.id, "sess_missing", "tenant_demo", db, _admin_user()
            )
        assert exc_info.value.status_code == 404


def test_conversation_messages_reuse_chat_metadata_and_hide_only_team_control_json() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        session = _make_session(
            db,
            session_id="sess_structured_reply",
            team_id=team.id,
            agent_id="agent_worker",
            title="团队任务:整理制度",
            created_at=base,
        )
        _make_message(
            db,
            session_id=session.id,
            message_id="msg_structured_reply",
            role="assistant",
            content=(
                "## 制度结论\n\n依据资料 [1]。\n"
                '```json\n{"example": true}\n```\n'
                '```json\n{"team_tasks": [{"title": "内部控制块"}]}\n```'
            ),
            metadata={
                "turn_id": "turn_structured_reply",
                "knowledge_citations": [
                    {"id": "1", "label": "1", "title": "报销制度", "excerpt": "制度正文"}
                ],
                "harness_artifacts": [
                    {
                        "type": "workspace_file",
                        "task_frame_id": "frame_1",
                        "path": "results/policy.md",
                    }
                ],
            },
            created_at=base + timedelta(seconds=1),
        )
        db.commit()

        [row] = teams_api.list_team_conversation_messages(
            team.id, session.id, "tenant_demo", db, _member_user()
        )

        assert row.turn_id == "turn_structured_reply"
        assert row.metadata["knowledge_citations"][0]["title"] == "报销制度"
        assert row.metadata["harness_artifacts"][0]["path"] == "results/policy.md"
        assert '"example": true' in row.content
        assert "team_tasks" not in row.content

# ---------- 发送复用:团队会话对本租户成员放行 ----------


def test_chat_turn_allows_tenant_member_on_team_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """团队会话(team_id 非空)允许本租户非创建者发言;普通会话仍仅创建者可见。"""
    with _test_session() as db:
        team = _seed_team(db)
        session = _make_session(
            db, session_id="sess_shared_tl", team_id=team.id, agent_id="agent_tl",
            title=f"团队 {team.name} · TL 对话", created_at=datetime(2026, 1, 1),
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(chat_api, "_schedule_session_title_summary", lambda *a, **kw: None)

        def fake_handle_turn(self, request):
            return ChatTurnResponse(
                reply="收到,先讨论。",
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        # 会话 user_id 是 user_admin,同租户成员 user_member 发言不再被所有权校验挡住
        response = chat_api.chat_turn(
            ChatTurnRequest(
                tenant_id="tenant_demo",
                session_id=session.id,
                client_turn_id="ct_member_1",
                message="聊聊规划",
            ),
            _member_user(),
            db,
        )
        assert response.reply.startswith("收到")

        private = _make_session(
            db, session_id="sess_private", team_id=None, agent_id="agent_tl",
            title="私聊", created_at=datetime(2026, 1, 1),
        )
        db.commit()
        with pytest.raises(HTTPException) as exc_info:
            chat_api.chat_turn(
                ChatTurnRequest(
                    tenant_id="tenant_demo",
                    session_id=private.id,
                    client_turn_id="ct_member_2",
                    message="你好",
                ),
                _member_user(),
                db,
            )
        assert exc_info.value.status_code == 404


def test_team_and_private_sessions_drive_trusted_knowledge_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """团队群聊传入团队范围，员工私聊明确传入空团队范围。"""
    observed_contexts: list[tuple[str, str | None]] = []

    class RecordingKnowledgeAccessService:
        """记录 Harness 从服务端会话解析出的知识范围。"""

        def __init__(self, db: Session) -> None:
            """保留数据库会话，模拟真实访问服务的构造契约。"""
            self.db = db

        def resolve_projections(
            self,
            *,
            tenant_id: str,
            agent_id: str,
            team_id: str | None,
        ) -> list[SimpleNamespace]:
            """按收到的可信团队标识返回可辨识的冻结版本。"""
            assert tenant_id == "tenant_demo"
            observed_contexts.append((agent_id, team_id))
            suffix = "shared" if team_id else "private"
            return [
                SimpleNamespace(
                    knowledge_base_id=f"kb_{suffix}",
                    knowledge_base_version_id=f"version_{suffix}",
                )
            ]

    monkeypatch.setattr(
        harness_v2_engine,
        "KnowledgeAccessService",
        RecordingKnowledgeAccessService,
    )

    with _test_session() as db:
        team = _seed_team(db)
        team_id = team.id
        team_session = _make_session(
            db,
            session_id="sess_team_knowledge_scope",
            team_id=team.id,
            agent_id="agent_tl",
            title=f"团队 {team.name} · TL 对话",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        private_session = _make_session(
            db,
            session_id="sess_private_knowledge_scope",
            team_id=None,
            agent_id="agent_tl",
            title="员工私聊",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.commit()

        engine = object.__new__(HarnessV2Engine)
        engine.db = db

        assert engine._freeze_turn_knowledge_versions(
            "tenant_demo", team_session
        ) == {"kb_shared": "version_shared"}
        assert engine._freeze_turn_knowledge_versions(
            "tenant_demo", private_session
        ) == {"kb_private": "version_private"}

    assert observed_contexts == [
        ("agent_tl", team_id),
        ("agent_tl", None),
    ]


def test_wakeup_turn_rejects_a_session_owned_by_another_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唤醒入口必须核对事件团队与持久化会话团队，不能跨团队复用。"""
    observed_requests: list[ChatTurnRequest] = []

    def fake_stream(self, request):
        """记录越权请求；修复后该函数不应被调用。"""
        observed_requests.append(request)
        yield {
            "event": "complete",
            "data": ChatTurnResponse(
                reply="不应执行",
                session_id=str(request.session_id),
                session_state=SessionPublic(
                    session_id=str(request.session_id),
                    tenant_id=request.tenant_id,
                ),
            ).model_dump(mode="json"),
        }

    monkeypatch.setattr(AgentLoop, "handle_turn_stream", fake_stream)

    with _test_session() as db:
        team_a = _seed_team(db)
        team_b = create_team(
            db,
            tenant_id="tenant_demo",
            name="共享执行团队B",
            description=None,
            owner_user_id="user_admin",
        )
        add_member(db, team_b, agent_id="agent_tl", role="leader")
        session_a = _make_session(
            db,
            session_id="sess_wrong_wakeup_team",
            team_id=team_a.id,
            agent_id="agent_tl",
            title=f"团队 {team_a.name} · TL 对话",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.commit()
        leader = db.get(AgentProfile, "agent_tl")
        assert leader is not None

        with pytest.raises(RuntimeError, match="不属于当前团队"):
            wakeup.run_agent_turn(
                db,
                team=team_b,
                agent=leader,
                session_id=session_a.id,
                wake_event_id="wake-cross-team",
                message="执行团队唤醒",
                interaction_mode="team_tl",
            )

    assert observed_requests == []


# ---------- 团队会话写入权限与共享 TL 隔离 ----------


def test_chat_turn_and_stream_reject_non_tl_team_sessions() -> None:
    """任务执行/竞标/验收会话仅可查看:人工 /turn、/stream 一律 403,TL 对话不受影响。"""
    with _test_session() as db:
        team = _seed_team(db)
        base = datetime(2026, 1, 1, 12, 0, 0)
        sessions = [
            _make_session(
                db, session_id="sess_ro_task", team_id=team.id, agent_id="agent_worker",
                title="团队任务:写方案", created_at=base,
            ),
            _make_session(
                db, session_id="sess_ro_review", team_id=team.id, agent_id="agent_tl",
                title="团队任务验收:写方案", created_at=base,
            ),
            _make_session(
                db, session_id="sess_ro_bid", team_id=team.id, agent_id="agent_worker",
                title="团队竞标:写方案", created_at=base,
            ),
        ]
        db.commit()

        for session in sessions:
            with pytest.raises(HTTPException) as exc_info:
                chat_api.chat_turn(
                    ChatTurnRequest(
                        tenant_id="tenant_demo",
                        session_id=session.id,
                        client_turn_id=f"ct_ro_{session.id}",
                        message="插一句话",
                    ),
                    _member_user(),
                    db,
                )
            assert exc_info.value.status_code == 403
            with pytest.raises(HTTPException) as exc_info:
                chat_api.chat_stream(
                    ChatTurnRequest(
                        tenant_id="tenant_demo",
                        session_id=session.id,
                        client_turn_id=f"cs_ro_{session.id}",
                        message="插一句话",
                    ),
                    _member_user(),
                    db,
                )
            assert exc_info.value.status_code == 403


def test_tl_chat_rejects_session_from_other_team_with_shared_tl() -> None:
    """共享 TL 场景:不能把 A 团队的 TL 会话传给 B 团队的 /tl/chat。"""
    with _test_session() as db:
        team_a = _seed_team(db)
        team_b = create_team(
            db, tenant_id="tenant_demo", name="共享TL团队B",
            description=None, owner_user_id="user_admin",
        )
        add_member(db, team_b, agent_id="agent_tl", role="leader")
        session_a = _make_session(
            db, session_id="sess_tl_a", team_id=team_a.id, agent_id="agent_tl",
            title=f"团队 {team_a.name} · TL 对话", created_at=datetime(2026, 1, 1),
        )
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            teams_api.tl_chat_endpoint(
                team_b.id,
                TeamTLChatRequest(
                    tenant_id="tenant_demo",
                    message="给 B 团队派个任务",
                    session_id=session_a.id,
                ),
                db,
                _admin_user(),
            )
        assert exc_info.value.status_code == 404


def test_team_threads_scope_tl_sessions_by_team_with_shared_tl() -> None:
    """共享 TL 场景:统一线程列表按 team_id 归属,同一会话不会以两个团队名重复出现。"""
    with _test_session() as db:
        team_a = _seed_team(db)
        team_b = create_team(
            db, tenant_id="tenant_demo", name="共享TL团队B",
            description=None, owner_user_id="user_admin",
        )
        add_member(db, team_b, agent_id="agent_tl", role="leader")
        base = datetime(2026, 1, 1, 12, 0, 0)
        session_a = _make_session(
            db, session_id="sess_thread_a", team_id=team_a.id, agent_id="agent_tl",
            title=f"团队 {team_a.name} · TL 对话", created_at=base,
        )
        session_b = _make_session(
            db, session_id="sess_thread_b", team_id=team_b.id, agent_id="agent_tl",
            title=f"团队 {team_b.name} · TL 对话", created_at=base,
        )
        db.commit()

        threads = teams_api.list_team_threads("tenant_demo", db, _member_user())
        tl_threads = [item for item in threads if item.kind == "tl_chat"]
        by_session = {item.session_id: item.team_id for item in tl_threads}
        assert by_session == {session_a.id: team_a.id, session_b.id: team_b.id}
