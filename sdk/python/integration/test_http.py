"""Real TCP + subprocess smoke test; no deployed server, secrets or LLM required."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from staffdeck import StaffDeck


@pytest.fixture
def live_api(api):
    import uvicorn
    from fastapi import FastAPI

    server, engine, token, _ = api
    headers = {"Authorization": f"Bearer {token}"}
    clients = server.get("/api-clients", headers=headers).json()
    credential = server.post(
        f"/api-clients/{clients[0]['id']}/credentials", headers=headers,
        json={"name": "socket-fixture", "scopes": ["*"]},
    )
    assert credential.status_code == 201
    key = credential.json()["api_key"]
    app = FastAPI()
    app.mount("/api/v1", server.app)
    runner = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{sock.getsockname()[1]}/api/v1"
        thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not runner.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert runner.started, "Local fixture HTTP server did not start"
            yield base_url, key, engine
        finally:
            runner.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "Local fixture HTTP server did not stop"


def test_live_api_uses_independent_connections(live_api):
    _, _, engine = live_api
    # Guard the fixture contract: request cleanup must not roll back a worker's
    # transaction through a shared StaticPool connection.
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection


def test_sdk_and_cli_over_http(live_api, monkeypatch, tmp_path):
    from app.public_api import jobs

    base_url, key, engine = live_api
    monkeypatch.setattr(jobs, "engine", engine)
    monkeypatch.setitem(jobs._handlers, "run", lambda db, job: {"reply": "local fixture"})
    env = {**os.environ, "STAFFDECK_BASE_URL": base_url, "STAFFDECK_API_KEY": key}
    # A clean wheel-only interpreter can be supplied to validate the shipped package.
    python = os.environ.get("STAFFDECK_TEST_PYTHON", sys.executable)
    if "STAFFDECK_TEST_PYTHON" in os.environ:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def cli(*args, body=None, expected=0):
        result = subprocess.run(
            [python, "-m", "staffdeck", *args], input=body, text=True, capture_output=True,
            env=env, cwd=tmp_path, timeout=10, check=False,
        )
        assert result.returncode == expected, result.stderr
        assert key not in result.stdout + result.stderr
        return result

    with StaffDeck(base_url=base_url, api_key=key) as sdk:
        assert sdk.agents.get("agent_api").data["id"] == "agent_api"
        receipt = json.loads(cli(
            "runs", "create", "--agent-id", "agent_api", "--json", "-",
            "--idempotency-key", "socket-run", body='{"input":"hello"}',
        ).stdout)
        assert receipt["status_code"] == 202
        run_id = receipt["data"]["id"]
        assert sdk.runs.get(run_id).data["status"] == "queued"
        jobs.run_job(run_id)
        assert sdk.runs.get(run_id).data["status"] == "succeeded"
        events = [json.loads(line) for line in cli("runs", "events", "--run-id", run_id).stdout.splitlines()]
        assert events[-1]["event"] == "run.succeeded"
        assert cli("runs", "events", "--run-id", run_id, "--last-event-id", events[-1]["id"]).stdout == ""
        result = json.loads(cli("runs", "wait", "--run-id", run_id).stdout)
        assert result["data"]["reply"] == "local fixture"
        env["STAFFDECK_API_KEY"] = "invalid-fixture-key"
        denied = cli("agents", "list", expected=1)
        assert not denied.stdout
        assert json.loads(denied.stderr)["error"]["status_code"] == 401


def test_coding_agent_cli_sop_knowledge_workflow(live_api, knowledge_worker, skill_card, tmp_path):
    """Shell subprocesses use only public CLI commands, as Codex/PilotDeck would."""
    _, jobs = knowledge_worker
    base_url, key, _ = live_api
    env = {**os.environ, "STAFFDECK_BASE_URL": base_url, "STAFFDECK_API_KEY": key}
    python = os.environ.get("STAFFDECK_TEST_PYTHON", sys.executable)
    if "STAFFDECK_TEST_PYTHON" in os.environ:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def cli(*args, body=None, expected=0):
        result = subprocess.run(
            [python, "-m", "staffdeck", *args],
            input=json.dumps(body, ensure_ascii=False) if body is not None else None,
            text=True, encoding="utf-8", capture_output=True, env=env, cwd=tmp_path,
            timeout=20, check=False,
        )
        assert result.returncode == expected, result.stderr
        assert key not in result.stdout + result.stderr
        if expected:
            assert not result.stdout
            return json.loads(result.stderr)["error"]
        assert not result.stderr
        return json.loads(result.stdout)

    agent = ("--agent-id", "agent_api")
    kb = cli("knowledge-bases", "create", *agent, "--json", "-", body={"name": "员工知识"})["data"]["id"]
    base = (*agent, "--knowledge-base-id", kb)
    receipt = cli("knowledge-bases", "upsert-entries", *base, "--json", "-",
                  "--idempotency-key", "cli-entry", body={
                      "entries": [{"title": "报销制度", "content": "# 报销\n交通费用可以报销。"}],
                  })
    job = receipt["data"]["id"]
    assert receipt["status_code"] == 202
    assert cli("jobs", "get", "--job-id", job)["data"]["status"] == "queued"
    jobs.run_job(job)
    doc = cli("jobs", "wait", "--job-id", job)["data"]["result"]["documents"][0]["document_id"]
    documents = cli("knowledge-bases", "documents", *base)["data"]["data"]
    current = next(d for d in documents if d["id"] == doc)
    edit = {"content_md": "# 报销\n交通和住宿费用均可报销。", "expected_updated_at": current["updated_at"]}
    updated = cli("knowledge-bases", "update-document", *base, "--document-id", doc,
                  "--json", "-", body=edit)["data"]
    assert "住宿" in updated["metadata"]["raw_text"]
    search = cli("knowledge-bases", "search", *base, "--json", "-", body={"query": "报销"})
    assert search["data"]["citations"]

    skill_card["nodes"][0]["capability_refs"]["knowledge_base_ids"] = [kb]
    card_file = tmp_path / "sop.json"
    card_file.write_text(json.dumps(skill_card, ensure_ascii=False), encoding="utf-8")
    draft = cli("sops", "create", *agent, "--json", str(card_file))
    sop = (*agent, "--sop-id", draft["data"]["sop_id"])
    args = (*sop, "--draft-id", draft["data"]["id"])
    fetched = cli("sops", "get-draft", *args)
    patch = [{"op": "replace", "path": "/description", "value": "使用员工知识库回答"}]
    cli("sops", "patch", *args, "--if-match", fetched["etag"], "--json", "-", body=patch)
    stale = cli("sops", "patch", *args, "--if-match", fetched["etag"], "--json", "-",
                body=patch, expected=1)
    assert stale["status_code"] == 412
    assert cli("sops", "validate", *args)["data"]["valid"]
    assert not cli("sops", "list", *agent)["data"]["data"]
    published = cli("sops", "publish", *args)["data"]
    assert published["draft"]["status"] == "published"
    assert cli("sops", "versions", *sop)["data"]["data"]

    file = tmp_path / "leave.md"
    file.write_text("# 年假\n每年有带薪年假。", encoding="utf-8")
    uploaded = cli("knowledge-bases", "upload-document", *base, "--file-path", str(file))
    jobs.run_job(uploaded["data"]["id"])
    assert cli("jobs", "wait", "--job-id", uploaded["data"]["id"])["data"]["result"]["documents"]
    file.write_bytes(b"")
    failed = cli("knowledge-bases", "upload-document", *base, "--file-path", str(file))["data"]["id"]
    jobs.run_job(failed)
    error = cli("jobs", "wait", "--job-id", failed, expected=4)
    assert error["kind"] == "job_failed" and error["job_id"] == failed
