"""Planner 词法快速路径（结构性守卫直通）的单测。

守卫原则：只在「LLM 没有任何路由决策空间」时直通；任何守卫不满足必须
返回 None 回落完整 LLM 规划。这里逐条验证每个守卫，防止未来改守卫时
悄悄放宽导致误路由。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.core.plan.planner import TurnPlanner, _fast_path_plan
from app.db.models import ChatSession, Skill


def _chat_session(**updates: Any) -> ChatSession:
    values: dict[str, Any] = {
        "id": "session-1",
        "tenant_id": "tenant-demo",
    }
    values.update(updates)
    return ChatSession(**values)


def _skill() -> Skill:
    return Skill(
        id="skill-refund",
        tenant_id="tenant-demo",
        skill_id="refund",
        name="退款流程",
        status="published",
        content_json={"start_node_id": "collect", "nodes": []},
    )


def test_fast_path_hits_on_clean_context() -> None:
    plan = _fast_path_plan(
        _chat_session(),
        "冲浪勇士 安全吗？有年龄限制吗",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )

    assert plan is not None
    assert plan.decision == "answer_only"
    assert plan.confidence == 1.0
    assert len(plan.task_frames) == 1
    frame = plan.task_frames[0]
    assert frame.kind == "conversation"
    assert frame.decision == "answer_only"
    assert frame.user_intent == "冲浪勇士 安全吗？有年龄限制吗"
    assert frame.source_message == "冲浪勇士 安全吗？有年龄限制吗"


def test_fast_path_plan_normalizes_like_llm_plan() -> None:
    """直通产物经 _normalize 后应与 LLM 版同构（帧 ID、requirements 兜底）。"""
    plan = _fast_path_plan(
        _chat_session(),
        "介绍一下儿童俱乐部",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is not None

    normalized = TurnPlanner()._normalize(
        plan,
        "介绍一下儿童俱乐部",
        _chat_session(),
        available_skills=[],
    )

    assert normalized.decision == "answer_only"
    assert len(normalized.task_frames) == 1
    frame = normalized.task_frames[0]
    assert frame.task_id
    assert frame.kind == "conversation"
    assert frame.execution_mode == "standard"
    assert frame.requirements
    assert frame.source_message == "介绍一下儿童俱乐部"


def test_fast_path_skips_when_skills_available() -> None:
    """有 SOP 可选时必须走 LLM：消息可能命中 trigger_intents。"""
    plan = _fast_path_plan(
        _chat_session(),
        "我要申请退款",
        available_skills=[_skill()],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None


def test_fast_path_skips_when_active_skill() -> None:
    plan = _fast_path_plan(
        _chat_session(active_skill_id="refund", active_step_id="collect"),
        "继续",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None


def test_fast_path_skips_when_pending_tasks() -> None:
    plan = _fast_path_plan(
        _chat_session(pending_tasks_json=[{"task_id": "task-1", "user_intent": "查工资"}]),
        "谢谢",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None


def test_fast_path_skips_when_task_frames_present() -> None:
    plan = _fast_path_plan(
        _chat_session(),
        "谢谢",
        available_skills=[],
        task_frame_state=[{"task_id": "task-1", "kind": "conversation"}],
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None


def test_fast_path_skips_on_handoff_keyword() -> None:
    plan = _fast_path_plan(
        _chat_session(),
        "我想转人工",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None

    plan = _fast_path_plan(
        _chat_session(),
        "please connect me to a human agent",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None


def test_fast_path_skips_on_slash_command_and_empty() -> None:
    for message in ("", "   ", "/help", "/模型"):
        plan = _fast_path_plan(
            _chat_session(),
            message,
            available_skills=[],
            task_frame_state=None,
            interaction_mode="normal",
            team_context=None,
        )
        assert plan is None, message


def test_fast_path_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "planner_fast_path_enabled", False)

    plan = _fast_path_plan(
        _chat_session(),
        "冲浪勇士 安全吗",
        available_skills=[],
        task_frame_state=None,
        interaction_mode="normal",
        team_context=None,
    )
    assert plan is None
