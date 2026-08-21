from datetime import datetime, timedelta

from app.api.chat import _build_turn_traces
from app.db.models import AgentEvent, Message
from app.observability.session_timings import enrich_turn_traces_with_timings


def test_enterprise_trace_timings_include_each_step_and_model_time() -> None:
    started_at = datetime(2026, 8, 2, 10, 0, 0)
    turn_payload = {
        "turn_id": "msg_user",
        "user_message_id": "msg_user",
        "client_turn_id": "turn_client",
    }
    frame_payload = {
        **turn_payload,
        "task_frame_id": "task_demo",
        "harness_run_id": "run_demo",
    }
    messages = [
        Message(
            id="msg_user",
            tenant_id="tenant_demo",
            session_id="session_demo",
            role="user",
            content="查询制度",
            created_at=started_at,
        ),
        Message(
            id="msg_assistant",
            tenant_id="tenant_demo",
            session_id="session_demo",
            role="assistant",
            content="查询完成",
            created_at=started_at + timedelta(milliseconds=8100),
        ),
    ]
    events = [
        _event(
            "user_message_received",
            started_at,
            {**turn_payload, "message_id": "msg_user", "message": "查询制度"},
        ),
        _model_span(
            started_at,
            finished_after_ms=2000,
            started_after_ms=500,
            duration_ms=1500,
            operation="turn_planner.plan",
        ),
        _event(
            "router_decision_created",
            started_at + timedelta(milliseconds=2050),
            {**turn_payload, "decision": "start_new_task", "user_intent": "查询制度"},
        ),
        _event(
            "task_frame_started",
            started_at + timedelta(milliseconds=2100),
            {**frame_payload, "kind": "conversation"},
        ),
        _model_span(
            started_at,
            finished_after_ms=3100,
            started_after_ms=2200,
            duration_ms=900,
            operation="harness.task_action",
            task_frame_id="task_demo",
            iteration=1,
        ),
        _event(
            "harness_action_created",
            started_at + timedelta(milliseconds=3150),
            {
                **frame_payload,
                "iteration": 1,
                "action": "tool",
                "tool_name": "knowledge_search",
            },
        ),
        _model_span(
            started_at,
            finished_after_ms=4000,
            started_after_ms=3300,
            duration_ms=700,
            operation="knowledge.document_route",
        ),
        _event(
            "harness_tool_completed",
            started_at + timedelta(milliseconds=5000),
            {
                **frame_payload,
                "iteration": 1,
                "tool_name": "knowledge_search",
                "success": True,
                "result": {"success": True},
            },
        ),
        _model_span(
            started_at,
            finished_after_ms=5900,
            started_after_ms=5100,
            duration_ms=800,
            operation="harness.task_action",
            task_frame_id="task_demo",
            iteration=2,
        ),
        _event(
            "harness_action_created",
            started_at + timedelta(milliseconds=6050),
            {**frame_payload, "iteration": 2, "action": "finish"},
        ),
        _event(
            "task_frame_finished",
            started_at + timedelta(milliseconds=6100),
            {**frame_payload, "status": "completed", "action_count": 2},
        ),
        _model_span(
            started_at,
            finished_after_ms=8000,
            started_after_ms=6200,
            duration_ms=1800,
            operation="response.generate",
        ),
        _event(
            "assistant_message_created",
            started_at + timedelta(milliseconds=8100),
            {
                **turn_payload,
                "message_id": "msg_assistant",
                "reply": "查询完成",
            },
        ),
    ]

    traces = enrich_turn_traces_with_timings(
        _build_turn_traces(messages, events, {}),
        events,
    )

    assert len(traces) == 1
    trace = traces[0]
    assert trace["duration_ms"] == 8100
    assert trace["model_duration_ms"] == 5700
    assert trace["model_call_count"] == 5
    assert trace["model_names"] == ["GLM Test"]
    lines = {line["id"]: line for line in trace["lines"]}
    assert lines["decision_router"]["duration_ms"] == 1550
    assert lines["decision_router"]["model_duration_ms"] == 1500
    assert lines["decision_router"]["model_names"] == ["GLM Test"]
    assert lines["harness_frame_task_demo"]["duration_ms"] == 4000
    assert lines["harness_frame_task_demo"]["model_duration_ms"] == 2400
    assert lines["harness_action_task_demo_1"]["duration_ms"] == 2800
    assert "model_duration_ms" not in lines["harness_action_task_demo_1"]
    assert lines["harness_action_task_demo_1"]["depth"] == 1
    assert "duration_ms" not in lines["harness_finish_task_demo_2"]
    assert "model_duration_ms" not in lines["harness_finish_task_demo_2"]
    harness_model_lines = [
        line
        for line in trace["lines"]
        if str(line["id"]).startswith("harness_model_task_demo_")
    ]
    assert [line["model_duration_ms"] for line in harness_model_lines] == [900, 800]
    assert [line["model_call_count"] for line in harness_model_lines] == [1, 1]
    assert [line["text"] for line in harness_model_lines] == [
        "第 1 轮决定调用能力",
        "第 2 轮决定完成任务",
    ]
    assert all(line["depth"] == 1 for line in harness_model_lines)
    assert lines["response_generation"]["duration_ms"] == 1800
    assert lines["response_generation"]["model_duration_ms"] == 1800


def test_enterprise_trace_timings_merge_overlapping_model_spans() -> None:
    started_at = datetime(2026, 8, 2, 11, 0, 0)
    turn_payload = {"turn_id": "msg_overlap", "user_message_id": "msg_overlap"}
    messages = [
        Message(
            id="msg_overlap",
            tenant_id="tenant_demo",
            session_id="session_overlap",
            role="user",
            content="继续",
            created_at=started_at,
        ),
        Message(
            id="msg_overlap_assistant",
            tenant_id="tenant_demo",
            session_id="session_overlap",
            role="assistant",
            content="已完成",
            created_at=started_at + timedelta(milliseconds=1000),
        ),
    ]
    events = [
        _event(
            "user_message_received",
            started_at,
            {**turn_payload, "message_id": "msg_overlap", "message": "继续"},
            session_id="session_overlap",
        ),
        _event(
            "stream_status",
            started_at + timedelta(milliseconds=100),
            {**turn_payload, "phase": "stepping", "iteration": 1},
            session_id="session_overlap",
        ),
        _model_span(
            started_at,
            finished_after_ms=700,
            started_after_ms=100,
            duration_ms=600,
            operation="step.plan",
            turn_id="msg_overlap",
            session_id="session_overlap",
        ),
        _model_span(
            started_at,
            finished_after_ms=900,
            started_after_ms=300,
            duration_ms=600,
            operation="knowledge.route",
            turn_id="msg_overlap",
            session_id="session_overlap",
        ),
        _event(
            "step_result",
            started_at + timedelta(milliseconds=900),
            {**turn_payload, "reply": "已完成"},
            session_id="session_overlap",
        ),
        _event(
            "assistant_message_created",
            started_at + timedelta(milliseconds=1000),
            {**turn_payload, "message_id": "msg_overlap_assistant", "reply": "已完成"},
            session_id="session_overlap",
        ),
    ]

    trace = enrich_turn_traces_with_timings(
        _build_turn_traces(messages, events, {}),
        events,
    )[0]

    assert trace["duration_ms"] == 1000
    assert trace["model_duration_ms"] == 800
    assert trace["model_call_count"] == 2
    assert all("duration_ms" in line for line in trace["lines"])
    assert all(line["model_duration_ms"] <= line["duration_ms"] for line in trace["lines"])


def test_enterprise_trace_without_model_spans_does_not_report_fake_zero() -> None:
    started_at = datetime(2026, 8, 2, 12, 0, 0)
    payload = {
        "turn_id": "msg_channel",
        "user_message_id": "msg_channel",
        "client_turn_id": "channel_event",
    }
    messages = [
        Message(
            id="msg_channel",
            tenant_id="tenant_demo",
            session_id="session_channel",
            role="user",
            content="你可以做什么",
            created_at=started_at,
        ),
        Message(
            id="msg_channel_answer",
            tenant_id="tenant_demo",
            session_id="session_channel",
            role="assistant",
            content="我可以查询制度。",
            created_at=started_at + timedelta(seconds=2),
        ),
    ]
    events = [
        _event(
            "user_message_received",
            started_at,
            {**payload, "message_id": "msg_channel"},
            session_id="session_channel",
        ),
        _event(
            "router_decision_created",
            started_at + timedelta(seconds=1),
            {**payload, "decision": "answer_only", "user_intent": "询问能力"},
            session_id="session_channel",
        ),
        _event(
            "assistant_message_created",
            started_at + timedelta(seconds=2),
            {**payload, "message_id": "msg_channel_answer"},
            session_id="session_channel",
        ),
    ]

    trace = enrich_turn_traces_with_timings(
        _build_turn_traces(messages, events, {}),
        events,
    )[0]

    assert trace["duration_ms"] == 2000
    assert "model_call_count" not in trace
    assert "model_duration_ms" not in trace
    assert "model_names" not in trace
    lines = {line["id"]: line for line in trace["lines"]}
    assert lines["decision_router"]["duration_ms"] == 1000


def test_enterprise_trace_does_not_time_instantaneous_skill_transition() -> None:
    started_at = datetime(2026, 8, 2, 13, 0, 0)
    payload = {
        "turn_id": "msg_skill",
        "user_message_id": "msg_skill",
        "client_turn_id": "skill_client_turn",
    }
    messages = [
        Message(
            id="msg_skill",
            tenant_id="tenant_demo",
            session_id="session_skill",
            role="user",
            content="申请营业执照",
            created_at=started_at,
        ),
        Message(
            id="msg_skill_answer",
            tenant_id="tenant_demo",
            session_id="session_skill",
            role="assistant",
            content="请提供公司名称。",
            created_at=started_at + timedelta(seconds=4),
        ),
    ]
    events = [
        _event(
            "user_message_received",
            started_at,
            {**payload, "message_id": "msg_skill"},
            session_id="session_skill",
        ),
        _event(
            "router_decision_created",
            started_at + timedelta(seconds=2),
            {
                **payload,
                "decision": "start_new_task",
                "target_skill_id": "cert_guide",
                "target_step_id": "collect_info",
                "user_intent": "申请营业执照",
            },
            session_id="session_skill",
        ),
        _event(
            "skill_started",
            started_at + timedelta(milliseconds=2002),
            {
                **payload,
                "to_skill_id": "cert_guide",
                "to_step_id": "collect_info",
            },
            session_id="session_skill",
        ),
        _event(
            "assistant_message_created",
            started_at + timedelta(seconds=4),
            {**payload, "message_id": "msg_skill_answer"},
            session_id="session_skill",
        ),
    ]

    trace = enrich_turn_traces_with_timings(
        _build_turn_traces(messages, events, {"cert_guide": "资质证照指引"}),
        events,
    )[0]

    lines = {line["id"]: line for line in trace["lines"]}
    assert lines["decision_router"]["duration_ms"] == 2000
    assert "duration_ms" not in lines["skill_state_cert_guide_active_collect_info"]


def _event(
    event_type: str,
    created_at: datetime,
    payload: dict,
    *,
    session_id: str = "session_demo",
) -> AgentEvent:
    return AgentEvent(
        tenant_id="tenant_demo",
        session_id=session_id,
        event_type=event_type,
        payload_json=payload,
        created_at=created_at,
    )


def _model_span(
    turn_started_at: datetime,
    *,
    finished_after_ms: int,
    started_after_ms: int,
    duration_ms: int,
    operation: str,
    turn_id: str = "turn_client",
    session_id: str = "session_demo",
    task_frame_id: str = "",
    iteration: int | None = None,
) -> AgentEvent:
    return _event(
        "llm_call_finished",
        turn_started_at + timedelta(milliseconds=finished_after_ms),
        {
            "turn_id": turn_id,
            "operation": operation,
            "started_at": (
                turn_started_at + timedelta(milliseconds=started_after_ms)
            ).isoformat(),
            "duration_ms": duration_ms,
            "model_name": "GLM Test",
            **({"task_frame_id": task_frame_id} if task_frame_id else {}),
            **({"iteration": iteration} if iteration is not None else {}),
        },
        session_id=session_id,
    )
