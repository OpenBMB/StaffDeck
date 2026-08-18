from __future__ import annotations

import pytest

from app.core.harness_v2_engine import _turn_slash_selection
from app.core.slash_commands import SlashCommandError
from app.db.models import ScheduledTask
from app.scheduled_tasks.service import _scheduled_task_sop_id
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
