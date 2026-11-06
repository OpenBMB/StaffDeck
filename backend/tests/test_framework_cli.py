from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlmodel import Session, SQLModel, create_engine

from app.api.auth import router as auth_router
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    APIJob,
    APIJobEvent,
    ChatSession,
    Message,
    Tenant,
    User,
)
from app.public_api.app import create_public_api_app
from app.public_api.jobs import job_read
from app.security.auth import hash_password
from app.session.helpers import public_session
from app.session.session_schema import ChatTurnResponse
from staffdeck import StaffDeck
from staffdeck.profiles import ProfileStore, connection_url, local_login

ROOT = Path(__file__).resolve().parents[2]


def test_profile_atomic_private_and_origin_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("STAFFDECK_CONFIG_DIR", str(tmp_path / "config"))
    store = ProfileStore("dev")
    assert store.load() is None
    value = {"base_url": "http://127.0.0.1:5173", "api_key": "sd_live_secret",
             "credential_id": "key1"}
    store.save_new(value)
    assert store.load()["base_url"] == "http://127.0.0.1:5173/api/v1"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        store.save_new({**value, "api_key": "replacement"})
    assert store.load()["api_key"] == "sd_live_secret"
    if os.name != "nt":
        store.path.chmod(0o644)
        with pytest.raises(ValueError):
            store.load()
        store.path.chmod(0o600)
    store.remove()
    victim = tmp_path / "victim"
    victim.write_text(json.dumps(value))
    if os.name != "nt":
        store.path.symlink_to(victim)
        with pytest.raises(OSError):
            store.load()


@pytest.mark.parametrize("url", [
    "http://example.com", "https://user:pass@example.com", "https://example.com?q=1",
    "https://example.com/#fragment", "file:///tmp/token", "https://example.com/not-api",
])
def test_login_rejects_insecure_or_ambiguous_destination(url):
    with pytest.raises(ValueError):
        connection_url(url)


def test_login_preserves_proxy_prefix_and_does_not_retry_writes():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "browser-token", "user": {"id": "member"}})
        return httpx.Response(200, json={"id": "key1", "api_key": "sd_live_key"})

    result = local_login("https://example.test/test/api/v1", "tenant", "member", "password",
                         transport=httpx.MockTransport(handler))
    assert [request.url.path for request in seen] == [
        "/test/api/auth/login", "/test/api/auth/me/api-credentials",
    ]
    assert seen[1].headers["authorization"] == "Bearer browser-token"
    assert "password" not in result and "token" not in result
    assert "browser-token" not in json.dumps(result)


def test_artifact_download_does_not_publish_partial_or_overwrite(tmp_path):
    target = tmp_path / "report.docx"
    with StaffDeck(base_url="https://example.test", api_key="secret",
                   transport=httpx.MockTransport(lambda request: httpx.Response(
                       200, content=b"artifact bytes", headers={"X-Request-ID": "artifact-read"},
                   ))) as client:
        result = client.runs.download_artifact(
            "run1", "frame1", str(target), artifact_path="report.docx",
        )
        assert result.request_id == "artifact-read" and target.read_bytes() == b"artifact bytes"
        with pytest.raises(FileExistsError):
            client.runs.download_artifact(
                "run1", "frame1", str(target), artifact_path="report.docx",
            )
        assert target.read_bytes() == b"artifact bytes"
        assert not list(tmp_path.glob(".staffdeck-download-*"))
    class BrokenDownload(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("connection closed")

    target = tmp_path / "partial.docx"
    with StaffDeck(base_url="https://example.test", api_key="secret",
                   transport=httpx.MockTransport(lambda request: httpx.Response(
                       200, stream=BrokenDownload(),
                   ))) as client:
        from staffdeck import TransportError
        with pytest.raises(TransportError):
            client.runs.download_artifact(
                "run1", "frame1", str(target), artifact_path="report.docx",
            )
    assert not target.exists() and not list(tmp_path.glob(".staffdeck-download-*"))


def test_terminal_cursor_certifies_public_events_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cursor.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        job = APIJob(tenant_id="t", credential_id="key", kind="run", status="running")
        db.add(job)
        db.flush()
        for sequence, public in [(1, True), (2, True), (3, False)]:
            db.add(APIJobEvent(tenant_id="t", job_id=job.id, sequence=sequence,
                               event_type="test", public=public, data_json={}))
        db.commit()
        assert job_read(job, db).final_event_id is None
        job.status = "succeeded"
        db.add(job)
        db.commit()
        assert job_read(job, db).final_event_id == "2"
    engine.dispose()


@pytest.fixture
def live_cli_server(tmp_path, monkeypatch):
    """Real HTTP and real persistence; only the costly model boundary is deterministic."""
    engine = create_engine(f"sqlite:///{tmp_path / 'server.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="cli_tenant", name="CLI Acceptance"))
        db.add(User(id="cli_user", tenant_id="cli_tenant", username="member", role="member",
                    password_hash=hash_password("local-acceptance-password")))
        db.add(User(id="other_user", tenant_id="cli_tenant", username="other", role="member",
                    password_hash="unused"))
        db.add(AgentProfile(id="cli_employee", tenant_id="cli_tenant", name="CLI Employee",
                            is_overall=False, status="active",
                            metadata_json={"owner_user_id": "cli_user"}))
        db.add(AgentProfile(id="private_employee", tenant_id="cli_tenant", name="Private",
                            is_overall=False, status="active",
                            metadata_json={"owner_user_id": "other_user"}))
        db.commit()

    def sessions():
        with Session(engine) as db:
            yield db

    class DeterministicLoop:
        def __init__(self, db):
            self.db = db

        def handle_turn_stream(self, request):
            assert request.channel == "public_api"
            session = self.db.get(ChatSession, request.session_id)
            self.db.add(Message(tenant_id=request.tenant_id, session_id=session.id,
                                role="assistant", content="CLI_E2E_OK",
                                metadata_json={"client_turn_id": request.client_turn_id}))
            for event, payload in [
                ("stream_delta", {"content": "CLI_E2E_OK"}), ("stream_end", {}),
            ]:
                self.db.add(AgentEvent(tenant_id=request.tenant_id, session_id=session.id,
                                       event_type=event, payload_json={
                                           **payload, "turn_id": request.client_turn_id,
                                       }))
            self.db.commit()
            response = ChatTurnResponse(reply="CLI_E2E_OK", session_id=session.id,
                                        session_state=public_session(session))
            yield {"event": "complete", "data": response.model_dump(mode="json")}

    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.engine", engine)
    monkeypatch.setattr("app.public_api.runs.engine", engine)
    monkeypatch.setattr("app.public_api.runs.AgentLoop", DeterministicLoop)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job",
                        lambda _name, func, **_kwargs: func())
    public = create_public_api_app()
    public.dependency_overrides[get_session] = sessions
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = sessions
    app.mount("/api/v1", public)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True
    thread.join(10)
    listener.close()
    engine.dispose()


def cli_call(server, *args, stdin="", executable=None):
    url, directory = server
    env = {key: value for key, value in os.environ.items()
           if key not in {"STAFFDECK_API_KEY", "STAFFDECK_BASE_URL", "STAFFDECK_PROFILE"}}
    env.update(PYTHONPATH=os.pathsep.join([str(ROOT / "backend"), str(ROOT / "backend/src")]),
               STAFFDECK_CONFIG_DIR=str(directory / "cli_config"))
    command = [executable] if executable else [sys.executable, "-m", "staffdeck_harness.cli"]
    return subprocess.run([*command, "--output", "json", "--base-url", url, *args], input=stdin, env=env,
                          text=True, encoding="utf-8", capture_output=True, timeout=30, check=False)


@pytest.mark.parametrize("client_kind", ["source", "packaged"])
def test_real_http_cli_login_permissions_chat_sse_and_revocation(
    live_cli_server, client_kind, monkeypatch,
):
    server = live_cli_server
    if client_kind == "packaged":
        binary = os.environ.get("STAFFDECK_TEST_CLI_BINARY")
        if not binary:
            pytest.skip("Set STAFFDECK_TEST_CLI_BINARY to exercise the native distribution")
        original = cli_call

        def packaged_call(*args, **kwargs):
            return original(*args, **kwargs, executable=binary)

        monkeypatch.setattr(sys.modules[__name__], "cli_call", packaged_call)
    login = cli_call(server, "auth", "login", "--tenant-id", "cli_tenant", "--username", "member",
                     "--password-stdin", stdin="local-acceptance-password\n")
    assert login.returncode == 0, login.stderr
    assert "api_key" not in login.stdout and "local-acceptance-password" not in login.stdout
    assert cli_call(server, "auth", "status").returncode == 0
    listed = cli_call(server, "agents", "list")
    assert listed.returncode == 0, listed.stderr
    assert "cli_employee" in listed.stdout and "private_employee" not in listed.stdout
    denied = cli_call(server, "agents", "get", "--agent-id", "private_employee")
    assert denied.returncode == 1
    chat = cli_call(server, "chat", "--agent-id", "cli_employee", "--message", "hello")
    assert chat.returncode == 0, chat.stderr
    receipt, result = [json.loads(line) for line in chat.stdout.splitlines()]
    assert receipt["type"] == "receipt" and result["data"]["reply"] == "CLI_E2E_OK"
    session_id = result["data"]["session_id"]
    continued = cli_call(server, "chat", "--agent-id", "cli_employee", "--session-id", session_id,
                         "--message", "continue", "--stream")
    assert continued.returncode == 0, continued.stderr
    assert "run.output.delta" in continued.stdout and "CLI_E2E_OK" in continued.stdout
    assert "run.succeeded" in continued.stdout
    config = server[1] / "cli_config/local.json"
    profile = json.loads(config.read_text())
    with StaffDeck(base_url=server[0], api_key=profile["api_key"]) as client:
        assert client.system.whoami().data["user_id"] == "cli_user"
    assert cli_call(server, "auth", "logout").returncode == 0
    assert not config.exists()
    with httpx.Client(base_url=server[0]) as client:
        assert client.get("/api/v1/agents", headers={
            "Authorization": f"Bearer {profile['api_key']}",
        }).status_code == 401


def test_desktop_cli_dispatch_does_not_start_server(monkeypatch):
    import desktop_launcher
    monkeypatch.setattr("staffdeck_harness.cli.main", lambda args: 17 if args == ["--help"] else 99)
    monkeypatch.setattr(desktop_launcher, "_apply_network_config",
                        lambda args: pytest.fail("CLI started the server"))
    assert desktop_launcher.main(["cli", "--help"]) == 17


def test_real_http_cli_resource_writes_and_sop_publication(live_cli_server):
    server = live_cli_server
    login = cli_call(server, "auth", "login", "--tenant-id", "cli_tenant", "--username", "member",
                     "--password-stdin", stdin="local-acceptance-password\n")
    assert login.returncode == 0, login.stderr
    created = cli_call(server, "agent", "create", "--json", "-", stdin=json.dumps({
        "name": "New CLI Employee", "source_mode": "blank",
    }))
    assert created.returncode == 0, created.stderr
    agent_id = json.loads(created.stdout)["data"]["id"]

    def command(resource, method, *args, body=None):
        argv = [resource, method, "--agent-id", agent_id, *args]
        if body is not None:
            argv += ["--json", "-"]
        result = cli_call(server, *argv, stdin=json.dumps(body) if body is not None else "")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["data"]

    knowledge = command("knowledge-bases", "create", body={
        "name": "CLI Knowledge", "description": "Acceptance", "capability_scope": "general",
    })
    assert knowledge["name"] == "CLI Knowledge"
    skill = command("skills", "create", body={
        "name": "CLI Skill", "slug": "cli-skill", "status": "draft",
        "markdown": "---\nname: CLI Skill\ndescription: Acceptance\n---\n# CLI\nReturn the result.",
        "capability_scope": "general",
    })
    assert skill["slug"] == "cli-skill"
    command("skills", "publish", "--slug", "cli-skill")
    task = command("scheduled-tasks", "create", body={
        "title": "CLI paused test", "prompt": "Test only", "schedule_type": "daily",
        "schedule": {"time": "09:00"}, "timezone": "Asia/Shanghai", "status": "paused",
    })
    command("scheduled-tasks", "resume", "--task-id", task["id"])
    command("scheduled-tasks", "pause", "--task-id", task["id"])
    assert command("scheduled-tasks", "runs", "--task-id", task["id"])["data"] == []
    command("scheduled-tasks", "archive", "--task-id", task["id"])
    content = {
        "skill_id": "cli_sop", "name": "CLI SOP", "version": "1.0.0",
        "description": "Acceptance", "trigger_intents": ["CLI"],
        "nodes": [{"node_id": "answer", "type": "respond", "name": "Answer",
                   "instruction": "Reply OK", "capability_refs": {
                       "general_skill_ids": [], "tool_ids": [], "knowledge_base_ids": [],
                   }}], "edges": [], "start_node_id": "answer", "terminal_node_ids": ["answer"],
    }
    draft = command("sops", "create", body=content)
    assert draft["status"] == "draft"
    valid = command("sops", "validate", "--sop-id", "cli_sop", "--draft-id", draft["id"])
    assert valid["valid"] is True
    published = command("sops", "publish", "--sop-id", "cli_sop", "--draft-id", draft["id"])
    command("agents", "set-resources", body=[
        {"resource_type": "skill", "resource_id": published["sop"]["id"]},
        {"resource_type": "general_skill", "resource_id": skill["id"]},
    ])
    bindings = command("agents", "resources")["data"]
    assert {(row["resource_type"], row["resource_id"]) for row in bindings} == {
        ("skill", published["sop"]["id"]), ("general_skill", skill["id"]),
    }
    assert all(row["metadata"]["owner_agent_id"] == agent_id for row in bindings)
    assert any(row["id"] == skill["id"] and row["status"] == "published"
               for row in command("skills", "list")["data"])
    listed = command("sops", "list")
    assert any(row["skill_id"] == "cli_sop" and row["status"] == "published"
               for row in listed["data"]), "Resource updates hid the published private SOP"
    chat = cli_call(server, "chat", "--agent-id", agent_id, "--message", "CLI")
    assert chat.returncode == 0, chat.stderr
    result = json.loads(chat.stdout.splitlines()[-1])["data"]
    assert result["agent_id"] == agent_id and result["reply"] == "CLI_E2E_OK"
    assert cli_call(server, "auth", "logout").returncode == 0
