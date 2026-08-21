from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.harness_v2_engine import _apply_forced_sop_snapshot, _turn_slash_selection
from app.core.slash_commands import SlashCommandError
from app.db.models import ScheduledTask, Skill
from app.scheduled_tasks import service as scheduled_task_service
from app.scheduled_tasks.service import (
    _prepare_scheduled_task_sop_metadata,
    _scheduled_task_sop_id,
    _scheduled_task_sop_snapshot,
    scheduled_task_read,
)
from app.session.session_schema import ChatTurnRequest


def test_scheduled_task_uses_server_pinned_sop_without_rewriting_visible_prompt() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant-demo",
        agent_id="agent-demo",
        message="每天汇总昨日销售数据",
        channel="scheduled_task",
        interaction_mode="scheduled_task",
        forced_sop_id="daily_sales_report",
    )

    selection = _turn_slash_selection(request)

    assert selection is not None
    assert selection.kind == "sop"
    assert selection.target == "daily_sales_report"
    assert selection.prompt == request.message


def test_scheduled_task_metadata_exposes_only_the_explicit_sop_selection() -> None:
    task = ScheduledTask(
        tenant_id="tenant-demo",
        agent_id="agent-demo",
        created_by_user_id="user-demo",
        title="日报",
        prompt="生成日报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
        metadata_json={"sop_id": "daily_report_v2", "source": "console"},
    )

    assert _scheduled_task_sop_id(task) == "daily_report_v2"
    assert _scheduled_task_sop_snapshot(task) is None


def test_pinned_scheduled_task_uses_immutable_sop_snapshot() -> None:
    current = Skill(
        tenant_id="tenant-demo",
        skill_id="daily_report_v2",
        version="2.0.0",
        name="日报（新版）",
        content_json={"nodes": [{"node_id": "new"}]},
        status="published",
    )
    snapshot = {
        "skill_id": "daily_report_v2",
        "version": "1.0.0",
        "name": "日报（固定版）",
        "content_json": {"nodes": [{"node_id": "pinned"}]},
    }
    task = ScheduledTask(
        tenant_id="tenant-demo",
        agent_id="agent-demo",
        created_by_user_id="user-demo",
        title="日报",
        prompt="生成日报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
        metadata_json={
            "sop_id": "daily_report_v2",
            "sop_version_policy": "pinned",
            "_sop_snapshot": snapshot,
        },
    )

    projected = _apply_forced_sop_snapshot(
        [current],
        _scheduled_task_sop_id(task),
        _scheduled_task_sop_snapshot(task),
    )

    assert projected[0].version == "1.0.0"
    assert projected[0].name == "日报（固定版）"
    assert projected[0].content_json["nodes"][0]["node_id"] == "pinned"


def test_latest_scheduled_task_keeps_current_published_sop() -> None:
    current = Skill(
        tenant_id="tenant-demo",
        skill_id="daily_report_v2",
        version="3.0.0",
        name="日报（当前最新版）",
        content_json={"nodes": [{"node_id": "latest"}]},
        status="published",
    )

    projected = _apply_forced_sop_snapshot([current], "daily_report_v2", None)

    assert projected[0] is current
    assert projected[0].version == "3.0.0"


def test_pinned_snapshot_cannot_restore_an_unbound_sop() -> None:
    snapshot = {
        "skill_id": "daily_report_v2",
        "version": "1.0.0",
        "name": "日报",
        "content_json": {"nodes": []},
    }

    assert _apply_forced_sop_snapshot([], "daily_report_v2", snapshot) == []


def test_prepare_pinned_policy_captures_server_owned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = Skill(
        tenant_id="tenant-demo",
        skill_id="daily_report_v2",
        version="2.1.0",
        name="日报",
        content_json={"nodes": [{"node_id": "root"}], "edges": []},
        status="published",
    )
    monkeypatch.setattr(
        scheduled_task_service,
        "visible_published_skills",
        lambda *_args: [current],
    )

    metadata = _prepare_scheduled_task_sop_metadata(
        object(),  # type: ignore[arg-type]
        "tenant-demo",
        "agent-demo",
        {
            "sop_id": "daily_report_v2",
            "sop_version_policy": "pinned",
            "_sop_snapshot": {"skill_id": "untrusted"},
        },
    )

    assert metadata["sop_version"] == "2.1.0"
    assert metadata["_sop_snapshot"]["skill_id"] == "daily_report_v2"
    assert metadata["_sop_snapshot"]["content_json"]["nodes"][0]["node_id"] == "root"


def test_scheduled_task_api_read_hides_internal_snapshot() -> None:
    task = ScheduledTask(
        tenant_id="tenant-demo",
        agent_id="agent-demo",
        created_by_user_id="user-demo",
        title="日报",
        prompt="生成日报",
        schedule_type="daily",
        schedule_json={"time": "09:00"},
        metadata_json={
            "sop_id": "daily_report_v2",
            "sop_version_policy": "pinned",
            "sop_version": "1.0.0",
            "_sop_snapshot": {"skill_id": "daily_report_v2"},
        },
    )

    metadata = scheduled_task_read(task).metadata

    assert metadata["sop_version_policy"] == "pinned"
    assert metadata["sop_version"] == "1.0.0"
    assert "_sop_snapshot" not in metadata


def test_scheduled_task_rejects_ambiguous_user_slash_and_server_pinned_sop() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant-demo",
        message="/sop another_sop",
        interaction_mode="scheduled_task",
        forced_sop_id="daily_report_v2",
    )

    with pytest.raises(SlashCommandError) as exc_info:
        _turn_slash_selection(request)

    assert exc_info.value.code == "FORCED_SOP_COMMAND_CONFLICT"


def test_prepare_latest_sop_metadata_rejects_unavailable_sop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scheduled_task_service,
        "visible_published_skills",
        lambda *_args: [],
    )

    with pytest.raises(HTTPException) as exc_info:
        _prepare_scheduled_task_sop_metadata(
            object(),  # type: ignore[arg-type]
            "tenant-demo",
            "agent-demo",
            {
                "sop_id": "missing_sop",
                "sop_version_policy": "latest",
            },
        )

    assert exc_info.value.status_code == 400
