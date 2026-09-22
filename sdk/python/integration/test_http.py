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
