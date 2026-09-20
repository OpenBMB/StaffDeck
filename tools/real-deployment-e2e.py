"""Real StaffDeck/PilotDeck deployment smoke and concurrency verification.

This runner deliberately uses HTTP and a file-backed SQLite database. It does
not import StaffDeck application internals and never writes a synthetic PASS.
External model/tool traffic is served by the deterministic parity mock.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover - environment probe
    websockets = None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def preferred_or_free(preferred: int) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            return free_port()


def http_json(base: str, method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace")
        return error.code, payload


def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"service did not become ready: {url}")


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def db_counts(path: Path) -> dict[str, int | None]:
    tables = [
        "sessions", "messages", "harness_turns", "harness_runs",
        "harness_task_frames", "harness_agent_loop_records",
        "harness_invocations", "human_handoff_requests", "scheduled_tasks",
        "scheduled_task_runs", "teams", "team_tasks", "team_task_events",
    ]
    result: dict[str, int | None] = {}
    connection = sqlite3.connect(path)
    try:
        for table in tables:
            try:
                result[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                result[table] = None
    finally:
        connection.close()
    return result


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.25):
    """Poll a real HTTP-backed condition without turning timeout into PASS."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def _handoff_resolved(base: str, handoff_id: str, token: str) -> dict[str, Any] | None:
    status, payload = http_json(base, "GET", "/api/chat/handoffs?tenant_id=tenant_demo&status=all", token=token)
    if status != 200 or not isinstance(payload, list):
        return None
    row = next((item for item in payload if isinstance(item, dict) and item.get("id") == handoff_id), None)
    if row and row.get("status") in {"answered", "resolved", "failed"}:
        return row
    return None


def _scheduled_run_terminal(base: str, task_id: str, token: str) -> dict[str, Any] | None:
    status, payload = http_json(base, "GET", f"/api/enterprise/scheduled-tasks/{task_id}/runs?tenant_id=tenant_demo", token=token)
    if status != 200 or not isinstance(payload, list) or not payload:
        return None
    row = payload[0]
    return row if isinstance(row, dict) and row.get("status") in {"succeeded", "completed", "failed", "skipped", "retrying"} else None


def _team_task_terminal(base: str, team_id: str, task_id: str, token: str) -> dict[str, Any] | None:
    status, payload = http_json(base, "GET", f"/api/enterprise/teams/{team_id}/tasks/{task_id}?tenant_id=tenant_demo", token=token)
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload if payload.get("status") in {"completed", "failed", "review", "escalated", "blocked"} else None


def gateway_ws_probe(uri: str, token: str) -> dict[str, Any]:
    if websockets is None:
        return {"status": "BLOCKED", "error": "python websockets package is unavailable"}
    import asyncio

    async def probe() -> dict[str, Any]:
        async with websockets.connect(uri, open_timeout=10, close_timeout=5) as ws:
            await ws.send(json.dumps({"type": "hello", "protocolVersion": "1.1", "clientName": "test", "clientVersion": "e2e", "token": token}))
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            payload = json.loads(raw)
            if payload.get("type") != "hello_ok":
                return {"status": "FAIL", "frame": payload}
            return {"status": "PASS", "frameType": payload.get("type"), "protocolVersion": payload.get("protocolVersion")}

    return asyncio.run(probe())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staffdeck-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pilotdeck-root", type=Path, default=Path("/Users/a1/Desktop/claw/openbmb/PilotDeck-core_agent_loop_0831"))
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()

    root = args.staffdeck_root.resolve()
    pilot = args.pilotdeck_root.resolve()
    report_dir = Path(tempfile.mkdtemp(prefix="staffdeck-e2e-"))
    db_path = report_dir / "staffdeck.db"
    data_dir = report_dir / "data"
    data_dir.mkdir()
    mock_port = 0
    app_port = preferred_or_free(5191)
    gateway_port = preferred_or_free(18790)
    mock_log = report_dir / "mock.jsonl"
    app_log = report_dir / "staffdeck.log"
    gateway_log = report_dir / "pilotdeck.log"
    processes: list[subprocess.Popen[str]] = []
    results: dict[str, Any] = {}

    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ULTRARAG_DATA_DIR": str(data_dir),
        "APP_SECRET": "e2e-test-secret",
        "DEMO_MODEL_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "DEMO_MODEL_NAME": "deterministic",
        "DEMO_MODEL_API_KEY": "e2e-test-key",
        "PILOTDECK_AGENT_LOOP_ENABLED": "true",
        "PILOTDECK_AGENT_LOOP_COMMAND": f"node {pilot / 'dist/src/cli/pilotdeck-agent-loop-sidecar.js'}",
        "PILOTDECK_AGENT_LOOP_CWD": str(pilot),
        "PILOTDECK_AGENT_LOOP_TIMEOUT_SECONDS": "30",
        "APP_PORT": str(app_port),
    })
    pilot_home = report_dir / "pilot-home"
    pilot_home.mkdir()

    try:
        mock = subprocess.Popen(
            [str(root / "backend/.venv/bin/python"), str(root / "tools/agent-loop-parity/mock_backend.py"), "--port", "0", "--record", str(mock_log)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        processes.append(mock)
        ready_line = mock.stdout.readline() if mock.stdout is not None else ""
        ready = json.loads(ready_line)
        mock_port = int(ready["port"])
        env["DEMO_MODEL_BASE_URL"] = f"http://127.0.0.1:{mock_port}/v1"
        wait_http(f"http://127.0.0.1:{mock_port}/health")

        (pilot_home / "pilotdeck.yaml").write_text(
            "schemaVersion: 1\n"
            "agent:\n  model: deterministic/deterministic\n"
            "model:\n  providers:\n    deterministic:\n"
            f"      protocol: openai\n      url: http://127.0.0.1:{mock_port}/v1\n      apiKey: e2e-test-key\n      models:\n        deterministic: {{}}\n",
            encoding="utf-8",
        )
        gateway_env = env | {
            "PILOT_HOME": str(pilot_home),
            "PILOTDECK_GATEWAY_PORT": str(gateway_port),
            "PILOTDECK_GATEWAY_URL": f"ws://127.0.0.1:{gateway_port}/ws",
        }
        gateway = subprocess.Popen(
            ["node", str(pilot / "dist/src/cli/pilotdeck.js"), "server"],
            cwd=pilot, env=gateway_env, stdout=gateway_log.open("w"), stderr=subprocess.STDOUT, text=True,
        )
        processes.append(gateway)
        wait_http(f"http://127.0.0.1:{gateway_port}/auth/local-token", timeout=30)
        token_path = pilot_home / "server-token"
        gateway_token = token_path.read_text(encoding="utf-8").strip()
        results["gateway"] = gateway_ws_probe(f"ws://127.0.0.1:{gateway_port}/ws", gateway_token)
        env["STAFFDECK_PILOTDECK_UPSTREAM"] = f"http://127.0.0.1:{gateway_port}"

        # Use the product's single-port entrypoint directly so this run has no
        # dependency on an already-running .dev supervisor.
        app = subprocess.Popen(
            [str(root / "backend/.venv/bin/python"), "-m", "uvicorn", "single_port_app:app", "--host", "127.0.0.1", "--port", str(app_port)],
            cwd=root / "backend", env=env, stdout=app_log.open("w"), stderr=subprocess.STDOUT, text=True,
        )
        processes.append(app)
        base = f"http://127.0.0.1:{app_port}"
        wait_http(f"{base}/api/health", timeout=60)
        results["deployment"] = {"base": base, "health": http_json(base, "GET", "/api/health")[0], "chatPage": http_json(base, "GET", "/chat/")[0], "enterprisePage": http_json(base, "GET", "/enterprise/dashboard")[0]}

        status, login = http_json(base, "POST", "/api/auth/login", {"tenant_id": "tenant_demo", "username": "admin", "password": "admin"})
        if status != 200:
            raise RuntimeError(f"login failed: {status} {login}")
        token = str(login["token"])
        status, agents = http_json(base, "GET", "/api/chat/agents?tenant_id=tenant_demo", token=token)
        if status != 200 or not agents:
            raise RuntimeError(f"agent list failed: {status} {agents}")
        agent_id = str(agents[0]["id"])
        use_status, _ = http_json(base, "POST", f"/api/chat/agents/{agent_id}/use?tenant_id=tenant_demo", token=token)
        results["auth"] = {"login": status, "agent": agent_id, "use": use_status}

        # Create a published, API-visible handoff SOP.  The SOP is deliberately
        # tiny; the real Harness/TaskFrame path still decides when to create the
        # durable handoff request.
        handoff_skill_id = f"e2e_handoff_{int(time.time())}"
        handoff_card = {
            "skill_id": handoff_skill_id,
            "name": "E2E Handoff",
            "version": "1.0.0",
            "description": "Deterministic E2E handoff fixture",
            "capability_scope": "general",
            "trigger_intents": ["E2E_HANDOFF"],
            "nodes": [{
                "node_id": "handoff",
                "type": "handoff",
                "name": "人工处理",
                "instruction": "将请求转交人工处理",
                "allowed_actions": ["handoff_human"],
                "assignee_user_id": str((login.get("user") or {}).get("id") or ""),
                "assignee_notify_channel": "web",
            }],
            "edges": [],
            "start_node_id": "handoff",
            "terminal_node_ids": ["handoff"],
        }
        skill_status, _skill_payload = http_json(
            base,
            "POST",
            f"/api/enterprise/skills?tenant_id=tenant_demo&agent_id={agent_id}",
            {"tenant_id": "tenant_demo", "content": handoff_card, "status": "published"},
            token,
        )
        results["handoffFixture"] = {"createStatus": skill_status, "skillId": handoff_skill_id}

        # The single-port app exposes the PilotDeck WebSocket proxy only when
        # STAFFDECK_PILOTDECK_UPSTREAM is configured. Record its actual state;
        # an unconfigured proxy is a deployment gap, not a fake success.
        proxy_status, proxy_payload = http_json(base, "GET", "/pilotdeck/health")
        results["staffdeckGatewayProxy"] = {"status": proxy_status, "response": proxy_payload}

        # Multi-turn HTTP session.
        first_status, first = http_json(base, "POST", "/api/chat/turn", {"tenant_id": "tenant_demo", "agent_id": agent_id, "client_turn_id": "e2e-multi-1", "message": "initial deployment query"}, token)
        session_id = first.get("session_id") if isinstance(first, dict) else None
        second_status, second = http_json(base, "POST", "/api/chat/turn", {"tenant_id": "tenant_demo", "session_id": session_id, "client_turn_id": "e2e-multi-2", "message": "follow up using the previous answer"}, token) if session_id else (0, {})
        results["multiTurn"] = {"first": first_status, "second": second_status, "sameSession": bool(session_id and second.get("session_id") == session_id), "sessionId": session_id, "replies": [first.get("reply") if isinstance(first, dict) else None, second.get("reply") if isinstance(second, dict) else None]}

        def concurrent_turn(index: int) -> dict[str, Any]:
            status_code, payload = http_json(base, "POST", "/api/chat/turn", {"tenant_id": "tenant_demo", "agent_id": agent_id, "client_turn_id": f"e2e-concurrent-{index}", "message": f"concurrent query {index}"}, token)
            return {"status": status_code, "sessionId": payload.get("session_id") if isinstance(payload, dict) else None, "reply": payload.get("reply") if isinstance(payload, dict) else None}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_results = list(pool.map(concurrent_turn, [1, 2]))
        results["concurrency"] = {"results": concurrent_results, "isolatedSessions": len({item["sessionId"] for item in concurrent_results}) == 2}

        # Handoff create/reply/resume through the public HTTP API.  A fixture
        # creation failure remains evidence and does not get collapsed into a
        # successful probe.
        handoff_turn_status, handoff_turn = http_json(
            base,
            "POST",
            "/api/chat/turn",
            {
                "tenant_id": "tenant_demo", "agent_id": agent_id,
                "client_turn_id": "e2e-handoff-create", "message": f"/sop {handoff_skill_id} E2E_HANDOFF",
            },
            token,
        ) if skill_status in {200, 201} else (0, {})
        pending_status, pending = http_json(base, "GET", "/api/chat/handoffs?tenant_id=tenant_demo&status=pending", token=token)
        handoff_id = pending[0].get("id") if isinstance(pending, list) and pending else None
        reply_status, reply_payload = (0, {})
        resume_observed = False
        if handoff_id:
            reply_status, reply_payload = http_json(
                base, "POST", f"/api/chat/handoffs/{handoff_id}/reply",
                {"tenant_id": "tenant_demo", "reply": "E2E 人工已确认"}, token,
            )
            resume_observed = bool(wait_for(lambda: _handoff_resolved(base, handoff_id, token), timeout=20))
        results["handoff"] = {
            "fixtureStatus": skill_status, "turnStatus": handoff_turn_status,
            "pendingListStatus": pending_status, "handoffId": handoff_id,
            "replyStatus": reply_status, "resumeObserved": resume_observed,
            "turn": handoff_turn, "reply": reply_payload,
        }

        # Scheduled task: create a pinned SOP task through the enterprise API,
        # invoke its real background worker and poll its durable run record.
        scheduled_skill_id = f"e2e_scheduled_{int(time.time())}"
        scheduled_card = {
            "skill_id": scheduled_skill_id,
            "name": "E2E Scheduled",
            "version": "1.0.0",
            "description": "Deterministic scheduled worker fixture",
            "capability_scope": "general",
            "trigger_intents": ["scheduled deterministic query"],
            "nodes": [{"node_id": "execute", "type": "collect_info", "name": "执行", "instruction": "完成定时任务"}],
            "edges": [], "start_node_id": "execute", "terminal_node_ids": ["execute"],
        }
        scheduled_skill_status, _scheduled_skill_payload = http_json(
            base, "POST", f"/api/enterprise/skills?tenant_id=tenant_demo&agent_id={agent_id}",
            {"tenant_id": "tenant_demo", "content": scheduled_card, "status": "published"}, token,
        )
        scheduled_body = {
            "tenant_id": "tenant_demo", "agent_id": agent_id,
            "title": "E2E scheduled worker", "prompt": "scheduled deterministic query",
            "schedule_type": "once", "schedule": {"run_at": "2099-01-01T00:00:00+08:00"},
            "timezone": "Asia/Shanghai", "status": "active",
            "metadata": {"sop_id": scheduled_skill_id, "sop_version_policy": "pinned"},
        }
        sched_create_status, sched_payload = http_json(base, "POST", "/api/enterprise/scheduled-tasks", scheduled_body, token)
        sched_run_status, sched_run = (0, {})
        sched_final = None
        if sched_create_status in {200, 201} and isinstance(sched_payload, dict):
            task_id = sched_payload.get("id")
            sched_run_status, sched_run = http_json(base, "POST", f"/api/enterprise/scheduled-tasks/{task_id}/run-now?tenant_id=tenant_demo", token=token)
            if sched_run_status in {200, 201}:
                sched_final = wait_for(lambda: _scheduled_run_terminal(base, task_id, token), timeout=60, interval=0.5)
        results["scheduled"] = {
            "skillStatus": scheduled_skill_status, "createStatus": sched_create_status, "runNowStatus": sched_run_status,
            "run": sched_run, "terminalRun": sched_final,
        }

        # Team worker: create a team, add two distinct seeded agents when
        # available, and create an assigned task that wakes the real worker.
        all_agent_ids = [str(item.get("id")) for item in agents if isinstance(item, dict) and item.get("id")]
        team_payload = {"tenant_id": "tenant_demo", "name": f"E2E Team {int(time.time())}"}
        team_create_status, team = http_json(base, "POST", "/api/enterprise/teams", team_payload, token)
        member_results = []
        team_task_status, team_task = (0, {})
        team_final = None
        if team_create_status in {200, 201} and isinstance(team, dict):
            team_id = team.get("id")
            for member_index, member_id in enumerate(all_agent_ids[:2]):
                member_results.append(http_json(
                    base, "POST", f"/api/enterprise/teams/{team_id}/members",
                    {"tenant_id": "tenant_demo", "agent_id": member_id, "role": "leader" if member_index == 0 else "member"}, token,
                ))
            assignee = all_agent_ids[1] if len(all_agent_ids) > 1 else (all_agent_ids[0] if all_agent_ids else None)
            if assignee:
                team_task_status, team_task = http_json(
                    base, "POST", f"/api/enterprise/teams/{team_id}/tasks",
                    {"tenant_id": "tenant_demo", "title": "E2E team worker", "description": "deterministic team task", "assignee_agent_id": assignee}, token,
                )
                task_id = team_task.get("id") if isinstance(team_task, dict) else None
                if task_id:
                    team_final = wait_for(lambda: _team_task_terminal(base, team_id, task_id, token), timeout=60, interval=0.5)
        results["team"] = {
            "createStatus": team_create_status, "teamId": team.get("id") if isinstance(team, dict) else None,
            "memberResults": member_results, "taskStatus": team_task_status,
            "task": team_task, "terminalTask": team_final,
        }

        coverage_gaps = []
        if not handoff_id or reply_status not in {200, 201} or not resume_observed:
            coverage_gaps.append("handoff create/reply/resume")
        if sched_create_status not in {200, 201} or sched_run_status not in {200, 201} or not sched_final:
            coverage_gaps.append("scheduled worker")
        if team_create_status not in {200, 201} or team_task_status not in {200, 201} or not team_final:
            coverage_gaps.append("team worker")
        results["coverageGaps"] = coverage_gaps

        results["database"] = db_counts(db_path)
        results["mockRequests"] = len([line for line in mock_log.read_text().splitlines() if '"direction": "request"' in line]) if mock_log.exists() else 0
        core_pass = all(item.get("status") == 200 for item in concurrent_results) and results["concurrency"]["isolatedSessions"] and results["multiTurn"]["sameSession"]
        results["status"] = "FAIL" if not core_pass else ("WARNING" if coverage_gaps else "PASS")
    except Exception as error:  # noqa: BLE001 - runner must classify any deployment failure
        results["status"] = "BLOCKED"
        results["error"] = str(error)
    finally:
        if not args.keep_runtime:
            for process in reversed(processes):
                terminate(process)

    report = report_dir / "REAL_DEPLOYMENT_E2E.zh.md"
    lines = ["# StaffDeck 完整部署 E2E", "", f"- status: **{results.get('status', 'BLOCKED')}**", f"- artifact directory: `{report_dir}`", f"- database: `{db_path}`", ""]
    for name, payload in results.items():
        if name in {"status", "error"}:
            continue
        lines += [f"## {name}", "", "```json", json.dumps(payload, ensure_ascii=False, indent=2), "```", ""]
    if results.get("error"):
        lines += ["## Error", "", str(results["error"]), ""]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": results.get("status"), "report": str(report), "artifacts": str(report_dir), "results": results}, ensure_ascii=False, indent=2))
    if not args.keep_runtime:
        # Keep report and evidence; only processes are cleaned. The directory
        # is intentionally retained for post-run inspection.
        pass
    return 0 if results.get("status") == "PASS" else (2 if results.get("status") == "WARNING" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
