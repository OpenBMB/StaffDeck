from __future__ import annotations

from datetime import datetime, timedelta

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
from app.core import AgentLoop
from app.db.models import ChatSession, Message, TeamTask, TeamWakeEvent
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic
from app.teams import wakeup
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
) -> Message:
    row = Message(
        id=message_id,
        tenant_id="tenant_demo",
        session_id=session_id,
        role=role,
        content=content,
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
        assert by_id[s_bid.id].task_id is None
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
