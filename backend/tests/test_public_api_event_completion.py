from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from test_public_api_v1 import _client, _tenant_key

from app.db.models import APIJob, APIJobEvent, User
from app.public_api import jobs
from app.public_api.auth import PublicPrincipal


@pytest.mark.parametrize("late_writer", ["event", "cancel", "recovery"])
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
def test_concurrent_terminalization_fences_late_writes(
    monkeypatch, tmp_path, late_writer, terminal_status,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'completion.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(jobs, "engine", engine)
    with Session(engine) as db:
        job = APIJob(tenant_id="fixture", credential_id="fixture", kind="run",
                     status="running", execution_owner="owner", execution_generation=1)
        db.add(job)
        db.commit()
        run_id = job.id
        jobs.emit_job_event(db, job, "run.started", {})
        db.commit()

    ready, resume = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def pause_write(conn, cursor, statement, parameters, context, executemany):
        if (threading.get_ident() != main_thread and not ready.is_set()
                and statement.startswith("UPDATE api_jobs")):
            ready.set()
            assert resume.wait(10)

    def late_write():
        if late_writer == "recovery":
            jobs.recover_public_jobs()
        elif late_writer == "cancel":
            principal = PublicPrincipal(
                tenant_id="fixture", actor_user=User(), scopes=frozenset({"runs:cancel"}),
            )
            with Session(engine) as db:
                assert jobs.cancel_job(run_id, principal=principal, db=db).status == terminal_status
        else:
            with Session(engine) as db:
                stale = db.get(APIJob, run_id)
                assert jobs.emit_job_event(db, stale, "run.output.delta", {}) is None
                db.commit()

    event.listen(engine, "before_cursor_execute", pause_write)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(late_write)
            try:
                assert ready.wait(10)
                with Session(engine) as db:
                    terminal = jobs._terminalize_job(
                        db, db.get(APIJob, run_id), owner="owner", generation=1,
                        status=terminal_status, stage=terminal_status,
                    )
                    assert terminal is not None
                    jobs._emit_terminal_job_event(db, terminal, status=terminal_status)
            finally:
                resume.set()
            future.result(timeout=10)
        with Session(engine) as db:
            assert db.get(APIJob, run_id).status == terminal_status
            events = db.exec(select(APIJobEvent).where(APIJobEvent.job_id == run_id)
                             .order_by(APIJobEvent.sequence)).all()
            assert [(item.sequence, item.event_type) for item in events] == [
                (1, "run.started"), (2, f"run.{terminal_status}"),
            ]
    finally:
        event.remove(engine, "before_cursor_execute", pause_write)
        engine.dispose()


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "restarted"])
def test_terminal_run_exposes_stable_public_event_cursor(monkeypatch, status):
    client, engine, token = _client(monkeypatch)
    monkeypatch.setattr(jobs, "engine", engine)
    key = _tenant_key(client, token, ["runs:read", "runs:create"])
    auth = {"Authorization": f"Bearer {key}"}

    def execute(db, job):
        jobs.update_job(db, job, event_type="run.output.delta", event_data={"content": "hello"})
        if status == "failed":
            raise RuntimeError("fixture failure")
        if status == "cancelled":
            raise jobs.JobCancelled()
        return {"reply": "hello"}

    monkeypatch.setitem(jobs._handlers, "run", execute)
    with client:
        created = client.post("/agents/agent_api/runs", headers=auth, json={"input": "hello"})
        assert created.status_code == 202
        run_id = created.json()["id"]
        path = f"/runs/{run_id}"
        assert client.get(path, headers=auth).json()["final_event_id"] is None
        with Session(engine) as db:
            stale_job = db.get(APIJob, run_id)
            db.expunge(stale_job)

        if status == "restarted":
            with Session(engine) as db:
                assert jobs._claim_job(db, run_id, "fixture-owner") is not None
            jobs.recover_public_jobs()
        else:
            jobs.run_job(run_id)

        completed = client.get(path, headers=auth).json()
        assert completed["status"] == ("failed" if status == "restarted" else status)
        with Session(engine) as db:
            events = db.exec(select(APIJobEvent).where(APIJobEvent.job_id == run_id)).all()
            final_id = str(max(event.sequence for event in events if event.public))
        assert completed["final_event_id"] == final_id

        # A delayed relay holding a pre-terminal job must not move the watermark.
        with Session(engine) as db:
            jobs.update_job(db, stale_job, event_type="run.output.delta",
                            event_data={"content": "too late"})
        jobs.recover_public_jobs()
        assert client.get(path, headers=auth).json()["final_event_id"] == final_id
        stream = client.get(path + "/events", headers=auth).text
        assert "too late" not in stream
        assert f"id: {final_id}\n" in stream
    engine.dispose()


def test_final_cursor_ignores_private_events_and_does_not_certify_pruned_history(monkeypatch):
    client, engine, token = _client(monkeypatch)
    key = _tenant_key(client, token, ["runs:read", "runs:create"])
    auth = {"Authorization": f"Bearer {key}"}
    with client:
        created = client.post("/agents/agent_api/runs", headers=auth, json={"input": "hello"})
        run_id = created.json()["id"]
        with Session(engine) as db:
            job = db.get(APIJob, run_id)
            job.status = "failed"
            db.add(job)
            db.add(APIJobEvent(tenant_id=job.tenant_id, job_id=run_id, sequence=2,
                               event_type="private", public=False))
            db.commit()
        path = f"/runs/{run_id}"
        assert client.get(path, headers=auth).json()["final_event_id"] == "1"
        with Session(engine) as db:
            for event in db.exec(select(APIJobEvent).where(APIJobEvent.job_id == run_id)).all():
                db.delete(event)
            db.commit()
        assert client.get(path, headers=auth).json()["final_event_id"] is None
    engine.dispose()
