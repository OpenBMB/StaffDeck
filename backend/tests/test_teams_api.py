from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import teams as teams_api
from app.core import AgentLoop
from app.db.models import AgentProfile, Team, TeamTask, TeamWakeEvent, Tenant, User
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic
from app.teams import wakeup
from app.teams.schema import (
    ReviewOverrideRequest,
    TeamCreateRequest,
    TeamLeaderUpdateRequest,
    TeamMemberAddRequest,
    TeamTaskResumeRequest,
    TeamTLChatRequest,
    TeamUpdateRequest,
)
from app.teams.service import (
    TeamTaskTransitionError,
    add_member,
    apply_task_transition,
    create_team,
    parse_tl_review,
    parse_tl_task_assignments,
    set_leader,
    task_activation_state,
)
from app.teams.wakeup import claim_wake_event, enqueue_wake_event, execute_wake_event


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _admin_user() -> User:
    return User(
        id="user_admin", tenant_id="tenant_demo", username="ops", role="admin", password_hash="test"
    )


def _member_user() -> User:
    return User(
        id="user_member",
        tenant_id="tenant_demo",
        username="member",
        role="member",
        password_hash="test",
    )


def _seed_agents(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(Tenant(id="tenant_other", name="Other"))
    db.add(AgentProfile(id="agent_tl", tenant_id="tenant_demo", name="TL"))
    db.add(AgentProfile(id="agent_worker", tenant_id="tenant_demo", name="Worker"))
    db.add(AgentProfile(id="agent_worker2", tenant_id="tenant_demo", name="Worker2"))
    db.add(AgentProfile(id="agent_outside", tenant_id="tenant_other", name="Outsider"))
    db.commit()


def _seed_team(db: Session) -> Team:
    _seed_agents(db)
    team = create_team(
        db,
        tenant_id="tenant_demo",
        name="增长团队",
        description=None,
        owner_user_id="user_admin",
    )
    add_member(db, team, agent_id="agent_tl", role="leader")
    add_member(db, team, agent_id="agent_worker")
    return team


def _make_task(db: Session, team: Team, *, status: str = "pending") -> TeamTask:
    task = TeamTask(
        team_id=team.id,
        tenant_id=team.tenant_id,
        title="调研竞品",
        description="输出调研报告",
        status=status,
        created_by_user_id="user_admin",
        created_by_tl=True,
        assignee_agent_id="agent_worker",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _stub_start_wakeup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把异步唤醒改成同步收集,避免测试里真的起线程。"""
    started: list[str] = []
    monkeypatch.setattr(wakeup, "start_wakeup_async", started.append)
    monkeypatch.setattr(teams_api, "start_wakeup_async", started.append)
    return started


# ---------- 团队与成员 CRUD ----------


def test_team_crud_and_unique_name() -> None:
    with _test_session() as db:
        _seed_agents(db)
        admin = _admin_user()
        team = teams_api.create_team_endpoint(
            TeamCreateRequest(tenant_id="tenant_demo", name="增长团队"), db, admin
        )
        assert team.owner_user_id == "user_admin"
        assert team.status == "active"

        with pytest.raises(HTTPException) as exc_info:
            teams_api.create_team_endpoint(
                TeamCreateRequest(tenant_id="tenant_demo", name="增长团队"), db, admin
            )
        assert exc_info.value.status_code == 409

        teams = teams_api.list_teams("tenant_demo", db, admin)
        assert [item.name for item in teams] == ["增长团队"]

        detail = teams_api.get_team_endpoint(team.id, "tenant_demo", db, admin)
        assert detail.id == team.id

        updated = teams_api.update_team_endpoint(
            team.id,
            TeamUpdateRequest(tenant_id="tenant_demo", description="负责增长"),
            db,
            admin,
        )
        assert updated.description == "负责增长"

        assert teams_api.delete_team_endpoint(team.id, "tenant_demo", db, admin) == {"ok": True}
        assert teams_api.list_teams("tenant_demo", db, admin) == []


def test_delete_team_cascades_members_and_tasks() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        assert teams_api.delete_team_endpoint(team.id, "tenant_demo", db, _admin_user()) == {
            "ok": True
        }
        assert db.get(TeamTask, task.id) is None
        assert db.exec(select(TeamWakeEvent)).all() == []


def test_member_add_remove_and_constraints() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()

        with pytest.raises(HTTPException) as exc_info:
            teams_api.add_member_endpoint(
                team.id,
                TeamMemberAddRequest(tenant_id="tenant_demo", agent_id="agent_worker"),
                db,
                admin,
            )
        assert exc_info.value.status_code == 409

        with pytest.raises(HTTPException) as exc_info:
            teams_api.add_member_endpoint(
                team.id,
                TeamMemberAddRequest(tenant_id="tenant_demo", agent_id="agent_outside"),
                db,
                admin,
            )
        assert exc_info.value.status_code == 404

        member = teams_api.add_member_endpoint(
            team.id,
            TeamMemberAddRequest(tenant_id="tenant_demo", agent_id="agent_worker2"),
            db,
            admin,
        )
        assert member.role == "member"
        assert member.agent_name == "Worker2"

        assert teams_api.remove_member_endpoint(
            team.id, "agent_worker2", "tenant_demo", db, admin
        ) == {"ok": True}


def test_leader_uniqueness_and_reassign() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        add_member(db, team, agent_id="agent_worker2")
        admin = _admin_user()

        leader = teams_api.set_leader_endpoint(
            team.id,
            TeamLeaderUpdateRequest(tenant_id="tenant_demo", agent_id="agent_worker2"),
            db,
            admin,
        )
        assert leader.role == "leader"

        detail = teams_api.get_team_endpoint(team.id, "tenant_demo", db, admin)
        leaders = [item for item in detail.members if item.role == "leader"]
        assert len(leaders) == 1
        assert leaders[0].agent_id == "agent_worker2"
        old_tl = next(item for item in detail.members if item.agent_id == "agent_tl")
        assert old_tl.role == "member"

        # 换任不存在的成员 -> 404
        with pytest.raises(HTTPException) as exc_info:
            set_leader(db, team, "agent_outside")
        assert exc_info.value.status_code == 404


def test_manage_permission_owner_or_admin() -> None:
    with _test_session() as db:
        _seed_agents(db)
        team = teams_api.create_team_endpoint(
            TeamCreateRequest(tenant_id="tenant_demo", name="增长团队"), db, _member_user()
        )
        # 非 owner 非 admin 不可管
        with pytest.raises(HTTPException) as exc_info:
            teams_api.update_team_endpoint(
                team.id,
                TeamUpdateRequest(tenant_id="tenant_demo", description="x"),
                db,
                _admin_user_other(),
            )
        assert exc_info.value.status_code == 403
        # owner(普通成员角色)可管
        updated = teams_api.update_team_endpoint(
            team.id,
            TeamUpdateRequest(tenant_id="tenant_demo", description="y"),
            db,
            _member_user(),
        )
        assert updated.description == "y"


def _admin_user_other() -> User:
    return User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        role="member",
        password_hash="test",
    )


# ---------- 任务状态机 ----------


def test_task_state_machine_transitions() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        kwargs = {"actor_type": "agent", "actor_id": "agent_worker", "event_type": "test"}

        with pytest.raises(TeamTaskTransitionError):
            apply_task_transition(db, task, "done", **kwargs)
        # pending -> bidding 为任务池竞标入口(增量 3 激活)
        apply_task_transition(db, task, "bidding", **kwargs)
        apply_task_transition(db, task, "pending", **kwargs)

        apply_task_transition(db, task, "in_progress", **kwargs)
        apply_task_transition(db, task, "review", **kwargs)
        apply_task_transition(db, task, "rework", **kwargs)
        apply_task_transition(db, task, "in_progress", **kwargs)
        apply_task_transition(db, task, "review", **kwargs)
        apply_task_transition(db, task, "done", **kwargs)
        assert task.version == 8
        with pytest.raises(TeamTaskTransitionError):
            apply_task_transition(db, task, "in_progress", **kwargs)


# ---------- TL 结构化输出解析 ----------


def test_parse_tl_task_assignments_ok() -> None:
    reply = (
        "好的,我来拆解。\n"
        '```json\n{"team_tasks": [{"title": "调研", "description": "竞品分析", '
        '"assignee_agent_id": "agent_worker"}]}\n```'
    )
    tasks = parse_tl_task_assignments(reply)
    assert tasks == [
        {"title": "调研", "description": "竞品分析", "assignee_agent_id": "agent_worker"}
    ]


def test_parse_tl_task_assignments_keeps_dependency_graph_fields() -> None:
    reply = (
        '```json\n{"team_tasks": ['
        '{"client_ref": "source", "title": "收集资料", '
        '"assignee_agent_id": "agent_worker"}, '
        '{"client_ref": "summary", "title": "汇总结论", '
        '"assignee_agent_id": "agent_worker2", "depends_on": ["source"], '
        '"activation_condition": {"type": "minimum_succeeded", "minimum": 1}}'
        "]}\n```"
    )

    assert parse_tl_task_assignments(reply)[1] == {
        "client_ref": "summary",
        "title": "汇总结论",
        "assignee_agent_id": "agent_worker2",
        "depends_on": ["source"],
        "activation_condition": {"type": "minimum_succeeded", "minimum": 1},
    }


def test_parse_tl_task_assignments_no_block_or_bad_json() -> None:
    assert parse_tl_task_assignments("随便聊聊,没有代码块") == []
    assert parse_tl_task_assignments('```json\n{"team_tasks": [坏掉的\n```') == []
    # title 缺失的条目被跳过;assignee 缺省表示投入任务池竞标(增量 3)
    reply = '```json\n{"team_tasks": [{"title": "", "assignee_agent_id": "a"}, {"title": "x"}]}\n```'
    assert parse_tl_task_assignments(reply) == [{"title": "x"}]


def test_parse_tl_review() -> None:
    reply = '验收完毕\n```json\n{"team_review": {"verdict": "rework", "comment": "重做"}}\n```'
    assert parse_tl_review(reply) == {"verdict": "rework", "comment": "重做"}
    assert parse_tl_review("没有块") is None
    assert parse_tl_review('```json\n{"team_review": {"verdict": "unknown"}}\n```') is None


# ---------- TL 对话入口 ----------


def test_tl_chat_creates_tasks_and_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        started = _stub_start_wakeup(monkeypatch)

        def fake_handle_turn(self, request):
            assert request.interaction_mode == "team_tl"
            assert request.message == "帮我调研竞品"
            # 团队上下文仅注入运行时，不写入可见消息。
            assert "agent_worker" in (request.context_injection or "")
            reply = (
                "收到,派给 Worker。\n"
                '```json\n{"team_tasks": [{"title": "竞品调研", '
                '"assignee_agent_id": "agent_worker"}, '
                '{"title": "外人任务", "assignee_agent_id": "agent_outside"}]}\n```'
            )
            return ChatTurnResponse(
                reply=reply,
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)

        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(tenant_id="tenant_demo", message="帮我调研竞品"),
            db,
            _admin_user(),
        )
        # 非成员 agent 的指派被跳过
        assert len(response.created_tasks) == 1
        task = response.created_tasks[0]
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_worker"
        assert task.created_by_tl is True
        assert "```json" not in response.reply

        wakes = db.exec(select(TeamWakeEvent)).all()
        assert len(wakes) == 1
        assert wakes[0].trigger_type == "task_assigned"
        assert wakes[0].status == "pending"
        assert wakes[0].payload_json["task_id"] == task.id
        assert started == [wakes[0].id]

        # 审计流水
        detail = teams_api.get_team_task(team.id, task.id, "tenant_demo", db, _admin_user())
        assert [item.event_type for item in detail.events] == ["task_created"]


def test_tl_chat_does_not_repair_natural_language_into_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少执行输入时的澄清回复不能被关键词启发式转换成成员任务。"""
    with _test_session() as db:
        team = _seed_team(db)
        started = _stub_start_wakeup(monkeypatch)
        requests: list[ChatTurnRequest] = []

        def fake_handle_turn(self, request):
            requests.append(request)
            return ChatTurnResponse(
                reply=(
                    "收到，我负责商品比价与购买，行政负责后续报销。"
                    "请问您具体想购买什么商品？"
                ),
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id,
                    tenant_id=request.tenant_id,
                    awaiting_input={"question": "具体想购买什么商品？"},
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(
                tenant_id="tenant_demo",
                message="我想买个东西，然后行政帮我报销一下",
            ),
            db,
            _admin_user(),
        )

        assert len(requests) == 1
        assert response.created_tasks == []
        assert db.exec(select(TeamTask)).all() == []
        assert db.exec(select(TeamWakeEvent)).all() == []
        assert started == []


def test_tl_chat_blocks_dependent_tasks_until_predecessor_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        add_member(db, team, agent_id="agent_worker2")
        started = _stub_start_wakeup(monkeypatch)

        def fake_handle_turn(self, request):
            reply = (
                "已按依赖关系安排。\n"
                '```json\n{"team_tasks": ['
                '{"client_ref": "collect", "title": "收集资料", '
                '"assignee_agent_id": "agent_worker"}, '
                '{"client_ref": "publish", "title": "发布报告", '
                '"assignee_agent_id": "agent_worker2", "depends_on": ["collect"], '
                '"activation_condition": {"type": "all_succeeded"}}'
                "]}\n```"
            )
            return ChatTurnResponse(
                reply=reply,
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(tenant_id="tenant_demo", message="先收集资料，再发布报告"),
            db,
            _admin_user(),
        )
        by_title = {item.title: item for item in response.created_tasks}
        source = db.get(TeamTask, by_title["收集资料"].id)
        dependent = db.get(TeamTask, by_title["发布报告"].id)
        assert source is not None and dependent is not None
        assert source.status == "pending"
        assert dependent.status == "blocked"
        assert dependent.depends_on_task_ids_json == [source.id]
        assert len(started) == 1

        for status in ("in_progress", "review", "done"):
            apply_task_transition(
                db,
                source,
                status,
                actor_type="agent",
                actor_id="agent_worker",
                event_type=f"test_{status}",
            )
        db.commit()
        activated = wakeup.activate_ready_tasks(db, team)
        db.refresh(dependent)

        assert [task.id for task in activated] == [dependent.id]
        assert dependent.status == "pending"
        assert len(started) == 2
        wakes = [
            item
            for item in db.exec(select(TeamWakeEvent)).all()
            if item.payload_json.get("task_id") == dependent.id
        ]
        assert len(wakes) == 1


def test_tl_chat_rejects_cyclic_dependency_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        started = _stub_start_wakeup(monkeypatch)

        def fake_handle_turn(self, request):
            return ChatTurnResponse(
                reply=(
                    '```json\n{"team_tasks": ['
                    '{"client_ref": "a", "title": "A", "depends_on": ["b"]}, '
                    '{"client_ref": "b", "title": "B", "depends_on": ["a"]}'
                    "]}\n```"
                ),
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(tenant_id="tenant_demo", message="创建循环任务"),
            db,
            _admin_user(),
        )

        assert response.created_tasks == []
        assert db.exec(select(TeamTask)).all() == []
        assert db.exec(select(TeamWakeEvent)).all() == []
        assert started == []


def test_tl_chat_rejects_duplicate_dependency_references(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        started = _stub_start_wakeup(monkeypatch)

        def fake_handle_turn(self, request):
            return ChatTurnResponse(
                reply=(
                    '```json\n{"team_tasks": ['
                    '{"client_ref": "shared", "title": "A"}, '
                    '{"client_ref": "shared", "title": "B"}'
                    "]}\n```"
                ),
                session_id=request.session_id,
                session_state=SessionPublic(
                    session_id=request.session_id, tenant_id=request.tenant_id
                ),
            )

        monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
        response = teams_api.tl_chat_endpoint(
            team.id,
            TeamTLChatRequest(tenant_id="tenant_demo", message="创建两项任务"),
            db,
            _admin_user(),
        )

        assert response.created_tasks == []
        assert db.exec(select(TeamTask)).all() == []
        assert db.exec(select(TeamWakeEvent)).all() == []
        assert started == []


@pytest.mark.parametrize(
    ("condition", "source_statuses", "needs_input", "expected"),
    [
        ({"type": "all_succeeded"}, ["done", "done"], [False, False], "ready"),
        ({"type": "any_succeeded"}, ["done", "in_progress"], [False, False], "ready"),
        (
            {"type": "minimum_succeeded", "minimum": 2},
            ["done", "done", "in_progress"],
            [False, False, False],
            "ready",
        ),
        ({"type": "all_terminal"}, ["done", "escalated"], [False, False], "ready"),
        ({"type": "all_succeeded"}, ["done", "escalated"], [False, False], "impossible"),
        ({"type": "all_terminal"}, ["done", "escalated"], [False, True], "blocked"),
    ],
)
def test_task_activation_state_supports_generic_fan_in_conditions(
    condition: dict[str, object],
    source_statuses: list[str],
    needs_input: list[bool],
    expected: str,
) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        sources: list[TeamTask] = []
        for index, (status, waiting) in enumerate(
            zip(source_statuses, needs_input, strict=True),
            1,
        ):
            source = TeamTask(
                team_id=team.id,
                tenant_id=team.tenant_id,
                title=f"前置任务 {index}",
                status=status,
                created_by_user_id="user_admin",
                created_by_tl=True,
                report_json={"needs_input": True} if waiting else {},
            )
            db.add(source)
            db.flush()
            sources.append(source)
        dependent = TeamTask(
            team_id=team.id,
            tenant_id=team.tenant_id,
            title="汇聚任务",
            status="blocked",
            created_by_user_id="user_admin",
            created_by_tl=True,
            depends_on_task_ids_json=[item.id for item in sources],
            activation_condition_json=condition,
        )
        db.add(dependent)
        db.flush()

        assert task_activation_state(db, dependent) == expected


def test_tl_chat_requires_leader() -> None:
    with _test_session() as db:
        _seed_agents(db)
        team = create_team(
            db, tenant_id="tenant_demo", name="无TL团队",
            description=None, owner_user_id="user_admin",
        )
        with pytest.raises(HTTPException) as exc_info:
            teams_api.tl_chat_endpoint(
                team.id,
                TeamTLChatRequest(tenant_id="tenant_demo", message="hi"),
                db,
                _admin_user(),
            )
        assert exc_info.value.status_code == 400


# ---------- 唤醒链路:成员执行 + TL 验收 ----------


def test_member_execution_report_then_tl_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        started = _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kwargs: "完成报告:已交付")
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *args, **kw: "completed")

        assert claim_wake_event(db, wake.id) is True
        execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.status == "review"
        assert task.report_json["full_reply"] == "完成报告:已交付"
        assert task.session_id is not None

        tl_wakes = db.exec(
            select(TeamWakeEvent).where(TeamWakeEvent.trigger_type == "task_report")
        ).all()
        assert len(tl_wakes) == 1
        assert tl_wakes[0].target_agent_id == "agent_tl"
        assert tl_wakes[0].status == "pending"
        assert started == [tl_wakes[0].id]
        assert db.get(TeamWakeEvent, wake.id).status == "done"


def test_tl_review_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        ("approve", "done", None),
        ("escalate", "escalated", None),
        ("rework", "rework", "task_rework"),
    ]
    for verdict, expected_status, expected_wake in cases:
        with _test_session() as db:
            team = _seed_team(db)
            task = _make_task(db, team, status="review")
            task.report_json = {"summary": "报告", "full_reply": "报告全文"}
            wake = enqueue_wake_event(
                db, team=team, target_agent_id="agent_tl",
                trigger_type="task_report", payload={"task_id": task.id},
            )
            db.commit()
            started = _stub_start_wakeup(monkeypatch)
            reply = f'```json\n{{"team_review": {{"verdict": "{verdict}", "comment": "意见"}}}}\n```'
            monkeypatch.setattr(
                wakeup, "run_agent_turn", lambda *args, _reply=reply, **kw: _reply
            )

            assert claim_wake_event(db, wake.id) is True
            execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

            db.refresh(task)
            assert task.status == expected_status
            assert task.review_json["verdict"] == verdict
            assert task.review_json["comment"] == "意见"
            if expected_wake:
                rework_wakes = db.exec(
                    select(TeamWakeEvent).where(TeamWakeEvent.trigger_type == expected_wake)
                ).all()
                assert len(rework_wakes) == 1
                assert rework_wakes[0].target_agent_id == "agent_worker"
                assert started == [rework_wakes[0].id]
            else:
                assert started == []


def test_tl_review_without_verdict_keeps_review(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="review")
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_tl",
            trigger_type="task_report", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: "我还需要想想")

        assert claim_wake_event(db, wake.id) is True
        execute_wake_event(db, db.get(TeamWakeEvent, wake.id))
        db.refresh(task)
        assert task.status == "review"
        assert db.get(TeamWakeEvent, wake.id).status == "done"


def test_failed_turn_escalates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(wakeup, "run_agent_turn", boom)
        assert claim_wake_event(db, wake.id) is True
        execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.status == "escalated"
        failed = db.get(TeamWakeEvent, wake.id)
        assert failed.status == "failed"
        assert "LLM 不可用" in (failed.error or "")


# ---------- 人改判(HITL override) ----------


def test_override_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="review")
        started = _stub_start_wakeup(monkeypatch)
        admin = _admin_user()

        result = teams_api.override_task_review(
            team.id, task.id,
            ReviewOverrideRequest(tenant_id="tenant_demo", verdict="approve", comment="通过"),
            db, admin,
        )
        assert result.status == "done"
        assert result.review["overridden_by_user_id"] == "user_admin"

        # done 之后不可再改判
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_review(
                team.id, task.id,
                ReviewOverrideRequest(tenant_id="tenant_demo", verdict="rework"),
                db, admin,
            )
        assert exc_info.value.status_code == 409

        # pending 状态不可改判
        task2 = _make_task(db, team)
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_review(
                team.id, task2.id,
                ReviewOverrideRequest(tenant_id="tenant_demo", verdict="approve"),
                db, admin,
            )
        assert exc_info.value.status_code == 409

        # rework:任务退回并唤醒成员
        task3 = _make_task(db, team, status="review")
        result = teams_api.override_task_review(
            team.id, task3.id,
            ReviewOverrideRequest(tenant_id="tenant_demo", verdict="rework", comment="重做"),
            db, admin,
        )
        assert result.status == "rework"
        rework_wakes = db.exec(
            select(TeamWakeEvent).where(TeamWakeEvent.trigger_type == "task_rework")
        ).all()
        assert len(rework_wakes) == 1
        assert rework_wakes[0].payload_json["task_id"] == task3.id
        assert started == [rework_wakes[0].id]

        # escalated 任务也可被人改判回 done
        task4 = _make_task(db, team, status="escalated")
        result = teams_api.override_task_review(
            team.id, task4.id,
            ReviewOverrideRequest(tenant_id="tenant_demo", verdict="approve"),
            db, admin,
        )
        assert result.status == "done"


def test_override_requires_manager() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="review")
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_review(
                team.id, task.id,
                ReviewOverrideRequest(tenant_id="tenant_demo", verdict="approve"),
                db, _admin_user_other(),
            )
        assert exc_info.value.status_code == 403


def test_resume_needs_input_task_with_user_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="escalated")
        task.report_json = {
            "needs_input": True,
            "full_reply": "请提供员工工号和物品清单。",
        }
        db.add(task)
        db.commit()
        started = _stub_start_wakeup(monkeypatch)

        result = teams_api.resume_team_task(
            team.id,
            task.id,
            TeamTaskResumeRequest(
                tenant_id="tenant_demo",
                answer="工号 001，需要 A4 纸 2 包。",
            ),
            db,
            _admin_user(),
        )

        assert result.status == "rework"
        assert result.report["needs_input"] is False
        assert result.review["comment"] == "工号 001，需要 A4 纸 2 包。"
        assert result.events[-1].event_type == "task_input_provided"
        wakes = db.exec(
            select(TeamWakeEvent).where(TeamWakeEvent.trigger_type == "task_rework")
        ).all()
        assert len(wakes) == 1
        assert wakes[0].payload_json["task_id"] == task.id
        assert started == [wakes[0].id]


def test_resume_rejects_task_not_waiting_for_input() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="escalated")

        with pytest.raises(HTTPException) as exc_info:
            teams_api.resume_team_task(
                team.id,
                task.id,
                TeamTaskResumeRequest(tenant_id="tenant_demo", answer="补充信息"),
                db,
                _admin_user(),
            )

        assert exc_info.value.status_code == 409


def test_resume_allows_the_user_who_requested_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="escalated")
        task.created_by_user_id = "user_member"
        task.report_json = {"needs_input": True, "full_reply": "请补充信息。"}
        db.add(task)
        db.commit()
        _stub_start_wakeup(monkeypatch)

        result = teams_api.resume_team_task(
            team.id,
            task.id,
            TeamTaskResumeRequest(tenant_id="tenant_demo", answer="补充信息"),
            db,
            _member_user(),
        )

        assert result.status == "rework"


# ---------- 唤醒事件原子认领 ----------


def test_claim_wake_event_only_once() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        assert claim_wake_event(db, wake.id) is True
        # 重复认领(并发/重试)只生效一次
        assert claim_wake_event(db, wake.id) is False
        assert db.get(TeamWakeEvent, wake.id).status == "claimed"


# ---------- 任务列表 ----------


def test_list_tasks_filter_by_status() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        _make_task(db, team)
        _make_task(db, team, status="review")
        admin = _admin_user()

        all_tasks = teams_api.list_team_tasks(team.id, "tenant_demo", None, db, admin)
        assert len(all_tasks) == 2
        review_tasks = teams_api.list_team_tasks(team.id, "tenant_demo", "review", db, admin)
        assert len(review_tasks) == 1
        assert review_tasks[0].status == "review"

        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_tasks(team.id, "tenant_demo", "nonsense", db, admin)
        assert exc_info.value.status_code == 400
