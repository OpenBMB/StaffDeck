from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import httpx
from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.external_business_tasks import (
    ExternalTaskCallback,
    external_business_task_callback,
    get_external_business_task,
)
from app.db.models import (
    ChatSession,
    ExternalBusinessTask,
    ExternalBusinessTaskEvent,
    HarnessTaskFrameRecord,
    Tenant,
    Tool,
    User,
)
from app.core.task_frame_store import TaskFrameStore, planned_frame_from_record
from app.session.session_schema import TurnPlan
from app.tools.external_tasks import callback_token_hash, poll_due_external_tasks
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult


class _Client:
    response_json: ClassVar[dict] = {"taskId": "provider-42", "status": "queued"}
    request_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method, url, **kwargs):
        del method, url
        type(self).request_headers = kwargs.get("headers", {})
        return httpx.Response(
            202,
            json=type(self).response_json,
            request=httpx.Request("POST", "https://provider.test/tasks"),
        )


def test_detached_submission_persists_task_and_returns_query_guidance(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.tools.tool_executor.httpx.Client",
        lambda **_: (_ for _ in ()).throw(AssertionError("Provider must not run during submit")),
    )
    with _session() as db:
        user, tool = _seed(db)
        result = ToolExecutor(db).execute(
            "tenant_demo",
            ToolCall(name=tool.name, arguments={"order": "A-1"}),
            user_id=user.id,
        )

        task = db.exec(select(ExternalBusinessTask)).one()
        assert result.success is True
        assert result.data["accepted"] is True
        assert result.data["task_id"] == task.id
        assert f"/external-business-tasks/{task.id}?" in result.data["status_query"]["path"]
        assert "tenant_id=tenant_demo&tool_id=tool_detached" in result.data["status_query"]["path"]
        assert task.external_task_id == task.id
        assert task.status == "queued"
        assert "下单任务" not in result.data["status_query"]["guidance"]


def test_detached_submission_does_not_require_provider_task_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.tools.tool_executor.httpx.Client",
        lambda **_: (_ for _ in ()).throw(AssertionError("Provider must not run during submit")),
    )
    with _session() as db:
        user, tool = _seed(db)
        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name=tool.name), user_id=user.id
        )
        task = db.exec(select(ExternalBusinessTask)).one()
        assert result.success is True
        assert result.data["task_id"] == task.id
        assert task.status == "queued"


def test_staffdeck_worker_executes_original_sync_tool(monkeypatch) -> None:
    with _session() as db:
        user, tool = _seed(db)
        task = ExternalBusinessTask(
            tenant_id="tenant_demo", user_id=user.id, tool_id=tool.id,
            external_task_id="exttask-1", status="queued",
            callback_token_hash=callback_token_hash("token"),
            request_json={"employee_id": "12343565"},
        )
        db.add(task)
        db.commit()
        monkeypatch.setattr(
            "app.tools.tool_executor.ToolExecutor.execute_sync_http",
            lambda *_args, **_kwargs: ToolResult(
                tool_name=tool.name,
                success=True,
                data={"remaining": 20000},
            ),
        )
        assert poll_due_external_tasks(db) == 1
        db.refresh(task)
        assert task.status == "completed"
        assert task.result_json == {"remaining": 20000}


def test_callback_auth_dedupe_and_user_owned_query() -> None:
    with _session() as db:
        user, tool = _seed(db)
        other = User(
            id="user_other", tenant_id="tenant_demo", username="other", password_hash="x"
        )
        db.add(other)
        token = "opaque-callback-secret"
        task = ExternalBusinessTask(
            id="exttask_1",
            tenant_id="tenant_demo",
            user_id=user.id,
            tool_id=tool.id,
            external_task_id="provider-42",
            status="accepted",
            callback_token_hash=callback_token_hash(token),
        )
        db.add(task)
        db.commit()

        request = ExternalTaskCallback(
            event_id="event-1", status="completed", result={"receipt": "ok"}
        )
        try:
            external_business_task_callback(task.id, request, "wrong-token", db)
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("callback must require its per-task credential")
        first = external_business_task_callback(task.id, request, token, db)
        duplicate = external_business_task_callback(task.id, request, token, db)
        assert first == {"accepted": True, "duplicate": False, "status": "completed"}
        assert duplicate["duplicate"] is True
        assert len(db.exec(select(ExternalBusinessTaskEvent)).all()) == 1

        result = get_external_business_task("provider-42", "tenant_demo", None, db, user)
        assert result["status"] == "completed"
        assert result["result"] == {"receipt": "ok"}
        try:
            get_external_business_task("provider-42", "tenant_demo", None, db, other)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("another user must not read this task")


def test_polling_only_updates_external_task_state(monkeypatch) -> None:
    with _session() as db:
        user, tool = _seed(db)
        task = ExternalBusinessTask(
            tenant_id="tenant_demo",
            user_id=user.id,
            tool_id=tool.id,
            external_task_id="provider-42",
            status="accepted",
            callback_token_hash=callback_token_hash("token"),
            status_url="https://provider.test/tasks/provider-42",
            status_config_json={
                "status_field": "task.state",
                "result_field": "task.output",
                "status_mapping": {"done": "completed"},
            },
            next_poll_at=tool.created_at,
        )
        db.add(task)
        db.commit()
        monkeypatch.setattr(
            "app.tools.external_tasks.httpx.get",
            lambda *_args, **_kwargs: SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"task": {"state": "done", "output": {"value": 9}}},
            ),
        )

        assert poll_due_external_tasks(db) == 1
        db.refresh(task)
        assert task.status == "completed"
        assert task.result_json == {"value": 9}
        assert task.poll_attempts == 1


def test_completed_external_task_marks_sop_frame_ready_to_resume() -> None:
    with _session() as db:
        user, tool = _seed(db)
        session = ChatSession(
            id="session_order",
            tenant_id="tenant_demo",
            user_id=user.id,
            agent_id="agent_order",
        )
        frame = HarnessTaskFrameRecord(
            tenant_id="tenant_demo",
            session_id=session.id,
            source_turn_id="turn_order",
            task_id="sop_order",
            kind="sop",
            status="waiting_external_task",
            skill_id="order_sop",
            step_id="submit_order",
            slots_json={"employee_id": "14534"},
        )
        task = ExternalBusinessTask(
            tenant_id="tenant_demo",
            user_id=user.id,
            session_id=session.id,
            task_frame_id=frame.task_id,
            tool_id=tool.id,
            external_task_id="provider-order-1",
            status="accepted",
            callback_token_hash=callback_token_hash("token"),
            resume_step_id="confirm_order",
        )
        db.add(session)
        db.add(frame)
        db.add(task)
        db.commit()

        result = external_business_task_callback(
            task.id,
            ExternalTaskCallback(
                event_id="order-complete-1",
                status="completed",
                result={"order_id": "ORD-001"},
            ),
            "token",
            db,
        )

        db.refresh(frame)
        assert result["status"] == "completed"
        assert frame.status == "ready_to_resume"
        assert frame.step_id == "confirm_order"
        assert frame.slots_json["order_id"] == "ORD-001"
        assert frame.result_json["external_task_result"] == {"order_id": "ORD-001"}


def test_ready_sop_frame_is_queued_on_the_next_turn() -> None:
    with _session() as db:
        user, _tool = _seed(db)
        session = ChatSession(
            id="session_resume",
            tenant_id="tenant_demo",
            user_id=user.id,
            agent_id="agent_order",
            active_skill_id="order_sop",
            active_step_id="confirm_order",
        )
        frame = HarnessTaskFrameRecord(
            tenant_id="tenant_demo",
            session_id=session.id,
            source_turn_id="turn_submit",
            task_id="sop_resume",
            kind="sop",
            decision="continue_active",
            status="ready_to_resume",
            skill_id="order_sop",
            step_id="confirm_order",
            slots_json={"order_id": "ORD-001"},
        )
        db.add(session)
        db.add(frame)
        db.commit()

        records = TaskFrameStore(db).persist_plan(
            session,
            "turn_resume",
            TurnPlan(
                decision="switch_to_pending",
                selected_task_id=frame.task_id,
                task_frames=[planned_frame_from_record(frame)],
            ),
        )

        assert len(records) == 1
        assert records[0].status == "queued"
        assert records[0].step_id == "confirm_order"
        assert records[0].slots_json["order_id"] == "ORD-001"
        TaskFrameStore(db).mark_running(records[0])
        assert records[0].status == "running"


def test_create_all_migrates_existing_database_with_external_task_tables() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR PRIMARY KEY, name VARCHAR)"))

    SQLModel.metadata.create_all(engine)

    tables = set(inspect(engine).get_table_names())
    assert {"external_business_tasks", "external_business_task_events"} <= tables


def _seed(db: Session) -> tuple[User, Tool]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(
        id="user_owner", tenant_id="tenant_demo", username="owner", password_hash="x"
    )
    tool = Tool(
        id="tool_detached",
        tenant_id="tenant_demo",
        name="orders.submit",
        method="POST",
        url="https://provider.test/tasks",
        config_json={
            "execution": {
                "execution_mode": "detached",
                "timeout_seconds": 8,
                "status_url": "https://provider.test/tasks/{taskId}",
                "poll_interval_seconds": 5,
            }
        },
    )
    db.add(user)
    db.add(tool)
    db.commit()
    return user, tool


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
