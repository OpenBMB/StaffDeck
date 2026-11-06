from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from app.tools import external_task_worker as worker
from app.tools import external_tasks as tasks
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolResult, ToolTestRequest
from app.db.models import ExternalBusinessTask, utc_now
from test_external_business_tasks import _seed, _session


def test_worker_recovers_from_exception_using_fresh_session(monkeypatch):
    calls, sessions = [], []
    stop = threading.Event()
    monkeypatch.setattr(worker, "_stop_event", stop)
    monkeypatch.setattr(
        worker, "Session", lambda _: sessions.append(object()) or nullcontext(sessions[-1])
    )
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(external_task_poll_seconds=0.01)
    )

    def poll(db):
        calls.append(db)
        if len(calls) == 1:
            raise RuntimeError("simulated transaction failure")
        stop.set()

    monkeypatch.setattr(worker, "poll_due_external_tasks", poll)
    thread = threading.Thread(target=worker.run_external_task_worker)
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert len(calls) == 2 and calls[0] is not calls[1]


def test_worker_shutdown_does_not_spawn_overlapping_generation(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, "_thread", None)
    monkeypatch.setattr(worker, "_stop_event", threading.Event())
    monkeypatch.setattr(worker, "Session", lambda _: nullcontext(None))

    def poll(_):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(worker, "poll_due_external_tasks", poll)
    worker.start_external_task_worker()
    try:
        assert entered.wait(2)
        old = worker._thread
        assert not worker.stop_external_task_worker(0.01)
        worker.start_external_task_worker()
        assert worker._thread is old
    finally:
        release.set()
        assert worker.stop_external_task_worker(2)


@pytest.mark.parametrize("provider_id", [0, "0", 1])
def test_http_200_task_id_without_status_is_polled_to_completion(monkeypatch, provider_id):
    submitted, urls = [], []

    def submit(*args, **kwargs):
        submitted.append(1)
        return SimpleNamespace(
            status_code=200,
            result=ToolResult(
                tool_name="test",
                success=True,
                data={"taskId": provider_id},
            ),
        )

    def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(
            200, json={"status": "completed", "result": "done"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(ToolExecutor, "execute_http_with_metadata", submit)
    monkeypatch.setattr(tasks.httpx, "get", get)
    with _session() as db:
        user, tool = _seed(db)
        tool.config_json = {
            "execution": {
                "execution_mode": "detached",
                "async_strategy": "provider_task",
                "status_url": "https://provider.test/status/{taskId}",
            }
        }
        db.add(tool)
        db.commit()
        ToolExecutor(db).execute("tenant_demo", ToolCall(name=tool.name), user_id=user.id)
        tasks.poll_due_external_tasks(db)
        task = db.exec(select(ExternalBusinessTask)).one()
        assert task.external_task_id == str(provider_id)
        assert task.status == "accepted" and task.next_poll_at is not None
        task.next_poll_at = utc_now() - timedelta(seconds=1)
        db.add(task)
        db.commit()
        tasks.poll_due_external_tasks(db)
        db.refresh(task)
        assert task.status == "completed"
        assert task.result_json == {"value": "done"}
        assert submitted == [1]
        assert urls == [f"https://provider.test/status/{provider_id}"]


def test_one_poisoned_submission_does_not_block_other_queued_jobs(monkeypatch):
    with _session() as db:
        user, tool = _seed(db)
        executor = ToolExecutor(db)
        a = executor.execute("tenant_demo", ToolCall(name=tool.name), user_id=user.id).data[
            "task_id"
        ]
        b = executor.execute("tenant_demo", ToolCall(name=tool.name), user_id=user.id).data[
            "task_id"
        ]
        calls = []

        def execute(db, task):
            calls.append(task.id)
            if task.id == a:
                raise RuntimeError("poisoned job")
            task.status = "completed"
            db.add(task)
            db.commit()

        monkeypatch.setattr(tasks, "_execute_local_task", execute)
        tasks.poll_due_external_tasks(db)
        assert calls == [a, b]
        assert db.get(ExternalBusinessTask, b).status == "completed"


def test_resume_query_cannot_submit_pending_task_again():
    with _session() as db:
        user, tool = _seed(db)
        executor = ToolExecutor(db)
        first = executor.execute(
            "tenant_demo",
            ToolCall(name=tool.name),
            user_id=user.id,
            session_id="s",
            task_frame_id="frame",
            invocation_id="call1",
        )
        again = executor.execute(
            "tenant_demo",
            ToolCall(name=tool.name),
            user_id=user.id,
            session_id="s",
            task_frame_id="frame",
            invocation_id="call2",
        )
        assert first.data["task_id"] == again.data["task_id"]
        assert len(db.exec(select(ExternalBusinessTask)).all()) == 1


def test_saved_tool_test_retries_use_same_request_id():
    from app.api.tools import test_tool as invoke
    from app.agents.branching import ensure_open_gallery_binding
    from app.db.models import AgentProfile

    with _session() as db:
        user, tool = _seed(db)
        user.role = "admin"
        db.add(AgentProfile(id="overall", tenant_id="tenant_demo", name="All", is_overall=True))
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id)
        db.commit()
        request = ToolTestRequest(tenant_id="tenant_demo", client_request_id="request-1")
        first = invoke(tool.id, request, agent_id=None, db=db, current_user=user)
        second = invoke(tool.id, request, agent_id=None, db=db, current_user=user)
        assert first.data["task_id"] == second.data["task_id"]
        assert len(db.exec(select(ExternalBusinessTask)).all()) == 1


def test_receipt_lookup_does_not_confuse_old_completion_with_new_submission():
    from app.core.harness_v2_engine import _external_task_for_result

    with _session() as db:
        user, tool = _seed(db)
        for task_id, status in [("old", "completed"), ("new", "submitting")]:
            db.add(
                ExternalBusinessTask(
                    id=task_id,
                    tenant_id="tenant_demo",
                    user_id=user.id,
                    tool_id=tool.id,
                    session_id="s",
                    task_frame_id="f",
                    status=status,
                    callback_token_hash="test",
                )
            )
        db.commit()
        frame = SimpleNamespace(tenant_id="tenant_demo", session_id="s", task_id="f")
        result = SimpleNamespace(structured_result={"task_id": "new"})
        assert _external_task_for_result(db, frame, result).status == "submitting"
        result.structured_result = {"task_id": "not-in-this-frame"}
        assert _external_task_for_result(db, frame, result) is None


def test_expired_queued_job_is_not_submitted(monkeypatch):
    with _session() as db:
        user, tool = _seed(db)
        db.add(
            ExternalBusinessTask(
                tenant_id="tenant_demo",
                user_id=user.id,
                tool_id=tool.id,
                status="queued",
                callback_token_hash="test",
                expires_at=utc_now() - timedelta(seconds=1),
            )
        )
        db.commit()
        monkeypatch.setattr(
            tasks, "_execute_local_task", lambda *a: pytest.fail("expired task submitted")
        )
        tasks.poll_due_external_tasks(db)
        assert db.exec(select(ExternalBusinessTask)).one().status == "expired"
