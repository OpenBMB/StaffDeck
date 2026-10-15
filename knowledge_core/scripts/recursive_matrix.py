"""Historical/current replay orchestrator. Any missing required case fails acceptance."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from recursive_contract import compare_reports
from compatibility_matrix import LEGACY_SDK_WIRE

BASELINE = "77c219737a9e8e24d6df39ba73212c60f5851b90"
SCRIPT = Path(__file__).with_name("contract_replay.py")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if subprocess.check_output(["git", "-C", str(args.baseline), "rev-parse", "HEAD"], text=True).strip() != BASELINE:
        parser.error("Primary baseline must be exactly 77c21973")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONPATH": str(args.current.resolve() / "knowledge_core/src"), "ULTRARAG_DOTENV": "/nonexistent-km-acceptance.env"}
    with tempfile.TemporaryDirectory(prefix="km-matrix-") as directory:
        identity = Path(directory) / "identity.json"
        identity.write_text(json.dumps({"contract-owner": {"id": "host", "tenant_id": "tenant_api", "spaces": ["*"], "permissions": ["read", "write", "history:write"]}}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with (output / "km-service.log").open("w") as log:
            owner = subprocess.Popen([sys.executable, str(SCRIPT.with_name("matrix_owner.py")), "--database-url", f"sqlite:///{directory}/km.sqlite", "--port", str(port)], cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if owner.poll() is not None: raise RuntimeError("Independent KM exited before readiness")
                    try:
                        if httpx.get(url + "/health/ready").status_code == 200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                else: raise RuntimeError("Independent KM did not become ready")
                for name, source, extra in [("baseline", args.baseline, []), ("current", args.current, ["--km-url", url])]:
                    command = [sys.executable, str(SCRIPT), "--source", str(source.resolve()), "--output", str(output / f"{name}.json"), *extra]
                    with (output / f"{name}.log").open("w") as run_log:
                        try:
                            result = subprocess.run(command, env=env, stdout=run_log, stderr=subprocess.STDOUT, timeout=180)
                        except subprocess.TimeoutExpired as exc:
                            (output / "matrix.json").write_text(json.dumps({
                                "schema": "km-recursive-compatibility/v1", "baseline": BASELINE,
                                "status": "not_accepted", "gaps": [f"{name}_replay_timeout"],
                            }, indent=2) + "\n")
                            raise RuntimeError(f"{name} replay exceeded 180 seconds") from exc
                    if result.returncode: raise RuntimeError(f"{name} replay exited {result.returncode}")
            finally:
                owner.terminate()
                try: owner.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    owner.kill()
                    owner.wait()
    baseline = json.loads((output / "baseline.json").read_text())
    current = json.loads((output / "current.json").read_text())
    cases = compare_reports(baseline, current)
    from app.api.module_knowledge import METHODS
    observed = {row["case"] for row in cases}
    # Operation-qualified error cases are retained in reports, with explicit
    # coverage aliases only when their expected condition was actually run.
    coverage = set(observed)
    for row in cases:
        for condition in ["unauthenticated", "permission_denied", "cross_space", "missing", "invalid", "conflict", "stale_version", "parse_failure", "capability_failure", "duplicate_publish"]:
            if row["case"].endswith("." + condition):
                coverage.add(row["case"].split(".")[0] + "." + condition)
    required = {f"module.{name}.success" for name in METHODS}
    required.update(f"{surface}.{name}" for surface in ["public", "sdk"] for name in LEGACY_SDK_WIRE["methods"])
    required.update("rest." + name for name in ["create", "list", "get", "update", "versions", "upload", "job_terminal", "documents", "get_document", "update_document", "buckets", "update_bucket", "chunks", "update_chunk", "query", "concepts", "get_concept", "update_concept", "lint", "export", "import_okf", "cancel", "list_jobs", "rollback", "publish", "sync", "archive", "confirm_discovery"])
    required.update(f"{surface}.{name}" for surface in ["public", "sdk"] for name in ["job_terminal", "upload_job_terminal", "parse_failure_terminal", "capability_failure_terminal"])
    required.update(f"{surface}.{name}" for surface in ["rest", "public", "sdk", "module"] for name in ["unauthenticated", "permission_denied", "cross_space", "missing", "invalid", "conflict", "stale_version", "parse_failure", "capability_failure", "duplicate_publish"])
    # Publication is absent from the historical public REST and Python SDK.
    # Those surfaces instead exercise their supported ingestion idempotency.
    for surface in ["public", "sdk"]:
        required.remove(surface + ".duplicate_publish")
        required.add(surface + ".duplicate_ingest")
    missing = sorted(required - coverage)
    unsuccessful = []
    for row in cases:
        if row["case"] not in required or row["case"].endswith(tuple("." + name for name in ["unauthenticated", "permission_denied", "cross_space", "missing", "invalid", "conflict", "stale_version", "parse_failure", "capability_failure", "duplicate_publish"])):
            continue
        for side in ["expected", "actual"]:
            response = row.get(side) or {}
            body = response.get("body")
            if not 200 <= response.get("status", 0) < 300 or isinstance(body, dict) and body.get("ok") is False:
                unsuccessful.append({"case": row["case"], "side": side, "status": response.get("status")})
    gaps = ["required_cases_missing"] if missing else []
    if unsuccessful: gaps.append("required_success_cases_not_successful")
    if not current["http_calls"]: gaps.append("no_independent_km_http_calls")
    terminal_expectations = {"rest.job_terminal": "succeeded", "rest.cancel": "cancelled", "rest.capability_failure": "failed", "module.get_job.success": "succeeded", "module.cancel_job.success": "cancelled", "module.get_job.capability_failure": "failed"}
    terminal_expectations.update({f"{surface}.{name}": status for surface in ["public", "sdk"] for name, status in [("job_terminal", "succeeded"), ("upload_job_terminal", "succeeded"), ("parse_failure_terminal", "failed"), ("capability_failure_terminal", "failed")]})
    terminal_failures = []
    indexed = {row["case"]: row for row in cases}
    for name, status in terminal_expectations.items():
        for side in ["expected", "actual"]:
            response = indexed.get(name, {}).get(side) or {}
            body = response.get("body", {})
            if name.startswith("module."):
                body = body.get("payload", {}).get("result", {}) if isinstance(body, dict) else {}
            if response.get("status") != 200 or not isinstance(body, dict) or body.get("status") != status:
                terminal_failures.append({"case": name, "side": side, "required_status": status})
    if terminal_failures: gaps.append("task_terminal_evidence_invalid")
    failed = [row["case"] for row in cases if row["status"] != "passed"]
    report = {"schema": "km-recursive-compatibility/v1", "baseline": BASELINE,
              "status": "passed" if not failed and not gaps else "not_accepted", "cases": cases,
              "missing_required_cases": missing, "gaps": gaps,
              "unsuccessful_required_cases": unsuccessful,
              "terminal_failures": terminal_failures,
              "http_call_count": len(current["http_calls"]), "failed_count": len(failed)}
    report["not_applicable"] = {surface + ".duplicate_publish": "77c21973 public API and SDK have no discovery publication operation" for surface in ["public", "sdk"]}
    (output / "matrix.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
