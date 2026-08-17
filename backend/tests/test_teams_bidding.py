from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from test_teams_api import (
    _admin_user,
    _admin_user_other,
    _stub_start_wakeup,
    _test_session,
)

from app.api import teams as teams_api
from app.db.models import (
    AgentProfile,
    Team,
    TeamTask,
    TeamTaskBid,
    TeamTaskEvent,
    TeamWakeEvent,
    Tenant,
)
from app.teams import wakeup
from app.teams.schema import AwardOverrideRequest, TeamTaskCreateRequest
from app.teams.service import (
    add_member,
    candidate_hp,
    create_team,
    parse_bid,
    parse_bid_award,
    parse_bid_scores,
    select_bid_candidates,
)


def _seed_pool_team(db: Session, *, config: dict | None = None) -> Team:
    """TL(也带匹配标签)+ 4 名能力标签各异的成员,用于候选选择与竞标流程。"""
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        AgentProfile(
            id="agent_tl", tenant_id="tenant_demo", name="TL",
            metadata_json={"expertise_tags": ["调研", "竞品"]},
        )
    )
    db.add(
        AgentProfile(
            id="agent_a", tenant_id="tenant_demo", name="甲",
            metadata_json={"expertise_tags": ["调研", "竞品"]},
        )
    )
    db.add(
        AgentProfile(
            id="agent_b", tenant_id="tenant_demo", name="乙",
            metadata_json={"expertise_tags": ["调研"]},
        )
    )
    db.add(
        AgentProfile(
            id="agent_c", tenant_id="tenant_demo", name="丙",
            metadata_json={"expertise_tags": ["定价"]},
        )
    )
    db.add(AgentProfile(id="agent_d", tenant_id="tenant_demo", name="丁"))
    db.commit()
    team = create_team(
        db,
        tenant_id="tenant_demo",
        name="竞标团队",
        description=None,
        owner_user_id="user_admin",
        config=config,
    )
    add_member(db, team, agent_id="agent_tl", role="leader")
    for agent_id in ("agent_a", "agent_b", "agent_c", "agent_d"):
        add_member(db, team, agent_id=agent_id)
    return team


def _make_pool_task(
    db: Session, team: Team, *, title: str = "调研竞品定价", description: str | None = "输出调研报告"
) -> TeamTask:
    task = TeamTask(
        team_id=team.id,
        tenant_id=team.tenant_id,
        title=title,
        description=description,
        status="pending",
        created_by_user_id="user_admin",
        created_by_tl=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _run_wake(db: Session, wake_id: str) -> TeamWakeEvent:
    assert wakeup.claim_wake_event(db, wake_id) is True
    event = db.get(TeamWakeEvent, wake_id)
    wakeup.execute_wake_event(db, event)
    return db.get(TeamWakeEvent, wake_id)


def _pending_wakes(db: Session, trigger_type: str) -> list[TeamWakeEvent]:
    rows = db.exec(
        select(TeamWakeEvent).where(
            TeamWakeEvent.trigger_type == trigger_type, TeamWakeEvent.status == "pending"
        )
    ).all()
    return sorted(rows, key=lambda row: row.target_agent_id)


def _events(db: Session, task_id: str, event_type: str) -> list[TeamTaskEvent]:
    return list(
        db.exec(
            select(TeamTaskEvent).where(
                TeamTaskEvent.task_id == task_id, TeamTaskEvent.event_type == event_type
            )
        ).all()
    )


def _bids(db: Session, task_id: str) -> list[TeamTaskBid]:
    return list(
        db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == task_id)).all()
    )


def _bid_reply(plan: str) -> str:
    return f'竞标陈述\n```json\n{{"bid": {{"plan": "{plan}", "confidence": "high"}}}}\n```'


def _award_reply(winner: str) -> str:
    return (
        "```json\n"
        f'{{"bid_award": {{"winner_agent_id": "{winner}", '
        '"scores": {"agent_a": {"score": 8.5, "rationale": "理解到位"}, '
        '"agent_b": {"score": 7.0, "rationale": "方案一般"}}, '
        '"comment": "甲更匹配"}}\n```'
    )


def _score_reply(*agent_scores: tuple[str, float]) -> str:
    inner = ", ".join(
        f'"{agent_id}": {{"score": {score}, "rationale": "打分理由"}}'
        for agent_id, score in agent_scores
    )
    return f"```json\n{{\"bid_scores\": {{{inner}}}}}\n```"


# ---------- 结构化输出解析 ----------


def test_parse_bid_block_and_fallback() -> None:
    reply = '说明\n```json\n{"bid": {"plan": "先调研再写", "estimated_cost": "2h"}}\n```'
    assert parse_bid(reply) == {"plan": "先调研再写", "estimated_cost": "2h"}
    assert parse_bid("没有代码块") is None
    assert parse_bid('```json\n{"bid": {"plan": ""}}\n```') is None


def test_parse_bid_award_requires_candidate_winner() -> None:
    candidates = {"agent_a", "agent_b"}
    reply = _award_reply("agent_a")
    award = parse_bid_award(reply, candidates)
    assert award is not None
    assert award["winner_agent_id"] == "agent_a"
    assert award["scores"]["agent_a"] == {"score": 8.5, "rationale": "理解到位"}
    assert award["comment"] == "甲更匹配"
    # winner 不在候选集 -> 视为未解析
    assert parse_bid_award(_award_reply("agent_outside"), candidates) is None
    assert parse_bid_award("没有块", candidates) is None


def test_parse_bid_scores() -> None:
    candidates = {"agent_a", "agent_b"}
    scores = parse_bid_scores(_score_reply(("agent_a", 9.0), ("agent_b", 8.0)), candidates)
    assert scores == {
        "agent_a": {"score": 9.0, "rationale": "打分理由"},
        "agent_b": {"score": 8.0, "rationale": "打分理由"},
    }
    # 无块/无合法分数 -> None(交由纠错重试)
    assert parse_bid_scores("没有块", candidates) is None
    assert parse_bid_scores(_score_reply(("agent_outside", 9.0)), candidates) is None
    # 分数截断到 0-10
    clamped = parse_bid_scores(_score_reply(("agent_a", 12.0)), candidates)
    assert clamped is not None
    assert clamped["agent_a"]["score"] == 10.0


# ---------- 血条(HP)计算 ----------


def _scored_bid(agent_id: str, round_: int, score: float | None) -> TeamTaskBid:
    return TeamTaskBid(
        task_id="task_1",
        team_id="team_1",
        tenant_id="tenant_demo",
        agent_id=agent_id,
        round=round_,
        kind="statement" if round_ == 1 else "rebuttal",
        content="方案",
        score=score,
    )


def test_candidate_hp_calculation() -> None:
    bids = [
        _scored_bid("agent_a", 1, 10.0),  # 满分不扣
        _scored_bid("agent_b", 1, 8.0),  # 扣 6
        _scored_bid("agent_b", 2, 5.0),  # 再扣 15,累计 79
        _scored_bid("agent_c", 1, 0.0),
        _scored_bid("agent_c", 2, 0.0),
        _scored_bid("agent_c", 3, 0.0),
        _scored_bid("agent_c", 4, 0.0),  # 扣 120,下限 0
        _scored_bid("agent_d", 1, None),  # 未打分不计
    ]
    hp = candidate_hp(bids)
    assert hp["agent_a"] == 100
    assert hp["agent_b"] == 79
    assert hp["agent_c"] == 0
    assert "agent_d" not in hp


# ---------- 候选选择 ----------


def test_select_candidates_tag_ranking_excludes_tl_and_caps3() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team)  # 标题:调研竞品定价
        candidates = select_bid_candidates(db, team, task)
        # agent_a(调研+竞品=2) 第一;调研/定价 各 1 分按 agent_id 序;TL 排除;封顶 3
        assert candidates == ["agent_a", "agent_b", "agent_c"]
        assert "agent_tl" not in candidates


def test_select_candidates_zero_match_fallback() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="完全无关的xyz任务", description=None)
        # 全员 0 分 -> 除 TL 外全部成员,仍封顶 3
        assert select_bid_candidates(db, team, task) == ["agent_a", "agent_b", "agent_c"]


def test_start_bidding_without_candidates_escalates() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(AgentProfile(id="agent_tl", tenant_id="tenant_demo", name="TL"))
        db.commit()
        team = create_team(
            db, tenant_id="tenant_demo", name="光杆团队",
            description=None, owner_user_id="user_admin",
        )
        add_member(db, team, agent_id="agent_tl", role="leader")
        task = _make_pool_task(db, team)

        wakeup.start_bidding(db, team, task)

        db.refresh(task)
        assert task.status == "escalated"
        escalated = _events(db, task.id, "task_escalated")
        assert len(escalated) == 1
        assert "无候选" in escalated[0].payload_json["reason"]
        assert db.exec(select(TeamWakeEvent)).all() == []


def test_start_bidding_enqueues_round1_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")  # 候选: agent_a, agent_b
        started = _stub_start_wakeup(monkeypatch)

        wakeup.start_bidding(db, team, task)

        db.refresh(task)
        assert task.status == "bidding"
        started_events = _events(db, task.id, "task_bidding_started")
        assert started_events[0].payload_json["candidate_agent_ids"] == ["agent_a", "agent_b"]
        wakes = _pending_wakes(db, "bid_request")
        assert [wake.target_agent_id for wake in wakes] == ["agent_a", "agent_b"]
        assert all(wake.payload_json["round"] == 1 for wake in wakes)
        assert started == [wake.id for wake in wakes]


# ---------- 陈述/反驳/裁决全流程 ----------


def _reach_judge(
    db: Session,
    team: Team,
    task: TeamTask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快进(辩论关闭):两名候选完成陈述后,bid_judge(award)唤醒处于 pending。"""
    _stub_start_wakeup(monkeypatch)
    replies = {"agent_a": _bid_reply("甲的方案"), "agent_b": "乙的纯文本方案(无代码块)"}
    monkeypatch.setattr(
        wakeup, "run_agent_turn", lambda *args, **kw: replies[kw["agent"].id]
    )
    wakeup.start_bidding(db, team, task)
    for wake in _pending_wakes(db, "bid_request"):
        assert wake.payload_json["round"] == 1
        _run_wake(db, wake.id)


def test_bidding_full_flow_three_rounds_hp(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 3 轮血条赛制:陈述 -> 打分 -> 反驳 -> 打分 -> 反驳 -> 裁决。"""
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        started = _stub_start_wakeup(monkeypatch)
        messages: dict[str, list[str]] = {"agent_a": [], "agent_b": [], "agent_tl": []}
        replies = {"agent_a": _bid_reply("甲的方案"), "agent_b": "乙的纯文本方案(无代码块)"}

        def fake_turn(*args, **kw):
            agent_id = kw["agent"].id
            messages[agent_id].append(kw["message"])
            if agent_id == "agent_tl":
                if "bid_scores" in kw["message"]:
                    return _score_reply(("agent_a", 9.0), ("agent_b", 8.0))
                return _award_reply("agent_a")
            return replies[agent_id]

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)

        wakeup.start_bidding(db, team, task)
        # 陈述轮:第一个候选完成后不推进,第二个完成后入队第 1 轮打分
        first, second = _pending_wakes(db, "bid_request")
        _run_wake(db, first.id)
        assert _pending_wakes(db, "bid_judge") == []
        assert len(_pending_wakes(db, "bid_request")) == 1
        _run_wake(db, second.id)

        db.refresh(task)
        assert task.status == "bidding"
        bids = _bids(db, task.id)
        assert {(bid.agent_id, bid.round, bid.kind) for bid in bids} == {
            ("agent_a", 1, "statement"),
            ("agent_b", 1, "statement"),
        }
        contents = {bid.agent_id: bid.content for bid in bids}
        assert contents["agent_a"] == "甲的方案"  # 有 bid 块用 plan
        assert contents["agent_b"] == "乙的纯文本方案(无代码块)"  # 无块用整条回复
        assert len(_events(db, task.id, "bid_submitted")) == 2

        # 第 1 轮打分:分数写回该轮 bid
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].target_agent_id == "agent_tl"
        assert judge[0].payload_json["mode"] == "score"
        assert judge[0].payload_json["round"] == 1
        _run_wake(db, judge[0].id)
        scores = {bid.agent_id: bid.score for bid in _bids(db, task.id)}
        assert scores == {"agent_a": 9.0, "agent_b": 8.0}
        scored_events = _events(db, task.id, "bid_scored")
        assert len(scored_events) == 1
        assert scored_events[0].payload_json["round"] == 1

        # 第 2 轮反驳:消息附各候选血条与上一轮其他候选的发言
        rebuttal_wakes = _pending_wakes(db, "bid_request")
        assert len(rebuttal_wakes) == 2
        assert all(wake.payload_json["round"] == 2 for wake in rebuttal_wakes)
        for wake in rebuttal_wakes:
            _run_wake(db, wake.id)
        assert any("HP=97" in msg for msg in messages["agent_a"])  # 甲 9 分 -> 97
        assert any("HP=94" in msg for msg in messages["agent_a"])  # 乙 8 分 -> 94
        assert any("乙的纯文本方案" in msg for msg in messages["agent_a"])
        assert any("反驳轮" in msg for msg in messages["agent_a"])
        bids = _bids(db, task.id)
        assert len([bid for bid in bids if bid.round == 2 and bid.kind == "rebuttal"]) == 2

        # 第 2 轮打分
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "score"
        assert judge[0].payload_json["round"] == 2
        _run_wake(db, judge[0].id)

        # 第 3 轮反驳(末轮)
        round3_wakes = _pending_wakes(db, "bid_request")
        assert len(round3_wakes) == 2
        assert all(wake.payload_json["round"] == 3 for wake in round3_wakes)
        for wake in round3_wakes:
            _run_wake(db, wake.id)
        bids = _bids(db, task.id)
        assert len([bid for bid in bids if bid.round == 3 and bid.kind == "rebuttal"]) == 2

        # 末轮已齐:直接裁决(不再打分)
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "award"
        _run_wake(db, judge[0].id)

        db.refresh(task)
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_a"
        awarded = _events(db, task.id, "task_awarded")
        assert awarded[0].payload_json["winner_agent_id"] == "agent_a"
        assert awarded[0].payload_json["comment"] == "甲更匹配"
        # 前两轮分数保留 score 模式打分;末轮 bid 由裁决分数补写
        round_scores = {(bid.agent_id, bid.round): bid.score for bid in _bids(db, task.id)}
        assert round_scores[("agent_a", 1)] == 9.0
        assert round_scores[("agent_a", 2)] == 9.0
        assert round_scores[("agent_a", 3)] == 8.5
        assert round_scores[("agent_b", 3)] == 7.0
        # 裁决消息包含候选 agent_id、陈述与血条
        judge_msg = messages["agent_tl"][-1]
        assert "agent_id=agent_a" in judge_msg and "甲的方案" in judge_msg
        assert "HP=" in judge_msg
        # 中标者走增量 1 的 task_assigned 链路
        assigned_wakes = _pending_wakes(db, "task_assigned")
        assert len(assigned_wakes) == 1
        assert assigned_wakes[0].target_agent_id == "agent_a"
        assert started[-1] == assigned_wakes[0].id

        # 任务详情带竞标记录,按 round/created_at 排序
        detail = teams_api.get_team_task(team.id, task.id, "tenant_demo", db, _admin_user())
        assert [(bid.round, bid.kind) for bid in detail.bids] == [
            (1, "statement"),
            (1, "statement"),
            (2, "rebuttal"),
            (2, "rebuttal"),
            (3, "rebuttal"),
            (3, "rebuttal"),
        ]
        names = {bid.agent_id: bid.agent_name for bid in detail.bids}
        assert names["agent_a"] == "甲"
        assert detail.bids[0].score == 9.0
        assert detail.bids[0].score_rationale == "打分理由"


def test_bidding_elimination_skips_to_award(monkeypatch: pytest.MonkeyPatch) -> None:
    """血条归零淘汰:5 轮配置下乙方四轮 0 分 HP 归零,审计淘汰并提前进裁决。"""
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 5})
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)

        def fake_turn(*args, **kw):
            agent_id = kw["agent"].id
            if agent_id == "agent_tl":
                if "bid_scores" in kw["message"]:
                    return _score_reply(("agent_a", 10.0), ("agent_b", 0.0))
                return _award_reply("agent_a")
            return _bid_reply(f"{agent_id}的方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)

        wakeup.start_bidding(db, team, task)
        for wake in _pending_wakes(db, "bid_request"):
            _run_wake(db, wake.id)
        # 第 1-4 轮:打分后乙 HP 70 -> 40 -> 10 -> 0
        for expected_round in (1, 2, 3, 4):
            judge = _pending_wakes(db, "bid_judge")
            assert len(judge) == 1
            assert judge[0].payload_json["mode"] == "score"
            assert judge[0].payload_json["round"] == expected_round
            _run_wake(db, judge[0].id)
            if expected_round < 4:
                wakes = _pending_wakes(db, "bid_request")
                assert len(wakes) == 2
                assert all(wake.payload_json["round"] == expected_round + 1 for wake in wakes)
                for wake in wakes:
                    _run_wake(db, wake.id)

        # 乙淘汰:审计 bid_eliminated,不再入队第 5 轮,直接进裁决
        eliminated = _events(db, task.id, "bid_eliminated")
        assert [row.actor_id for row in eliminated] == ["agent_b"]
        assert eliminated[0].payload_json["hp"] == 0
        assert _pending_wakes(db, "bid_request") == []
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "award"
        _run_wake(db, judge[0].id)

        db.refresh(task)
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_a"
        # 乙的 bid 保留分数,HP 由前端从 bids 计算
        assert candidate_hp(_bids(db, task.id))["agent_b"] == 0


def test_bid_score_fallback_five_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """打分解析失败(含纠错重试)兜底 5 分并审计,不阻塞竞标流程。"""
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "collect_turn_reply_fragments", lambda *a, **kw: [])

        def fake_turn(*args, **kw):
            if kw["agent"].id == "agent_tl":
                return "我打不了分"  # 首轮与纠错轮都无打分块
            return _bid_reply(f"{kw['agent'].id}的方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)

        wakeup.start_bidding(db, team, task)
        for wake in _pending_wakes(db, "bid_request"):
            _run_wake(db, wake.id)
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        _run_wake(db, judge[0].id)

        # 兜底:该轮全员 5 分,审计 fallback 与 unparsed,流程推进到第 2 轮
        scores = {bid.agent_id: bid.score for bid in _bids(db, task.id)}
        assert scores == {"agent_a": 5.0, "agent_b": 5.0}
        assert len(_events(db, task.id, "bid_score_fallback")) == 1
        assert len(_events(db, task.id, "bid_score_unparsed")) == 1
        db.refresh(task)
        assert task.status == "bidding"
        wakes = _pending_wakes(db, "bid_request")
        assert len(wakes) == 2
        assert all(wake.payload_json["round"] == 2 for wake in wakes)


def test_bidding_config_one_statement_then_award(monkeypatch: pytest.MonkeyPatch) -> None:
    """config=1 兼容旧行为:陈述后直接裁决(无打分、无反驳轮)。"""
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 1})
        task = _make_pool_task(db, team, title="调研")
        _reach_judge(db, team, task, monkeypatch)

        assert all(bid.kind == "statement" for bid in _bids(db, task.id))
        assert _pending_wakes(db, "bid_request") == []
        judge = _pending_wakes(db, "bid_judge")
        assert len(judge) == 1
        assert judge[0].payload_json["mode"] == "award"


def test_bidding_skips_rebuttal_when_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 0})
        task = _make_pool_task(db, team, title="调研")
        _reach_judge(db, team, task, monkeypatch)

        assert all(bid.kind == "statement" for bid in _bids(db, task.id))
        judge_wakes = _pending_wakes(db, "bid_judge")
        assert len(judge_wakes) == 1


def test_bidding_partial_failure_advances_with_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        started = _stub_start_wakeup(monkeypatch)

        def fake_turn(*args, **kw):
            if kw["agent"].id == "agent_a":
                raise RuntimeError("LLM 不可用")
            return _bid_reply("乙的方案")

        monkeypatch.setattr(wakeup, "run_agent_turn", fake_turn)

        wakeup.start_bidding(db, team, task)
        wakes = _pending_wakes(db, "bid_request")
        failed_wake = _run_wake(db, wakes[0].id)  # agent_a 失败
        assert failed_wake.status == "failed"
        db.refresh(task)
        # 候选失败不升级任务(区别于任务执行失败)
        assert task.status == "bidding"
        failed_events = _events(db, task.id, "bid_failed")
        assert len(failed_events) == 1
        assert failed_events[0].actor_id == "agent_a"
        assert _events(db, task.id, "task_escalated") == []

        _run_wake(db, wakes[1].id)  # agent_b 成功 -> 有效陈述 1 <2,直接裁决
        db.refresh(task)
        assert task.status == "bidding"
        assert _pending_wakes(db, "bid_request") == []
        judge_wakes = _pending_wakes(db, "bid_judge")
        assert len(judge_wakes) == 1
        assert started[-1] == judge_wakes[0].id


def test_bidding_no_valid_statements_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        _stub_start_wakeup(monkeypatch)

        def boom(*args, **kw):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(wakeup, "run_agent_turn", boom)
        wakeup.start_bidding(db, team, task)
        for wake in _pending_wakes(db, "bid_request"):
            _run_wake(db, wake.id)

        db.refresh(task)
        assert task.status == "escalated"
        escalated = _events(db, task.id, "task_escalated")
        assert "无人应标" in escalated[0].payload_json["reason"]
        assert _pending_wakes(db, "bid_judge") == []


# ---------- TL 裁决解析路径 ----------


def test_bid_judge_parses_award_from_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 0})
        task = _make_pool_task(db, team, title="调研")
        _reach_judge(db, team, task, monkeypatch)
        # 最终回复被改写丢块,裁决块只存在于 frame 级 reply_fragment
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: "我选甲(已改写)")
        monkeypatch.setattr(
            wakeup, "collect_turn_reply_fragments",
            lambda *args, **kw: [_award_reply("agent_a")],
        )

        _run_wake(db, _pending_wakes(db, "bid_judge")[0].id)

        db.refresh(task)
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_a"


def test_bid_judge_repair_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 0})
        task = _make_pool_task(db, team, title="调研")
        _reach_judge(db, team, task, monkeypatch)
        monkeypatch.setattr(wakeup, "collect_turn_reply_fragments", lambda *a, **kw: [])
        tl_replies = iter(["我还要想想", _award_reply("agent_b")])
        monkeypatch.setattr(
            wakeup, "run_agent_turn", lambda *args, **kw: next(tl_replies)
        )

        _run_wake(db, _pending_wakes(db, "bid_judge")[0].id)

        db.refresh(task)
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_b"
        assert len(_events(db, task.id, "bid_award_unparsed")) == 1


def test_bid_judge_invalid_winner_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db, config={"bid_rebuttal_rounds": 0})
        task = _make_pool_task(db, team, title="调研")
        _reach_judge(db, team, task, monkeypatch)
        monkeypatch.setattr(wakeup, "collect_turn_reply_fragments", lambda *a, **kw: [])
        # 首轮与纠错轮都给出非候选 winner
        monkeypatch.setattr(
            wakeup, "run_agent_turn", lambda *args, **kw: _award_reply("agent_outside")
        )

        _run_wake(db, _pending_wakes(db, "bid_judge")[0].id)

        db.refresh(task)
        assert task.status == "escalated"
        escalated = _events(db, task.id, "task_escalated")
        assert "裁决失败" in escalated[0].payload_json["reason"]
        assert task.assignee_agent_id is None


# ---------- 人推翻判罚 ----------


def test_award_override_during_bidding_and_late_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team, title="调研")
        started = _stub_start_wakeup(monkeypatch)
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *a, **kw: _bid_reply("方案"))
        wakeup.start_bidding(db, team, task)

        result = teams_api.override_task_award(
            team.id, task.id,
            AwardOverrideRequest(
                tenant_id="tenant_demo", agent_id="agent_c", comment="人指定丙"
            ),
            db, _admin_user(),
        )
        assert result.status == "pending"
        assert result.assignee_agent_id == "agent_c"
        overridden = _events(db, task.id, "award_overridden")
        assert overridden[0].actor_type == "user"
        assert overridden[0].payload_json["previous_assignee_agent_id"] is None
        assigned_wakes = _pending_wakes(db, "task_assigned")
        assert [wake.target_agent_id for wake in assigned_wakes] == ["agent_c"]
        assert started[-1] == assigned_wakes[0].id

        # 迟到的竞标唤醒:任务已非 bidding,落地即跳过且不写 bid
        late_bid_wake = _pending_wakes(db, "bid_request")[0]
        finished = _run_wake(db, late_bid_wake.id)
        assert finished.status == "done"
        assert _bids(db, task.id) == []
        skipped = _events(db, task.id, "bid_skipped")
        assert skipped[-1].payload_json["task_status"] == "pending"

        # 迟到的裁决唤醒同样跳过
        late_judge = wakeup.enqueue_wake_event(
            db, team=team, target_agent_id="agent_tl",
            trigger_type="bid_judge", payload={"task_id": task.id},
        )
        db.commit()
        _run_wake(db, late_judge.id)
        db.refresh(task)
        assert task.status == "pending"
        assert task.assignee_agent_id == "agent_c"
        assert len(_events(db, task.id, "bid_skipped")) == 2


def test_award_override_pending_task_and_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        started = _stub_start_wakeup(monkeypatch)
        admin = _admin_user()
        # 人直派的 pending 任务(执行开始前)也可改派
        created = teams_api.create_team_task_endpoint(
            team.id,
            TeamTaskCreateRequest(
                tenant_id="tenant_demo", title="直派任务", assignee_agent_id="agent_a"
            ),
            db, admin,
        )
        result = teams_api.override_task_award(
            team.id, created.id,
            AwardOverrideRequest(tenant_id="tenant_demo", agent_id="agent_b"),
            db, admin,
        )
        assert result.status == "pending"
        assert result.assignee_agent_id == "agent_b"
        overridden = _events(db, created.id, "award_overridden")
        assert overridden[0].payload_json["previous_assignee_agent_id"] == "agent_a"
        assigned_wakes = _pending_wakes(db, "task_assigned")
        assert [wake.target_agent_id for wake in assigned_wakes] == ["agent_a", "agent_b"]
        assert started[-1] == assigned_wakes[1].id

        # 非成员不能中标
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_award(
                team.id, created.id,
                AwardOverrideRequest(tenant_id="tenant_demo", agent_id="agent_outside"),
                db, admin,
            )
        assert exc_info.value.status_code == 404

        # 非 owner/admin 不能改判
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_award(
                team.id, created.id,
                AwardOverrideRequest(tenant_id="tenant_demo", agent_id="agent_a"),
                db, _admin_user_other(),
            )
        assert exc_info.value.status_code == 403

        # 执行中/验收中的任务不可推翻判罚
        running = TeamTask(
            team_id=team.id, tenant_id=team.tenant_id, title="执行中",
            status="in_progress", assignee_agent_id="agent_a",
        )
        db.add(running)
        db.commit()
        with pytest.raises(HTTPException) as exc_info:
            teams_api.override_task_award(
                team.id, running.id,
                AwardOverrideRequest(tenant_id="tenant_demo", agent_id="agent_b"),
                db, admin,
            )
        assert exc_info.value.status_code == 409


# ---------- 人建任务端点 ----------


def test_create_task_endpoint_assign_and_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        started = _stub_start_wakeup(monkeypatch)
        admin = _admin_user()

        # 指定 assignee -> pending + task_assigned 唤醒(与 TL 直派同路)
        direct = teams_api.create_team_task_endpoint(
            team.id,
            TeamTaskCreateRequest(
                tenant_id="tenant_demo", title="直派任务",
                description="直接执行", priority="high", assignee_agent_id="agent_a",
            ),
            db, admin,
        )
        assert direct.status == "pending"
        assert direct.assignee_agent_id == "agent_a"
        assert direct.priority == "high"
        assert direct.created_by_tl is False
        created_events = _events(db, direct.id, "task_created")
        assert created_events[0].actor_type == "user"
        wakes = _pending_wakes(db, "task_assigned")
        assert [wake.target_agent_id for wake in wakes] == ["agent_a"]
        assert started == [wakes[0].id]

        # 省略 assignee -> 投池竞标
        pooled = teams_api.create_team_task_endpoint(
            team.id,
            TeamTaskCreateRequest(tenant_id="tenant_demo", title="调研"),
            db, admin,
        )
        assert pooled.status == "bidding"
        assert pooled.assignee_agent_id is None
        bid_wakes = _pending_wakes(db, "bid_request")
        assert [wake.target_agent_id for wake in bid_wakes] == ["agent_a", "agent_b"]

        # 权限与非成员校验
        with pytest.raises(HTTPException) as exc_info:
            teams_api.create_team_task_endpoint(
                team.id,
                TeamTaskCreateRequest(tenant_id="tenant_demo", title="x"),
                db, _admin_user_other(),
            )
        assert exc_info.value.status_code == 403
        with pytest.raises(HTTPException) as exc_info:
            teams_api.create_team_task_endpoint(
                team.id,
                TeamTaskCreateRequest(
                    tenant_id="tenant_demo", title="x", assignee_agent_id="agent_outside"
                ),
                db, admin,
            )
        assert exc_info.value.status_code == 404
        with pytest.raises(HTTPException) as exc_info:
            teams_api.create_team_task_endpoint(
                team.id,
                TeamTaskCreateRequest(tenant_id="tenant_demo", title="  "),
                db, admin,
            )
        assert exc_info.value.status_code == 400


def test_list_tasks_filter_bidding() -> None:
    with _test_session() as db:
        team = _seed_pool_team(db)
        task = _make_pool_task(db, team)
        task.status = "bidding"
        db.add(task)
        db.commit()
        _make_pool_task(db, team, title="另一个任务")  # pending

        admin = _admin_user()
        bidding = teams_api.list_team_tasks(team.id, "tenant_demo", "bidding", db, admin)
        assert len(bidding) == 1
        assert bidding[0].status == "bidding"
        assert bidding[0].bids == []  # 列表视图不带竞标记录
