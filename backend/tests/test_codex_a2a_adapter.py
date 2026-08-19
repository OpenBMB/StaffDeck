from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.a2a import codex_adapter
from app.db.models import A2ATaskEvent, A2ATaskRun


def _client(monkeypatch, tmp_path) -> tuple[TestClient, object]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    settings = SimpleNamespace(
        codex_a2a_enabled=True,
        codex_a2a_token="",
        codex_a2a_workspace_root=str(tmp_path / "workspaces"),
        codex_a2a_command="codex",
        codex_a2a_timeout_seconds=30,
    )
    monkeypatch.setattr(codex_adapter, "engine", engine)
    monkeypatch.setattr(codex_adapter, "get_settings", lambda: settings)
    monkeypatch.setattr(codex_adapter, "_launch", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(codex_adapter.router)
    return TestClient(app), engine


def test_codex_a2a_uses_public_task_id_and_supports_continuation(monkeypatch, tmp_path) -> None:
    client, engine = _client(monkeypatch, tmp_path)
    message = {
        "messageId": "message-1",
        "role": "ROLE_USER",
        "parts": [{"text": "First request"}],
    }

    submitted = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "SendMessage",
            "params": {"message": message},
        },
    )

    assert submitted.status_code == 200
    task = submitted.json()["result"]
    assert task["status"]["state"] == "submitted"
    assert task["id"]
    with Session(engine) as db:
        stored = db.exec(select(A2ATaskRun)).one()
        assert stored.id != task["id"]
        assert stored.remote_task_id == task["id"]
        stored.status = "input-required"
        db.add(stored)
        db.commit()

    continued = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-2",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "message-2",
                    "role": "ROLE_USER",
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "parts": [{"text": "Follow-up input"}],
                }
            },
        },
    )

    assert continued.status_code == 200
    continued_task = continued.json()["result"]
    assert continued_task["id"] == task["id"]
    assert continued_task["contextId"] == task["contextId"]
    assert continued_task["status"]["state"] == "submitted"

    repeated_first_turn = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-3",
            "method": "SendMessage",
            "params": {"message": message},
        },
    ).json()["result"]
    assert repeated_first_turn["id"] == task["id"]
    with Session(engine) as db:
        stored = db.exec(select(A2ATaskRun)).one()
        assert stored.invocation_id == "message-1"
        message_ids = {
            event.external_event_id
            for event in db.exec(select(A2ATaskEvent)).all()
            if event.external_event_id
        }
        assert message_ids == {"message-1", "message-2"}


def test_codex_a2a_get_cancel_and_list_tasks(monkeypatch, tmp_path) -> None:
    client, _ = _client(monkeypatch, tmp_path)
    submitted = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "message-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "Do work"}],
                }
            },
        },
    ).json()["result"]

    fetched = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-2",
            "method": "GetTask",
            "params": {"id": submitted["id"]},
        },
    )
    assert fetched.status_code == 200
    assert fetched.json()["result"]["id"] == submitted["id"]

    listed = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-3",
            "method": "ListTasks",
            "params": {"contextId": submitted["contextId"]},
        },
    )
    assert [task["id"] for task in listed.json()["result"]["tasks"]] == [submitted["id"]]

    canceled = client.post(
        "/api/a2a/codex",
        json={
            "jsonrpc": "2.0",
            "id": "request-4",
            "method": "CancelTask",
            "params": {"id": submitted["id"]},
        },
    )
    assert canceled.status_code == 200
    assert canceled.json()["result"]["status"]["state"] == "canceled"


def test_codex_a2a_message_id_is_idempotent(monkeypatch, tmp_path) -> None:
    client, engine = _client(monkeypatch, tmp_path)
    payload = {
        "jsonrpc": "2.0",
        "id": "request-idempotent",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "same-message",
                "role": "ROLE_USER",
                "parts": [{"text": "Do work once"}],
            }
        },
    }

    first = client.post("/api/a2a/codex", json=payload).json()["result"]
    second = client.post("/api/a2a/codex", json=payload).json()["result"]

    assert second["id"] == first["id"]
    with Session(engine) as db:
        assert len(list(db.exec(select(A2ATaskRun)).all())) == 1
