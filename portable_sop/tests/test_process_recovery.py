"""Exercise durable recovery through a real HTTP server process boundary."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen


@contextmanager
def runtime_process(directory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "SOP_RUNTIME_DB": str(directory / "runtime.sqlite"),
    }
    env.pop("SOP_DEFINITIONS_DIR", None)
    env.pop("SOP_PUBLISH_TOKEN", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "staffdeck_sop_runtime.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=directory,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(path, payload=None):
        req = Request(
            endpoint + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    try:
        for _attempt in range(100):
            if process.poll() is not None:
                raise AssertionError("Independent Runtime exited before HTTP readiness")
            try:
                assert request("/healthz")["contract"] == "sop.lifecycle/v3"
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Independent Runtime did not become ready")
        yield request
    finally:
        process.terminate()
        process.wait(timeout=10)


def envelope(request_id, revision=None, **payload):
    return {
        "protocolVersion": "3.0",
        "requestId": request_id,
        "operationId": request_id,
        "turnId": "turn",
        **({"expectedRevision": revision} if revision is not None else {}),
        **payload,
    }


def test_process_restart_recovers_wait_replays_and_completion(tmp_path):
    definition = {
        "sop_id": "flow",
        "version": "1",
        "content": {
            "start_node_id": "collect",
            "nodes": [
                {"node_id": "collect", "expected_user_info": ["name"]},
                {"node_id": "done"},
            ],
            "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
            "terminal_node_ids": ["done"],
        },
    }
    wait_payload = envelope(
        "wait", 0, proposal={"status": "awaiting_user", "reply": "Name?"}
    )
    with runtime_process(tmp_path) as request:
        request("/v1/definitions", definition)
        request("/v1/sessions/s/prepare", envelope("prepare", sopId="flow"))
        waited = request("/v1/sessions/s/submit", wait_payload)
    resume_payload = envelope(
        "resume",
        1,
        waitId=waited["payload"]["state"]["pending_wait"]["wait_id"],
        slotUpdates={"name": "Ada"},
    )
    with runtime_process(tmp_path) as request:
        assert request("/v1/sessions/s")["state"]["revision"] == 1
        assert request("/v1/sessions/s/submit", wait_payload) == waited
        resumed = request("/v1/sessions/s/resume", resume_payload)
        assert resumed["payload"]["state"]["revision"] == 2
    with runtime_process(tmp_path) as request:
        assert request("/v1/sessions/s/resume", resume_payload) == resumed
        request(
            "/v1/sessions/s/submit",
            envelope(
                "advance", 2, proposal={"status": "completed", "reply": "Collected"}
            ),
        )
        final_payload = envelope(
            "done", 3, proposal={"status": "completed", "reply": "Done"}
        )
        final = request("/v1/sessions/s/submit", final_payload)
    with runtime_process(tmp_path) as request:
        assert request("/v1/sessions/s/submit", final_payload) == final
        assert request("/v1/sessions/s")["state"]["status"] == "completed"
        assert request("/v1/sessions/s")["state"]["revision"] == 4
