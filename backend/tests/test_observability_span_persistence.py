from __future__ import annotations

import json

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import AgentEvent, Tenant
from app.observability import EventLog, persist_spans
from app.observability.spans import emit_span_event, start_llm_call

USAGE = {
    "input_tokens": 212,
    "output_tokens": 10,
    "total_tokens": 222,
    "cached_input_tokens": 154,
}

def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine

def _seed(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()

def _events(db: Session) -> list[AgentEvent]:
    return list(db.exec(select(AgentEvent)).all())

def test_llm_usage_is_persisted_inside_the_context() -> None:
    """上下文内的模型调用留下带 token 用量的 llm_call_finished 行。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(
            db,
            tenant_id="tenant_demo",
            session_id="session_1",
            client_turn_id="run_1",
        ):
            span = start_llm_call(model="demo-model")
            span.finish(**USAGE)

        rows = _events(db)
        finished = [row for row in rows if row.event_type == "llm_call_finished"]
        assert len(finished) == 1
        payload = finished[0].payload_json
        assert payload["input_tokens"] == 212
        assert payload["output_tokens"] == 10
        assert payload["cached_input_tokens"] == 154
        assert payload["model"] == "demo-model"
        # 关联字段:用量要能按运行归集
        assert payload["client_turn_id"] == "run_1"
        assert finished[0].tenant_id == "tenant_demo"
        assert finished[0].session_id == "session_1"

def test_no_sink_bound_means_no_record() -> None:
    """回归基线:不绑定时同样的调用不留任何痕迹——这正是被修复的缺口。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        span = start_llm_call(model="demo-model")
        span.finish(**USAGE)
        assert _events(db) == []

def test_sink_is_released_after_the_context() -> None:
    """离开上下文后不再捕获,避免把无关调用记到该会话名下。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            start_llm_call(model="demo-model").finish(**USAGE)
        before = len(_events(db))

        start_llm_call(model="demo-model").finish(**USAGE)
        assert len(_events(db)) == before

@pytest.mark.parametrize(
    ("tenant_id", "session_id"),
    [("", "session_1"), ("tenant_demo", ""), ("", "")],
)
def test_missing_identifiers_degrade_to_noop(tenant_id: str, session_id: str) -> None:
    """缺租户或会话时静默跳过:可观测性不能让业务执行失败。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id=tenant_id, session_id=session_id):
            start_llm_call(model="demo-model").finish(**USAGE)
        assert _events(db) == []

def test_sink_failure_does_not_break_execution(monkeypatch) -> None:
    """写入失败不得中断业务:span 发射侧已兜底,这里锁定该保证。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        def boom(*_args, **_kwargs):
            raise RuntimeError("事件写入失败")
        monkeypatch.setattr(EventLog, "record", boom)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            emit_span_event("llm_call_finished", dict(USAGE))

def test_scheduled_task_run_persists_model_usage(monkeypatch) -> None:
    from datetime import datetime

    import app.scheduled_tasks.service as service
    from app.db.models import AgentProfile, ChatSession, ScheduledTask, ScheduledTaskRun
    from app.session.session_schema import ChatTurnResponse

    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        db.add(
            AgentProfile(
                id="agent_demo",
                tenant_id="tenant_demo",
                name="巡检员",
                status="active",
            )
        )
        db.add(
            ChatSession(
                id="session_sched",
                tenant_id="tenant_demo",
                agent_id="agent_demo",
                status="active",
            )
        )
        task = ScheduledTask(
            id="task_1",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            created_by_user_id="user_1",
            title="每日巡检",
            prompt="巡检",
        )
        run = ScheduledTaskRun(
            id="run_1",
            tenant_id="tenant_demo",
            scheduled_task_id="task_1",
            agent_id="agent_demo",
            user_id="user_1",
            scheduled_for=datetime(2026, 8, 26, 9, 0, 0),
            session_id="session_sched",
        )
        db.add(task)
        db.add(run)
        db.commit()

        class FakeLoop:
            def __init__(self, _db):
                pass

            def handle_turn_stream(self, request):
                # 模拟一次真实的模型调用
                start_llm_call(model="demo-model").finish(**USAGE)
                yield {
                    "event": "complete",
                    "data": ChatTurnResponse(
                        reply="巡检完成",
                        session_id=request.session_id,
                        session_state="active",
                    ).model_dump(),
                }
        monkeypatch.setattr(service, "AgentLoop", FakeLoop)
        service._execute_prepared_scheduled_task(db, task, run, manual=True)

        finished = [
            row for row in _events(db) if row.event_type == "llm_call_finished"
        ]
        assert len(finished) == 1, "定时任务运行未落下模型用量"
        assert finished[0].session_id == "session_sched"
        assert finished[0].payload_json["total_tokens"] == 222
        assert finished[0].payload_json["client_turn_id"] == "run_1"

def test_llm_span_bodies_are_not_persisted() -> None:
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            span = start_llm_call(model="demo-model")
            span.finish(
                **USAGE,
                request_messages=[{"role": "user", "content": "机密提示词"}],
                request_payload={"messages": [{"content": "机密提示词"}]},
                response_text="机密回复",
                response_message={"content": "机密回复"},
                response_payload={"choices": [{"text": "机密回复"}]},
            )

        payload = _events(db)[-1].payload_json
        for dropped in (
            "request_messages",
            "request_payload",
            "response_text",
            "response_message",
            "response_payload",
        ):
            assert dropped not in payload, f"{dropped} 不应落库"
        assert "机密" not in json.dumps(payload, ensure_ascii=False)
        assert payload["total_tokens"] == 222
        assert payload["cached_input_tokens"] == 154
        assert payload["model"] == "demo-model"
        assert payload["span_id"]

def test_non_llm_spans_pass_through_unchanged() -> None:
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            emit_span_event(
                "knowledge_span_finished",
                {"operation": "knowledge.search", "query_chars": 12, "max_chunks": 5},
            )

        payload = _events(db)[-1].payload_json
        assert payload["query_chars"] == 12
        assert payload["max_chunks"] == 5

def test_projection_keeps_fields_existing_consumers_need() -> None:
    required = {
        "span_id",
        "operation",
        "model_name",
        "task_frame_id",
        "iteration",
        "attempt",
        "max_attempts",
        "json_attempt",
        "json_max_attempts",
        "started_at",
        "duration_ms",
        "model",
        "error_type",
        "error",
        "request_parameters",
    }
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            emit_span_event(
                "llm_call_finished",
                {key: f"v_{key}" for key in required} | {"request_payload": {"x": 1}},
            )
        payload = _events(db)[-1].payload_json
        assert required <= set(payload), f"白名单遗漏:{sorted(required - set(payload))}"
        assert "request_payload" not in payload


def test_trimmed_rows_are_marked() -> None:
    """裁剪过的行带 bodies_omitted,避免"未留存"被误读成"模型返回为空"。"""
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        with persist_spans(db, tenant_id="tenant_demo", session_id="session_1"):
            emit_span_event("llm_call_finished", {**USAGE, "response_text": "原文"})
            emit_span_event("llm_call_finished", dict(USAGE))  # 无原文可裁
            emit_span_event("knowledge_span_finished", {"query_chars": 3})

        trimmed, untouched, other = [row.payload_json for row in _events(db)]
        assert trimmed["bodies_omitted"] is True
        assert "bodies_omitted" not in untouched, "没裁掉任何东西时不应加标记"
        assert "bodies_omitted" not in other, "非 llm_call 不应加标记"
