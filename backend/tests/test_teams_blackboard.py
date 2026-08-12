from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import select
from test_teams_api import (
    _admin_user,
    _admin_user_other,
    _make_task,
    _seed_team,
    _stub_start_wakeup,
    _test_session,
)

from app.api import teams as teams_api
from app.db.models import TeamBlackboardEntry, TeamTaskEvent, TeamWakeEvent
from app.teams import wakeup
from app.teams.schema import (
    TeamBlackboardEntryArchiveRequest,
    TeamBlackboardEntryCreateRequest,
    TeamBlackboardEntryUpdateRequest,
)
from app.teams.service import (
    blackboard_context_lines,
    parse_blackboard_suggestions,
    parse_tl_review,
    write_blackboard_entries,
)
from app.teams.wakeup import (
    build_member_task_message,
    build_tl_chat_message,
    build_tl_review_message,
)


def _entries(db, team, status="active"):
    return list(
        db.exec(
            select(TeamBlackboardEntry).where(
                TeamBlackboardEntry.team_id == team.id,
                TeamBlackboardEntry.status == status,
            )
        ).all()
    )


# ---------- 写入流水线 ----------


def test_pipeline_normalize_and_batch_dedup() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        written, skipped = write_blackboard_entries(
            db,
            team=team,
            entries=[
                {"content": "  定价页 转化率  3.2% ", "tags": [" Pricing ", "PRICING", "数据"]},
                {"content": "定价页 转化率 3.2%"},  # 规范化后与首条相同 -> 批次内去重
                {"content": "   "},  # 空内容丢弃
                "非字典条目",
            ],
            source_type="human",
        )
        db.commit()
        assert len(written) == 1
        assert written[0].content == "定价页 转化率 3.2%"
        assert written[0].tags_json == ["pricing", "数据"]
        assert len(skipped) == 3


def test_pipeline_substring_skip_and_superset_merge() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        write_blackboard_entries(
            db,
            team=team,
            entries=[{"content": "竞品 A 定价 99 元", "tags": ["pricing"]}],
            source_type="human",
        )
        db.commit()

        # 新内容是既有条目的子串 -> 不新增
        written, skipped = write_blackboard_entries(
            db, team=team, entries=[{"content": "竞品 A 定价"}], source_type="human"
        )
        assert written == []
        assert len(skipped) == 1
        assert len(_entries(db, team)) == 1

        # 新内容是既有条目的超集 -> 合并更新既有条目(黑板是活文档)
        written, skipped = write_blackboard_entries(
            db,
            team=team,
            entries=[{"content": "竞品 A 定价 99 元,含 20 席", "tags": ["竞品"]}],
            source_type="member",
            source_agent_id="agent_worker",
            source_task_id=task.id,
        )
        db.commit()
        assert skipped == []
        assert len(written) == 1
        rows = _entries(db, team)
        assert len(rows) == 1
        entry = rows[0]
        assert entry.id == written[0].id
        assert entry.content == "竞品 A 定价 99 元,含 20 席"
        assert entry.tags_json == ["pricing", "竞品"]
        assert entry.source_type == "human"  # 合并更新不改来源


def test_pipeline_citation_with_task_title() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        written, _ = write_blackboard_entries(
            db,
            team=team,
            entries=[{"content": "关键结论"}],
            source_type="member",
            source_agent_id="agent_worker",
            source_task_id=task.id,
        )
        db.commit()
        assert written[0].citation_json == {"task_id": task.id, "task_title": "调研竞品"}
        assert written[0].source_agent_id == "agent_worker"
        assert written[0].source_task_id == task.id

        # 无任务来源时 citation 为空
        written2, _ = write_blackboard_entries(
            db, team=team, entries=[{"content": "另一条"}], source_type="human"
        )
        assert written2[0].citation_json == {}


def test_pipeline_invalid_source_type() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        with pytest.raises(ValueError):
            write_blackboard_entries(
                db, team=team, entries=[{"content": "x"}], source_type="robot"
            )


# ---------- 解析 ----------


def test_parse_blackboard_suggestions() -> None:
    reply = (
        "报告正文\n```json\n"
        '{"blackboard_suggestions": [{"content": "结论一", "tags": ["A"]}, '
        '{"content": ""}, {"content": "结论二"}]}\n```'
    )
    assert parse_blackboard_suggestions(reply) == [
        {"content": "结论一", "tags": ["A"]},
        {"content": "结论二"},
    ]
    assert parse_blackboard_suggestions("没有块") == []


def test_parse_tl_review_with_blackboard_writes() -> None:
    reply = (
        "```json\n{\"team_review\": {\"verdict\": \"approve\", \"comment\": \"好\", "
        '"blackboard_writes": [{"content": "结论", "tags": ["a"]}]}}\n```'
    )
    result = parse_tl_review(reply)
    assert result is not None
    assert result["verdict"] == "approve"
    assert result["blackboard_writes"] == [{"content": "结论", "tags": ["a"]}]
    # 不带 blackboard_writes 时保持增量 1 的返回形状
    plain = parse_tl_review('```json\n{"team_review": {"verdict": "rework"}}\n```')
    assert plain == {"verdict": "rework", "comment": ""}


# ---------- 成员建议暂存 + TL 裁决写入 ----------


def test_member_report_stores_suggestions_from_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        wake = wakeup.enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        reply = (
            "完成报告:已交付\n```json\n"
            '{"blackboard_suggestions": [{"content": "竞品 A 定价 99 元", "tags": ["pricing"]}]}\n```'
        )
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: reply)
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *args, **kw: "completed")

        assert wakeup.claim_wake_event(db, wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.status == "review"
        assert task.report_json["blackboard_suggestions"] == [
            {"content": "竞品 A 定价 99 元", "tags": ["pricing"]}
        ]
        # 建议只是暂存,不直接写黑板
        assert _entries(db, team) == []


def test_member_report_suggestions_from_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        wake = wakeup.enqueue_wake_event(
            db, team=team, target_agent_id="agent_worker",
            trigger_type="task_assigned", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        # 最终回复被改写丢块,JSON 块只存在于 frame 级 reply_fragment
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: "完成报告(已改写)")
        fragment = (
            '```json\n{"blackboard_suggestions": [{"content": "来自 fragment 的结论"}]}\n```'
        )
        monkeypatch.setattr(
            wakeup, "collect_turn_reply_fragments", lambda *args, **kw: [fragment]
        )
        monkeypatch.setattr(wakeup, "_team_harness_outcome", lambda *args, **kw: "completed")

        assert wakeup.claim_wake_event(db, wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.report_json["blackboard_suggestions"] == [
            {"content": "来自 fragment 的结论"}
        ]


def test_tl_review_adjudicates_blackboard_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="review")
        task.report_json = {
            "full_reply": "报告全文",
            "blackboard_suggestions": [
                {"content": "竞品 A 定价 99 元", "tags": ["pricing"]},
                {"content": "不值得记的草稿"},
            ],
        }
        wake = wakeup.enqueue_wake_event(
            db, team=team, target_agent_id="agent_tl",
            trigger_type="task_report", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        # TL 只认可第一条(并改了措辞),第二条未写入即视为拒绝
        reply = (
            "```json\n{\"team_review\": {\"verdict\": \"approve\", \"comment\": \"通过\", "
            '"blackboard_writes": [{"content": "竞品 A 定价 99 元/月", "tags": ["pricing"]}]}}\n```'
        )
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: reply)

        assert wakeup.claim_wake_event(db, wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.status == "done"
        rows = _entries(db, team)
        assert len(rows) == 1
        entry = rows[0]
        assert entry.content == "竞品 A 定价 99 元/月"
        assert entry.tags_json == ["pricing"]
        assert entry.source_type == "member"
        assert entry.source_agent_id == "agent_worker"
        assert entry.source_task_id == task.id
        assert entry.citation_json["task_title"] == "调研竞品"

        events = list(
            db.exec(
                select(TeamTaskEvent).where(TeamTaskEvent.event_type == "blackboard_written")
            ).all()
        )
        assert len(events) == 1
        assert events[0].payload_json["written"] == 1
        assert events[0].payload_json["entry_ids"] == [entry.id]


def test_tl_review_without_writes_keeps_blackboard_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team, status="review")
        task.report_json = {
            "full_reply": "报告全文",
            "blackboard_suggestions": [{"content": "成员的建议"}],
        }
        wake = wakeup.enqueue_wake_event(
            db, team=team, target_agent_id="agent_tl",
            trigger_type="task_report", payload={"task_id": task.id},
        )
        db.commit()
        _stub_start_wakeup(monkeypatch)
        reply = '```json\n{"team_review": {"verdict": "approve", "comment": "通过"}}\n```'
        monkeypatch.setattr(wakeup, "run_agent_turn", lambda *args, **kw: reply)

        assert wakeup.claim_wake_event(db, wake.id) is True
        wakeup.execute_wake_event(db, db.get(TeamWakeEvent, wake.id))

        db.refresh(task)
        assert task.status == "done"
        assert _entries(db, team) == []
        assert (
            db.exec(
                select(TeamTaskEvent).where(TeamTaskEvent.event_type == "blackboard_written")
            ).all()
            == []
        )


# ---------- 启动注入 top-K ----------


def test_blackboard_injection_into_messages() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        write_blackboard_entries(
            db,
            team=team,
            entries=[{"content": "竞品 A 定价 99 元", "tags": ["pricing", "竞品"]}],
            source_type="human",
        )
        db.commit()

        member_msg = build_member_task_message(db, team, task, rework=False)
        assert "团队黑板" in member_msg
        assert "- [pricing,竞品] 竞品 A 定价 99 元" in member_msg
        assert "blackboard_suggestions" in member_msg

        tl_chat_msg = build_tl_chat_message(db, team, "帮我看看竞品定价")
        assert "团队黑板" in tl_chat_msg
        assert "竞品 A 定价 99 元" in tl_chat_msg

        review_msg = build_tl_review_message(db, team, task)
        assert "团队黑板" in review_msg
        # 报告无建议时不追加裁决说明
        assert "blackboard_writes" not in review_msg

        task.report_json = {
            "full_reply": "报告",
            "blackboard_suggestions": [{"content": "建议一", "tags": ["x"]}],
        }
        review_msg = build_tl_review_message(db, team, task)
        assert "建议一" in review_msg
        assert "blackboard_writes" in review_msg


def test_blackboard_injection_absent_when_empty() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        task = _make_task(db, team)
        assert "团队黑板(相关工作记忆)" not in build_member_task_message(
            db, team, task, rework=False
        )
        assert "团队黑板(相关工作记忆)" not in build_tl_chat_message(db, team, "随便聊聊")
        assert "团队黑板(相关工作记忆)" not in build_tl_review_message(db, team, task)


def test_blackboard_topk_pinned_first_and_archived_excluded() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        for index in range(12):
            entry = TeamBlackboardEntry(
                team_id=team.id,
                tenant_id=team.tenant_id,
                content=f"条目 {index}",
                tags_json=[],
                source_type="human",
                pinned=(index == 0),  # 最旧的一条置顶
            )
            db.add(entry)
        archived = TeamBlackboardEntry(
            team_id=team.id,
            tenant_id=team.tenant_id,
            content="已归档条目",
            source_type="human",
            status="archived",
        )
        db.add(archived)
        db.commit()

        lines = blackboard_context_lines(db, team, "任意任务文本")
        assert len(lines) == 10
        # pinned 的最旧条目仍在 top-K 内
        assert any("条目 0" in line for line in lines)
        assert all("已归档条目" not in line for line in lines)


def test_blackboard_tag_relevance_scoring() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        write_blackboard_entries(
            db,
            team=team,
            entries=[
                {"content": "无关条目", "tags": ["其他"]},
                {"content": "定价相关条目", "tags": ["定价"]},
            ],
            source_type="human",
        )
        db.commit()
        lines = blackboard_context_lines(db, team, "请调研定价策略")
        assert lines[0].endswith("定价相关条目")


# ---------- API ----------


def test_blackboard_api_crud_flow() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        admin = _admin_user()

        created = teams_api.create_blackboard_entry(
            team.id,
            TeamBlackboardEntryCreateRequest(
                tenant_id="tenant_demo", content="竞品 A 定价 99 元", tags=["Pricing"]
            ),
            db,
            admin,
        )
        assert len(created.entries) == 1
        assert created.skipped == []
        entry = created.entries[0]
        assert entry.source_type == "human"
        assert entry.tags == ["pricing"]
        assert entry.status == "active"
        assert entry.pinned is False

        # 重复直写 -> 流水线去重
        dup = teams_api.create_blackboard_entry(
            team.id,
            TeamBlackboardEntryCreateRequest(tenant_id="tenant_demo", content="竞品 A 定价 99 元"),
            db,
            admin,
        )
        assert dup.entries == []
        assert len(dup.skipped) == 1

        listed = teams_api.list_blackboard_entries(team.id, "tenant_demo", "active", db, admin)
        assert len(listed) == 1

        updated = teams_api.update_blackboard_entry(
            team.id,
            entry.id,
            TeamBlackboardEntryUpdateRequest(
                tenant_id="tenant_demo", content="竞品 A 定价 99 元/月", pinned=True
            ),
            db,
            admin,
        )
        assert updated.content == "竞品 A 定价 99 元/月"
        assert updated.pinned is True

        archived = teams_api.archive_blackboard_entry(
            team.id,
            entry.id,
            TeamBlackboardEntryArchiveRequest(tenant_id="tenant_demo"),
            db,
            admin,
        )
        assert archived.status == "archived"
        assert teams_api.list_blackboard_entries(team.id, "tenant_demo", "active", db, admin) == []
        archived_list = teams_api.list_blackboard_entries(
            team.id, "tenant_demo", "archived", db, admin
        )
        assert len(archived_list) == 1

        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_blackboard_entries(team.id, "tenant_demo", "nonsense", db, admin)
        assert exc_info.value.status_code == 400


def test_blackboard_write_requires_manager_read_open_to_tenant() -> None:
    with _test_session() as db:
        team = _seed_team(db)  # owner 是 user_admin
        outsider = _admin_user_other()  # 同租户,非 owner 非 admin

        with pytest.raises(HTTPException) as exc_info:
            teams_api.create_blackboard_entry(
                team.id,
                TeamBlackboardEntryCreateRequest(tenant_id="tenant_demo", content="x"),
                db,
                outsider,
            )
        assert exc_info.value.status_code == 403

        # 本租户普通登录用户可读
        assert teams_api.list_blackboard_entries(team.id, "tenant_demo", "active", db, outsider) == []

        # 跨租户不可读
        other_tenant_user = _admin_user()
        other_tenant_user.tenant_id = "tenant_other"
        with pytest.raises(HTTPException) as exc_info:
            teams_api.list_blackboard_entries(
                team.id, "tenant_demo", "active", db, other_tenant_user
            )
        assert exc_info.value.status_code == 403

        # 不存在的条目 404
        with pytest.raises(HTTPException) as exc_info:
            teams_api.update_blackboard_entry(
                team.id,
                "bbentry_missing",
                TeamBlackboardEntryUpdateRequest(tenant_id="tenant_demo", pinned=True),
                db,
                _admin_user(),
            )
        assert exc_info.value.status_code == 404


def test_delete_team_cascades_blackboard() -> None:
    with _test_session() as db:
        team = _seed_team(db)
        write_blackboard_entries(
            db, team=team, entries=[{"content": "条目"}], source_type="human"
        )
        db.commit()
        assert teams_api.delete_team_endpoint(team.id, "tenant_demo", db, _admin_user()) == {
            "ok": True
        }
        assert db.exec(select(TeamBlackboardEntry)).all() == []
