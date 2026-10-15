"""Exercise cancellation and hard process interruption using an installed wheel."""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


CHILD = """
import sys,time
from pathlib import Path
import uvicorn
from knowledge_core.api import create_app
from knowledge_core.runtime import Runtime
class SlowParser:
    def parse(self, filename, content):
        Path(sys.argv[3]).write_text(filename)
        while not Path(sys.argv[4]).exists():
            time.sleep(.05)
        Path(sys.argv[3] + '.parsed').write_text(filename)
        return content.decode(), 'md'
uvicorn.run(create_app(Runtime(sys.argv[1], parser=SlowParser())), host='127.0.0.1', port=int(sys.argv[2]))
"""


def wait_for(fn):
    for _ in range(200):
        try:
            result = fn()
            if result:
                return result
        except httpx.HTTPError:
            pass
        time.sleep(.05)
    raise RuntimeError("Task acceptance timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18795)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    directory = args.directory.resolve()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Task acceptance port is occupied")
    started, release = directory / "started", directory / "release"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    command = [sys.executable, "-c", CHILD, f"sqlite:///{directory / 'tasks.sqlite'}",
               str(args.port), str(started), str(release)]
    process = None
    def start():
        child = subprocess.Popen(command, cwd=directory, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{args.port}/health/ready").status_code == 200)
        return child
    def stop(child, hard=False):
        child.kill() if hard else child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    try:
        process = start()
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}/api/km/v1") as client:
            def upload(name):
                response = client.post("/documents", json={
                    "filename": name, "content_base64": base64.b64encode(b"# Expense\n\nKeep expense receipts.").decode()})
                response.raise_for_status()
                return response.json()
            cancelled = upload("cancel.md")
            wait_for(started.exists)
            response = client.post(f"/jobs/{cancelled['id']}/cancel")
            response.raise_for_status()
            release.touch()
            def terminal(identifier):
                job = client.get(f"/jobs/{identifier}").json()
                return job if job["status"] in {"cancelled", "failed", "succeeded"} else None
            result = wait_for(lambda: terminal(cancelled["id"]))
            assert result["status"] == "cancelled" and result["finished_at"]
            assert "content_base64" not in result
            assert client.get("/documents").json() == []
            wait_for(lambda: started.with_name(started.name + '.parsed').exists())
            started.unlink()
            release.unlink()
            interrupted = upload("interrupt.md")
            wait_for(started.exists)
            stop(process, hard=True)
            process = None
            process = start()
            result = wait_for(lambda: terminal(interrupted["id"]))
            assert result["status"] == "failed" and result["error"] == "PROCESS_RESTART_INTERRUPTED"
            assert result["finished_at"] and "content_base64" not in result
            assert client.get("/documents").json() == []
        report = {"ok": True, "installed_python": sys.executable,
            "running_cancel": cancelled["id"], "hard_restart_interrupted": interrupted["id"],
            "partial_documents": 0, "terminal_payload_cleared": True}
        (directory / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        if process:
            release.touch()
            stop(process)


if __name__ == "__main__":
    main()
