"""Run only against copied development data and an isolated host configuration."""

import argparse
import json
import os
import sqlite3
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from knowledge_core.migration import compare, migrate, preflight


@contextmanager
def service(database, port, frozen):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} is already occupied; stop the rehearsal service first")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    command = [sys.executable, "-m", "knowledge_core.cli", "--database-url",
               f"sqlite:///{database}", "--port", str(port)]
    if frozen:
        command.append("--read-only")
    process = subprocess.Popen(command, cwd=database.parent, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode())
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("KM startup timed out")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def evidence(client, space, query):
    response = client.post("/query", json={"space_id": space, "query": query, "use_model": False})
    response.raise_for_status()
    chunks = response.json()["chunks"]
    assert chunks, "Expected retrieval evidence"
    result = []
    for chunk in chunks:
        citation = client.get(f"/citations/{chunk['id']}", params={"space_id": space})
        citation.raise_for_status()
        result.append({"id": chunk["id"], "content": chunk["content"], "citation": citation.json()})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18787)
    parser.add_argument("--query", default="Expense receipts")
    parser.add_argument("--sd-url")
    parser.add_argument("--sd-session", type=Path)
    parser.add_argument("--sd-space", default="sd:local:overall")
    parser.add_argument("--pd-url")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    assert source != target and not target.exists(), "Use a new development target database"
    if args.sd_url and not args.sd_session:
        parser.error("--sd-session is required with --sd-url")
    assert preflight(source)["ok"], "Finish/cancel tasks and fix associations before freezing"
    copy = target.with_name(target.stem + "-source-copy.sqlite")
    hosts = []
    if args.sd_url:
        session = json.loads(args.sd_session.read_text())
        hosts.append(("sd", httpx.Client(base_url=args.sd_url + "/api/km/v1",
                     headers={"Authorization": "Bearer " + session["token"]}), args.sd_space))
    if args.pd_url:
        hosts.append(("pd", httpx.Client(base_url=args.pd_url + "/api/modules/knowledge/core"), "local"))
    core = httpx.Client(base_url=f"http://127.0.0.1:{args.port}/api/km/v1")
    try:
        with service(source, args.port, True):
            denied = core.post("/knowledge-bases", json={"name": "Frozen write"})
            assert denied.status_code == 503
            before = {name: evidence(client, space, args.query) for name, client, space in hosts}
            before["core"] = evidence(core, "local", args.query)
            with sqlite3.connect(source) as origin, sqlite3.connect(copy) as destination:
                origin.backup(destination)
            initial = migrate(copy, f"sqlite:///{target}")
            repeat = migrate(copy, f"sqlite:///{target}")
            comparison = compare(copy, f"sqlite:///{target}")
            assert initial["ok"] and repeat["ok"] and comparison["ok"]
        with service(target, args.port, True):
            after = {name: evidence(client, space, args.query) for name, client, space in hosts}
            after["core"] = evidence(core, "local", args.query)
            assert after == before, "Retrieval or citation changed after migration"
        with service(target, args.port, False):
            created = core.post("/knowledge-bases", json={"name": "Cutover write " + str(time.time_ns())})
            created.raise_for_status()
        args.report.write_text(json.dumps({"ok": True, "source": str(source), "target": str(target),
            "frozen_write_status": denied.status_code, "migration": initial, "repeat": repeat,
            "comparison": comparison, "hosts": list(before), "query_citation_equal": True,
            "writes_restored": True, "sample_base_id": created.json()["id"]}, indent=2))
        print(f"Cutover rehearsal passed: {args.report}")
    finally:
        core.close()
        for _, client, _ in hosts:
            client.close()


if __name__ == "__main__":
    main()
