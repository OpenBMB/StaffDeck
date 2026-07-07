from datetime import datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import (
    _build_turn_traces,
    _message_turn_ids_from_events,
    _persist_chat_turn_cancelled,
    message_read,
)
from app.db.models import AgentEvent, ChatSession, Message


def test_turn_trace_uses_router_skill_hint_when_events_have_turn_id() -> None:
    started_at = datetime(2026, 6, 5, 6, 35, 4)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_test",
            role="user",
            content="帮我下单a2，实际发货a3",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "帮我下单a2，实际发货a3"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="router_decision_created",
            payload_json={
                "decision": "continue_current_skill",
                "target_skill_id": "skill_purchase_001",
                "target_step_id": "confirm_purchase",
                "user_intent": "下单",
                "reason": "继续购买流程",
                "user_message_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="skill_step_changed",
            payload_json={
                "from_step_id": "confirm_purchase",
                "to_step_id": "end",
                "user_message_id": "msg_user",
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_test",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user", "reply": "已完成"},
            created_at=started_at + timedelta(seconds=3),
        ),
    ]

    traces = _build_turn_traces(messages, events, {"skill_purchase_001": "购买商品流程"})

    skill_lines = [
        line
        for line in traces[0]["lines"]
        if line["kind"] == "skill" and "购买商品流程" in line["text"]
    ]
    assert skill_lines
    assert skill_lines[0]["text"] == "推进技能 购买商品流程"
    assert skill_lines[0]["detail"] == "step end"


def test_turn_trace_falls_back_to_knowledge_citations_without_events() -> None:
    started_at = datetime(2026, 6, 20, 10, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_citation",
            role="user",
            content="引用规则是什么？",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_citation",
            role="assistant",
            content="回答需要展示知识引用。[1]",
            metadata_json={
                "knowledge_citations": [
                    {
                        "title": "知识引用测试说明 / 引用规则",
                        "source_title": "citation-demo.md",
                    }
                ]
            },
            created_at=started_at + timedelta(seconds=1),
        ),
    ]

    traces = _build_turn_traces(messages, [], {})

    assert len(traces) == 1
    assert traces[0]["turn_id"] == "msg_user"
    assert [line["text"] for line in traces[0]["lines"]] == [
        "执行记录",
        "识别为业务资料问答",
        "查询业务资料",
        "读取业务资料",
        "生成带引用回答",
    ]


def test_turn_trace_keeps_running_routing_status_for_refresh() -> None:
    started_at = datetime(2026, 7, 4, 9, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_running",
            role="user",
            content="你好",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_running",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_running",
            event_type="stream_status",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=100),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] is None
    assert any(
        line["id"] == "decision_router" and line["text"] == "判断意图" and line["state"] == "running"
        for line in traces[0]["lines"]
    )


def test_turn_trace_cancel_event_closes_running_status_for_refresh() -> None:
    started_at = datetime(2026, 7, 4, 9, 5, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            role="user",
            content="暂停测试",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "暂停测试"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="stream_status",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=100),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_cancelled",
            event_type="stream_cancelled",
            payload_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=300),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] == (started_at + timedelta(milliseconds=300)).isoformat()
    assert all(line["state"] != "running" for line in traces[0]["lines"])
    assert any(
        line["id"] == "generation_stopped"
        and line["text"] == "用户已停止生成"
        and line["state"] == "completed"
        for line in traces[0]["lines"]
    )


def test_scheduled_task_draft_trace_restores_config_stages_for_refresh() -> None:
    started_at = datetime(2026, 7, 7, 16, 50, 0)
    draft = {
        "should_create": True,
        "tenant_id": "tenant_demo",
        "agent_id": "agent_demo",
        "title": "提醒我喝咖啡",
        "prompt": "提醒我喝咖啡",
        "schedule_type": "daily",
        "schedule": {"time": "16:50"},
        "timezone": "Asia/Shanghai",
        "confidence": 0.95,
        "source_session_id": "session_schedule",
    }
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_schedule",
            role="user",
            content="16:50提醒我喝咖啡",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_schedule",
            role="assistant",
            content="我已按你选择的定时项目整理成自动任务草案。",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user", "scheduled_task_draft": draft},
            created_at=started_at + timedelta(milliseconds=500),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "16:50提醒我喝咖啡"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "scheduled_task_intent",
                "text": "识别定时任务需求",
            },
            created_at=started_at + timedelta(milliseconds=100),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "scheduled_task_parse",
                "text": "解析执行计划",
            },
            created_at=started_at + timedelta(milliseconds=200),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="scheduled_task_draft_created",
            payload_json={**draft, "turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=300),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_schedule",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant", "turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(milliseconds=500),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] == (started_at + timedelta(milliseconds=500)).isoformat()
    assert [line["id"] for line in traces[0]["lines"]] == [
        "scheduled_task_intent",
        "scheduled_task_parse",
        "scheduled_task_draft",
    ]
    assert all(line["state"] == "completed" for line in traces[0]["lines"])
    assert traces[0]["lines"][1]["detail"] == "计划：每天 16:50"
    assert "提醒我喝咖啡" in traces[0]["lines"][2]["detail"]


def test_cancel_endpoint_persists_terminal_trace_for_client_turn_id() -> None:
    db = _test_db()
    started_at = datetime(2026, 7, 4, 9, 5, 0)
    session_row = ChatSession(id="session_cancel_endpoint", tenant_id="tenant_demo", user_id="user_demo")
    db.add(session_row)
    db.add(
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id=session_row.id,
            role="user",
            content="暂停测试",
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="user_message_received",
            payload_json={
                "message_id": "msg_user",
                "client_turn_id": "turn_local_1",
                "message": "暂停测试",
            },
            created_at=started_at,
        )
    )
    db.add(
        AgentEvent(
            tenant_id="tenant_demo",
            session_id=session_row.id,
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "routing",
                "text": "正在判断用户意图",
            },
            created_at=started_at + timedelta(milliseconds=100),
        )
    )
    db.commit()

    assert _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_1", "user_demo")
    db.commit()
    assert not _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_1", "user_demo")

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == session_row.id)
        .order_by(AgentEvent.created_at)
    ).all()
    cancel_events = [event for event in events if event.event_type == "stream_cancelled"]
    assert len(cancel_events) == 1
    assert cancel_events[0].payload_json["turn_id"] == "msg_user"
    assert cancel_events[0].payload_json["user_message_id"] == "msg_user"
    assert cancel_events[0].payload_json["client_turn_id"] == "turn_local_1"

    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == session_row.id)
        .order_by(Message.created_at)
    ).all()
    assistant_messages = [message for message in messages if message.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "已停止生成"
    assert assistant_messages[0].metadata_json["turn_id"] == "msg_user"
    assert assistant_messages[0].metadata_json["user_message_id"] == "msg_user"
    assert assistant_messages[0].metadata_json["client_turn_id"] == "turn_local_1"
    traces = _build_turn_traces(messages, events, {})
    assert traces[0]["completed_at"] == cancel_events[0].created_at.isoformat()
    assert all(line["state"] != "running" for line in traces[0]["lines"])
    assert any(
        line["id"] == "generation_stopped"
        and line["text"] == "用户已停止生成"
        and line["state"] == "completed"
        for line in traces[0]["lines"]
    )


def test_cancel_endpoint_persists_cancel_even_before_user_event_is_visible() -> None:
    db = _test_db()
    session_row = ChatSession(id="session_cancel_before_event", tenant_id="tenant_demo", user_id="user_demo")
    db.add(session_row)
    db.commit()

    assert _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_pending", "user_demo")
    db.commit()
    assert not _persist_chat_turn_cancelled(db, "tenant_demo", session_row, "turn_local_pending", "user_demo")

    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == session_row.id)
        .order_by(AgentEvent.created_at)
    ).all()
    cancel_events = [event for event in events if event.event_type == "stream_cancelled"]
    assert len(cancel_events) == 1
    assert cancel_events[0].payload_json["turn_id"] == "turn_local_pending"
    assert cancel_events[0].payload_json["user_message_id"] == "turn_local_pending"
    assert cancel_events[0].payload_json["client_turn_id"] == "turn_local_pending"
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == session_row.id)
        .order_by(Message.created_at)
    ).all()
    assert [message.role for message in messages] == []


def test_turn_trace_without_terminal_event_stays_open_for_refresh_recovery() -> None:
    started_at = datetime(2026, 7, 4, 9, 6, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_refresh",
            role="user",
            content="你是谁",
            created_at=started_at,
        )
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_refresh",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你是谁"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_refresh",
            event_type="stream_status",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "phase": "routing",
                "text": "正在判断用户意图",
            },
            created_at=started_at + timedelta(milliseconds=100),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert traces[0]["completed_at"] is None
    assert any(line["id"] == "decision_router" and line["state"] == "running" for line in traces[0]["lines"])
    assert all(line["id"] != "generation_stopped" for line in traces[0]["lines"])


def test_turn_trace_ignores_trace_events_without_turn_id() -> None:
    started_at = datetime(2026, 7, 4, 9, 8, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            role="user",
            content="北京今天天气如何",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            role="assistant",
            content="北京今天晴朗。",
            created_at=started_at + timedelta(seconds=50),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "北京今天天气如何"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="router_decision_created",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "decision": "answer_only",
                "user_intent": "查询天气",
                "reason": "实时信息查询",
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_selected",
            payload_json={
                "skill_slug": "maomao-weather",
                "skill_name": "weather",
                "reason": "匹配天气查询能力",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="tool_result",
            payload_json={
                "toolName": "weather",
                "rawToolName": "maomao-weather",
                "success": True,
                "content": {"tool_name": "maomao-weather", "success": True, "data": {"found": True}},
            },
            created_at=started_at + timedelta(seconds=4),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_trace",
            payload_json={
                "skill_slug": "maomao-weather",
                "phase": "planning",
                "message": "正在根据 SKILL.md 生成 runner",
            },
            created_at=started_at + timedelta(seconds=4),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_trace",
            payload_json={
                "skill_slug": "maomao-weather",
                "phase": "reflection_reviewed",
                "message": "已完成运行结果校验",
                "review": {"reason": "结果可用"},
            },
            created_at=started_at + timedelta(seconds=5),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="general_skill_run_finished",
            payload_json={"skill_slug": "maomao-weather", "success": True},
            created_at=started_at + timedelta(seconds=6),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_general_skill",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant",
                "user_message_id": "msg_user",
                "reply": "北京今天晴朗。",
            },
            created_at=started_at + timedelta(seconds=50),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    texts = [line["text"] for line in traces[0]["lines"]]
    assert traces[0]["turn_id"] == "msg_user"
    assert "选择通用技能 weather" not in texts
    assert "调用工具 weather" not in texts
    assert "正在根据 SKILL.md 生成 runner" not in texts
    assert "已完成运行结果校验" not in texts
    assert "通用技能运行完成" not in texts


def test_turn_trace_restores_stream_tool_and_skill_events_with_turn_id() -> None:
    started_at = datetime(2026, 7, 4, 9, 9, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            role="user",
            content="北京今天天气如何",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            role="assistant",
            content="北京今天晴朗。",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(seconds=50),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "北京今天天气如何"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="general_skill_trace",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "skill_slug": "maomao-weather",
                "phase": "planning",
                "message": "正在根据 SKILL.md 生成 runner",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="tool_result",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "toolName": "weather",
                "rawToolName": "maomao-weather",
                "success": True,
                "content": {"tool_name": "maomao-weather", "success": True, "data": {"found": True}},
            },
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="agent_loop_completed",
            payload_json={
                "turn_id": "msg_user",
                "user_message_id": "msg_user",
                "iteration": 1,
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_stream_trace",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant",
                "user_message_id": "msg_user",
                "reply": "北京今天晴朗。",
            },
            created_at=started_at + timedelta(seconds=50),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    texts = [line["text"] for line in traces[0]["lines"]]
    assert "正在根据 SKILL.md 生成 runner" in texts
    assert "调用工具 weather" in texts
    assert "重新分析执行动作" in texts


def test_turn_trace_uses_message_id_for_repeated_user_text() -> None:
    started_at = datetime(2026, 7, 3, 10, 0, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="assistant",
            content="你好！",
            created_at=started_at + timedelta(seconds=2),
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="user",
            content="你好",
            created_at=started_at + timedelta(seconds=10),
        ),
        Message(
            id="msg_assistant_second",
            tenant_id="tenant_demo",
            session_id="session_repeat",
            role="assistant",
            content="请问有什么可以帮您？",
            created_at=started_at + timedelta(seconds=12),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user_first", "reply": "你好！"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=10),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_second",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第二轮问候",
            },
            created_at=started_at + timedelta(seconds=11),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"user_message_id": "msg_user_second", "reply": "请问有什么可以帮您？"},
            created_at=started_at + timedelta(seconds=12),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["msg_user_first", "msg_user_second"]
    assert traces[1]["user_message_id"] == "msg_user_second"
    assert any(line["text"] == "判断意图 问候" and line["detail"] == "第二轮问候" for line in traces[1]["lines"])


def test_turn_trace_keeps_late_trace_events_after_assistant_event() -> None:
    started_at = datetime(2026, 7, 6, 10, 0, 0)
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            role="assistant",
            content="你好！",
            metadata_json={"turn_id": "msg_user", "user_message_id": "msg_user"},
            created_at=started_at + timedelta(seconds=2),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="stream_status",
            payload_json={"user_message_id": "msg_user", "turn_id": "msg_user", "phase": "routing", "text": "正在判断用户意图"},
            created_at=started_at + timedelta(milliseconds=200),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant", "user_message_id": "msg_user", "reply": "你好！"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user",
                "turn_id": "msg_user",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "晚到的意图明细也要保留",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_late_trace",
            event_type="step_result",
            payload_json={
                "user_message_id": "msg_user",
                "turn_id": "msg_user",
                "reply": "直接回复问候",
            },
            created_at=started_at + timedelta(seconds=4),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert len(traces) == 1
    assert traces[0]["completed_at"] == (started_at + timedelta(seconds=2)).isoformat()
    assert any(
        line["text"] == "判断意图 问候" and line["detail"] == "晚到的意图明细也要保留"
        for line in traces[0]["lines"]
    )
    assert any(line["text"] == "完成步骤判断" and line["detail"] == "直接回复问候" for line in traces[0]["lines"])
    assert all(line["state"] != "running" for line in traces[0]["lines"])


def test_turn_trace_does_not_merge_interleaved_repeated_turns() -> None:
    started_at = datetime(2026, 7, 3, 10, 30, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="user",
            content="你好",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="assistant",
            content="我是第一个回答。",
            created_at=started_at + timedelta(seconds=12),
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="user",
            content="你好",
            created_at=started_at + timedelta(seconds=2),
        ),
        Message(
            id="msg_assistant_second",
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            role="assistant",
            content="我是第二个回答。",
            created_at=started_at + timedelta(seconds=14),
        ),
    ]
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_first",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第一轮问候",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=2),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="router_decision_created",
            payload_json={
                "user_message_id": "msg_user_second",
                "decision": "answer_only",
                "user_intent": "问候",
                "reason": "第二轮问候",
            },
            created_at=started_at + timedelta(seconds=3),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_first",
                "user_message_id": "msg_user_first",
                "reply": "我是第一个回答。",
            },
            created_at=started_at + timedelta(seconds=12),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_interleaved",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_second",
                "user_message_id": "msg_user_second",
                "reply": "我是第二个回答。",
            },
            created_at=started_at + timedelta(seconds=14),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["msg_user_first", "msg_user_second"]
    assert traces[0]["completed_at"] == (started_at + timedelta(seconds=12)).isoformat()
    assert traces[1]["completed_at"] == (started_at + timedelta(seconds=14)).isoformat()
    first_details = [line.get("detail") for line in traces[0]["lines"]]
    second_details = [line.get("detail") for line in traces[1]["lines"]]
    assert "第一轮问候" in first_details
    assert "第二轮问候" not in first_details
    assert "第二轮问候" in second_details
    assert "第一轮问候" not in second_details


def test_turn_trace_without_message_id_does_not_bind_user_messages() -> None:
    started_at = datetime(2026, 7, 3, 11, 0, 0)
    messages = [
        Message(
            id="msg_user_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            role="user",
            content="第一句",
            created_at=started_at,
        ),
        Message(
            id="msg_user_second",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            role="user",
            content="第二句",
            created_at=started_at + timedelta(seconds=10),
        ),
    ]
    events = [
        AgentEvent(
            id="evt_user_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="user_message_received",
            payload_json={"message": "第二句"},
            created_at=started_at,
        ),
        AgentEvent(
            id="evt_assistant_first",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="assistant_message_created",
            payload_json={"reply": "收到"},
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            id="evt_user_second",
            tenant_id="tenant_demo",
            session_id="session_sequence",
            event_type="user_message_received",
            payload_json={"message": "第二句"},
            created_at=started_at + timedelta(seconds=10),
        ),
    ]

    traces = _build_turn_traces(messages, events, {})

    assert [trace["turn_id"] for trace in traces] == ["evt_user_first", "evt_user_second"]
    assert [trace["user_message_id"] for trace in traces] == [None, None]


def test_message_turn_ids_from_events_use_ids_not_message_text() -> None:
    started_at = datetime(2026, 7, 3, 12, 0, 0)
    events = [
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_first", "message": "你好"},
            created_at=started_at,
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_first",
                "user_message_id": "msg_user_first",
                "reply": "你好！",
            },
            created_at=started_at + timedelta(seconds=1),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message_id": "msg_user_second", "message": "你好"},
            created_at=started_at + timedelta(seconds=10),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={
                "message_id": "msg_assistant_second",
                "turn_id": "msg_user_second",
                "reply": "请问有什么可以帮您？",
            },
            created_at=started_at + timedelta(seconds=11),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="user_message_received",
            payload_json={"message": "你好"},
            created_at=started_at + timedelta(seconds=20),
        ),
        AgentEvent(
            tenant_id="tenant_demo",
            session_id="session_repeat",
            event_type="assistant_message_created",
            payload_json={"message_id": "msg_assistant_without_user_id", "reply": "旧事件不应猜测归属"},
            created_at=started_at + timedelta(seconds=21),
        ),
    ]

    assert _message_turn_ids_from_events(events) == {
        "msg_user_first": "msg_user_first",
        "msg_assistant_first": "msg_user_first",
        "msg_user_second": "msg_user_second",
        "msg_assistant_second": "msg_user_second",
    }


def test_message_read_uses_metadata_turn_id_when_event_mapping_is_missing() -> None:
    row = Message(
        id="msg_assistant",
        tenant_id="tenant_demo",
        session_id="session_repeat",
        role="assistant",
        content="你好",
        metadata_json={"turn_id": "msg_user"},
        created_at=datetime(2026, 7, 4, 12, 0, 0),
    )

    assert message_read(row).turn_id == "msg_user"


def _test_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
