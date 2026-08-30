from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from test_teams_api import (
    _admin_user,
    _admin_user_other,
    _stub_start_wakeup,
    _test_session,
)
from test_teams_bidding import (
    _bid_reply,
    _events,
    _make_pool_task,
    _pending_wakes,
    _run_wake,
    _seed_pool_team,
)

from app.api import teams as teams_api
from app.db.models import (
    ChatSession,
    KnowledgeIngestJob,
    Team,
    TeamBlackboardEntry,
    TeamTask,
    TeamTaskBid,
    TeamWakeEvent,
    User,
    new_id,
    utc_now,
)
from app.teams import wakeup
from app.teams.schema import (
    AwardOverrideRequest,
    TeamBlackboardPromoteRequest,
    TeamKnowledgeSelection,
    TeamSharedKnowledgeCreate,
)
from app.teams.service import (
    bind_team_knowledge_base,
    member_concurrency,
    record_task_event,
)
from app.teams.sweeper import (
    DEFAULT_TASK_TIMEOUT_MINUTES,
    sweep_timed_out_tasks,
    task_timeout_minutes,
)
from app.teams.wakeup import enqueue_wake_event


def _foreign_user() -> User:
    return User(
        id="user_foreign",
        tenant_id="tenant_else",
        username="foreign",
        role="admin",
        password_hash="test",
    )


def _make_assigned_task(
    db: Session, team: Team, *, title: str, assignee: str, status: str = "pending"
) -> TeamTask:
    task = TeamTask(
        team_id=team.id,
        tenant_id=team.tenant_id,
        title=title,
        status=status,
        created_by_user_id="user_admin",
        created_by_tl=True,
        assignee_agent_id=assignee,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------- 成员串行排队 ----------


def test_member_needs_input_escalates_with_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """成员 awaiting_user(如索要合同文本):保留提问并升级给人,不进入 TL 验收。"""
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_assigned_task(db, team, title="合同审查", assignee="agent_a")
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(
            wakeup, "run_agent_turn", lambda *a, **kw: "请提供合同文本,我才能逐条审查"
        )
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *a, **kw: "needs_input")

        finished = _run_wake(db, wake.id)

        assert finished.status == "done"
        db.refresh(task)
        assert task.status == "escalated"
        assert task.report_json["needs_input"] is True
        assert "合同文本" in task.report_json["full_reply"]
        events = _events(db, task.id, "task_needs_input")
        assert len(events) == 1
        assert "合同文本" in events[0].payload_json["question"]
        # 不触发 TL 验收唤醒
        assert _pending_wakes(db, "task_report") == []


def test_member_concurrency_config_fallback() -> None:
    base = {"tenant_id": "t", "name": "n", "owner_user_id": "u"}
    assert member_concurrency(Team(**base)) == 1
    assert member_concurrency(Team(**base, config_json={"member_concurrency": 3})) == 3
    # 非数字/非正数配置回退默认 1
    assert member_concurrency(Team(**base, config_json={"member_concurrency": "abc"})) == 1
    assert member_concurrency(Team(**base, config_json={"member_concurrency": 0})) == 1
    # 非 dict 配置(绕过校验直写)同样回退默认
    weird = Team(**base)
    weird.config_json = "x"
    assert member_concurrency(weird) == 1


def test_execution_wake_queues_when_member_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        _make_assigned_task(db, team, title="执行中", assignee="agent_a", status="in_progress")
        queued_task = _make_assigned_task(db, team, title="排队任务", assignee="agent_a")
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": queued_task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(
            wakeup, "run_agent_turn",
            lambda *a, **kw: pytest.fail("排队中的唤醒不应执行"),
        )

        finished = _run_wake(db, wake.id)

        # 排队:事件保持 pending,记 wake_queued 审计,任务不动
        assert finished.status == "pending"
        queued = _events(db, queued_task.id, "wake_queued")
        assert len(queued) == 1
        assert queued[0].actor_type == "system"
        assert queued[0].payload_json["wake_event_id"] == wake.id
        db.refresh(queued_task)
        assert queued_task.status == "pending"


def test_member_queue_drains_after_task_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        first = _make_assigned_task(db, team, title="先做", assignee="agent_a")
        second = _make_assigned_task(db, team, title="后做", assignee="agent_a")
        started = _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *a, **kw: "完成报告")
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *a, **kw: "completed")
        wake1 = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": first.id},
        )
        wake2 = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": second.id},
        )
        db.commit()

        _run_wake(db, wake1.id)  # 第一个任务执行完成 -> review

        db.refresh(first)
        assert first.status == "review"
        # 终态后自动出队:最老的 pending 执行类唤醒被拉起(TL 验收唤醒在前)
        assert started[-1] == wake2.id
        assert _events(db, second.id, "wake_queued") == []

        # 出队的唤醒此时可正常执行(成员已空闲)
        _run_wake(db, wake2.id)
        db.refresh(second)
        assert second.status == "review"
        assert db.get(TeamWakeEvent, wake2.id).status == "done"


def test_member_concurrency_config_allows_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"member_concurrency": 2})
        _make_assigned_task(db, team, title="执行中", assignee="agent_a", status="in_progress")
        second = _make_assigned_task(db, team, title="并发任务", assignee="agent_a")
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": second.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *a, **kw: "完成报告")
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *a, **kw: "completed")

        finished = _run_wake(db, wake.id)

        # 上限 2:已有 1 个 in_progress 仍可直接执行,不排队
        assert finished.status == "done"
        db.refresh(second)
        assert second.status == "review"
        assert _events(db, second.id, "wake_queued") == []


def test_bid_wake_bypasses_member_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        _make_assigned_task(db, team, title="执行中", assignee="agent_a", status="in_progress")
        bidding_task = _make_pool_task(db, team, title="调研")
        bidding_task.status = "bidding"
        db.add(bidding_task)
        db.commit()
        wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="bid_request", payload={"task_id": bidding_task.id, "round": 1},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *a, **kw: _bid_reply("甲的方案"))

        finished = _run_wake(db, wake.id)

        # 竞标属轻量 turn:不受执行排队影响,照跑照记
        assert finished.status == "done"
        bids = db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == bidding_task.id)).all()
        assert len(bids) == 1
        assert _events(db, bidding_task.id, "bid_submitted")
        assert _events(db, bidding_task.id, "wake_queued") == []


# ---------- 超时清扫 ----------


def _make_timed_task(
    db: Session, team: Team, *, status: str, minutes_ago: float, assignee: str | None = None
) -> TeamTask:
    task = TeamTask(
        team_id=team.id,
        tenant_id=team.tenant_id,
        title=f"{status}任务",
        status=status,
        assignee_agent_id=assignee,
    )
    task.updated_at = utc_now() - timedelta(minutes=minutes_ago)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_sweep_escalates_timed_out_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        started = _stub_start_wakeup(monkeypatch)
        stale = _make_timed_task(db, team, status="in_progress", minutes_ago=60, assignee="agent_a")
        stale_wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": stale.id},
        )
        # 同成员的另一个排队唤醒:超时释放额度后应被出队拉起
        queued_task = _make_assigned_task(db, team, title="排队任务", assignee="agent_a")
        queued_wake = enqueue_wake_event(
            db, team=team, target_agent_id="agent_a",
            trigger_type="task_assigned", payload={"task_id": queued_task.id},
        )
        fresh = _make_timed_task(db, team, status="review", minutes_ago=10, assignee="agent_b")
        done = _make_timed_task(db, team, status="done", minutes_ago=120, assignee="agent_b")
        db.commit()

        swept = sweep_timed_out_tasks(db)

        assert [task.id for task in swept] == [stale.id]
        db.refresh(stale)
        assert stale.status == "escalated"
        escalated = _events(db, stale.id, "task_escalated")
        assert escalated[0].actor_type == "system"
        assert escalated[0].payload_json["reason"] == "timeout"
        assert escalated[0].payload_json["from_status"] == "in_progress"
        # 关联 pending 唤醒标记 failed(error=timeout)
        failed_wake = db.get(TeamWakeEvent, stale_wake.id)
        assert failed_wake.status == "failed"
        assert failed_wake.error == "timeout"
        # in_progress 超时释放执行额度,出队该成员的排队唤醒
        assert started == [queued_wake.id]
        # 未超时的 review 与终态 done 不误伤
        db.refresh(fresh)
        db.refresh(done)
        assert fresh.status == "review"
        assert done.status == "done"
        assert db.get(TeamWakeEvent, queued_wake.id).status == "pending"


def test_sweep_respects_team_timeout_config() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"task_timeout_minutes": 5})
        stale = _make_timed_task(db, team, status="bidding", minutes_ago=10)

        swept = sweep_timed_out_tasks(db)

        assert [task.id for task in swept] == [stale.id]
        db.refresh(stale)
        assert stale.status == "escalated"


def test_task_timeout_minutes_config_fallback() -> None:
    base = {"tenant_id": "t", "name": "n", "owner_user_id": "u"}
    assert task_timeout_minutes(Team(**base)) == DEFAULT_TASK_TIMEOUT_MINUTES
    assert task_timeout_minutes(Team(**base, config_json={"task_timeout_minutes": 45})) == 45.0
    # 非数字/非正数配置回退默认
    assert task_timeout_minutes(Team(**base, config_json={"task_timeout_minutes": "abc"})) == 30.0
    assert task_timeout_minutes(Team(**base, config_json={"task_timeout_minutes": -5})) == 30.0
    # 非 dict 配置(绕过校验直写)同样回退默认
    weird = Team(**base)
    weird.config_json = "x"
    assert task_timeout_minutes(weird) == 30.0


# ---------- 团队级审计与统一线程 ----------


def test_team_events_endpoint_aggregates_desc_with_title() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        admin = _admin_user()
        task1 = _make_pool_task(db, team, title="任务一")
        task2 = _make_pool_task(db, team, title="任务二")
        for task in (task1, task2):
            record_task_event(
                db, team_id=team.id, task_id=task.id,
                actor_type="user", actor_id="user_admin", event_type="task_created",
            )
        db.commit()

        events = teams_api.list_team_events(team.id, "tenant_demo", 50, db, admin)

        assert len(events) == 2
        assert events[0].created_at >= events[1].created_at
        titles = {item.task_id: item.task_title for item in events}
        assert titles == {task1.id: "任务一", task2.id: "任务二"}

        # limit 生效
        limited = teams_api.list_team_events(team.id, "tenant_demo", 1, db, admin)
        assert len(limited) == 1
        # 其他租户的用户 -> 403
        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_events(team.id, "tenant_demo", 50, db, _foreign_user())
        assert exc_info.value.status_code == 403


def test_team_threads_aggregates_tl_chat_and_tasks() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        admin = _admin_user()
        tl_session = ChatSession(
            id=new_id("session"), tenant_id="tenant_demo", user_id="user_admin",
            agent_id="agent_tl", title=f"团队 {team.name} · TL 对话", status="active",
            team_id=team.id,
        )
        # 非 TL 对话标题的同 agent 会话不入选
        other_session = ChatSession(
            id=new_id("session"), tenant_id="tenant_demo", user_id="user_admin",
            agent_id="agent_tl", title="日常闲聊", status="active",
            team_id=team.id,
        )
        task_session = ChatSession(
            id=new_id("session"), tenant_id="tenant_demo", user_id="user_admin",
            agent_id="agent_a", title="团队任务:调研", status="active",
        )
        db.add(tl_session)
        db.add(other_session)
        db.add(task_session)
        task = _make_assigned_task(db, team, title="调研", assignee="agent_a", status="review")
        task.session_id = task_session.id
        task.updated_at = utc_now() + timedelta(minutes=1)  # 任务线程排在最前
        db.add(task)
        db.commit()

        threads = teams_api.list_team_threads("tenant_demo", db, admin)

        assert [(item.kind) for item in threads] == ["task", "tl_chat"]
        task_thread = threads[0]
        assert task_thread.team_id == team.id
        assert task_thread.team_name == team.name
        assert task_thread.session_id == task_session.id
        assert task_thread.task_id == task.id
        assert task_thread.title == "调研"
        assert task_thread.task_status == "review"
        tl_thread = threads[1]
        assert tl_thread.session_id == tl_session.id
        assert tl_thread.task_id is None
        assert tl_thread.task_status is None
        assert "TL 对话" in tl_thread.title
        # 按 updated_at 倒序
        assert threads[0].updated_at >= threads[1].updated_at

        # 其他租户的用户 -> 403
        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_team_threads("tenant_demo", db, _foreign_user())
        assert exc_info.value.status_code == 403


# ---------- 黑板沉淀到知识库 ----------


def test_blackboard_promote_creates_ingest_job_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        admin = _admin_user()
        binding = bind_team_knowledge_base(
            db,
            team=team,
            selection=TeamKnowledgeSelection(
                create_shared=TeamSharedKnowledgeCreate(name="团队黑板共享库"),
                is_default=True,
            ),
            actor_user_id=admin.id,
        )
        task = _make_pool_task(db, team, title="来源任务")
        entry = TeamBlackboardEntry(
            team_id=team.id, tenant_id=team.tenant_id,
            content="竞品 A 定价 99 元", tags_json=["pricing"],
            source_type="member", source_agent_id="agent_a", source_task_id=task.id,
            citation_json={"task_id": task.id, "task_title": task.title},
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        enqueued: list[tuple[tuple, dict]] = []
        monkeypatch.setattr(
            teams_api, "enqueue_async_job",
            lambda *args, **kw: enqueued.append((args, kw)),
        )

        resp = teams_api.promote_blackboard_entry(
            team.id, entry.id,
            TeamBlackboardPromoteRequest(tenant_id="tenant_demo"),
            db, admin,
        )

        assert resp.already_promoted is False
        assert resp.knowledge_base_id == binding.knowledge_base_id
        job = db.get(KnowledgeIngestJob, resp.ingest_job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.tenant_id == "tenant_demo"
        assert job.knowledge_base_id == resp.knowledge_base_id
        # 原始资料 markdown:含条目内容、tags、来源团队/任务标注
        markdown = base64.b64decode(job.metadata_json["content_base64"]).decode("utf-8")
        assert "竞品 A 定价 99 元" in markdown
        assert "pricing" in markdown
        assert team.name in markdown
        assert task.title in markdown
        # citation 回写
        db.refresh(entry)
        assert entry.citation_json["knowledge_base_id"] == resp.knowledge_base_id
        assert entry.citation_json["ingest_job_id"] == job.id
        assert entry.citation_json["task_id"] == task.id  # 既有引用保留
        # 异步执行复用知识库 ingest 队列
        assert len(enqueued) == 1
        assert enqueued[0][0][0] == "knowledge_ingest"
        assert enqueued[0][0][2] == job.id

        # 重复 promote:返回既有引用,不重复建 job
        resp2 = teams_api.promote_blackboard_entry(
            team.id, entry.id,
            TeamBlackboardPromoteRequest(tenant_id="tenant_demo"),
            db, admin,
        )
        assert resp2.already_promoted is True
        assert resp2.ingest_job_id == job.id
        assert resp2.knowledge_base_id == resp.knowledge_base_id
        assert len(enqueued) == 1
        assert len(db.exec(select(KnowledgeIngestJob)).all()) == 1

        # 非 owner/admin 不可沉淀
        with pytest.raises(HTTPException) as exc_info:
            teams_api.promote_blackboard_entry(
                team.id, entry.id,
                TeamBlackboardPromoteRequest(tenant_id="tenant_demo"),
                db, _admin_user_other(),
            )
        assert exc_info.value.status_code == 403


# ---------- 迟到竞标语义 ----------


def test_late_bid_wake_after_override_records_bid_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)
        calls: list[str] = []

        def fake_turn(*args, **kw):
            calls.append(kw["agent"].id)
            return _bid_reply("方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)
        wakeup.start_bidding(db, team, task)

        # 人改判:任务离开 bidding
        teams_api.override_task_award(
            team.id, task.id,
            AwardOverrideRequest(tenant_id="tenant_demo", agent_id="agent_c"),
            db, _admin_user(),
        )
        calls.clear()

        late_wake = _pending_wakes(db, "bid_request")[0]
        finished = _run_wake(db, late_wake.id)

        # 迟到竞标:记 bid_skipped 直接返回,不执行 turn、不落 bid、不记 bid_submitted
        assert finished.status == "done"
        assert calls == []
        skipped = _events(db, task.id, "bid_skipped")
        assert len(skipped) == 1
        assert skipped[0].payload_json["task_status"] == "pending"
        assert db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == task.id)).all() == []
        assert _events(db, task.id, "bid_submitted") == []
