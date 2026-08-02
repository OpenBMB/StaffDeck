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
    lines = {line["id"]: line for line in trace["lines"]}
    assert lines["decision_router"]["duration_ms"] == 1550
    assert lines["decision_router"]["model_duration_ms"] == 1500
    assert lines["harness_frame_task_demo"]["duration_ms"] == 4000
    assert lines["harness_frame_task_demo"]["model_duration_ms"] == 2400
    assert lines["harness_action_task_demo_1"]["duration_ms"] == 2800
    assert lines["harness_action_task_demo_1"]["model_duration_ms"] == 1600
    assert lines["harness_finish_task_demo_2"]["duration_ms"] == 950
    assert lines["harness_finish_task_demo_2"]["model_duration_ms"] == 800
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
        },
        session_id=session_id,
    )
