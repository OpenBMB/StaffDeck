"""Fail closed on missing evidence for the KM plugin convergence phase."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from acceptance_gate import WORKFLOW


def evaluate(root):
    gates = {}
    def read(path):
        return json.loads((root / path).read_text())
    def require(value, message):
        if not value:
            raise ValueError(message)
    def check(name, action):
        try:
            action()
            gates[name] = {"status": "passed"}
        except (OSError, ValueError, KeyError, TypeError, StopIteration, ET.ParseError) as error:
            gates[name] = {"status": "not_accepted", "reason": str(error)}
    def regression():
        require(read("regression/report.json")["status"] == "passed", "Regression failed")
        for group in ("core", "compatibility"):
            suites = list(ET.parse(root / f"regression/{group}.xml").getroot().iter("testsuite"))
            require(sum(int(row.get("tests", 0)) for row in suites) > 0, "No tests")
            require(not any(int(row.get(field, 0)) for row in suites for field in ("failures", "errors", "skipped")), "Failed or skipped regression")
        runtime = ET.parse(root / "pd-runtime.xml").getroot()
        require(len(list(runtime.iter("testcase"))) >= 60 and not list(runtime.iter("failure")) and not list(runtime.iter("error")) and not list(runtime.iter("skipped")), "PD runtime regression failed or skipped")
        for path, minimum in (("pd-tests.json", 12), ("sd-bridge-tests.json", 2)):
            tests = read(path)
            require(tests["success"] and tests["numPassedTests"] >= minimum and not tests["numPendingTests"] and not tests["numFailedTests"], "Host bridge/route regression incomplete")
    def compatibility():
        value = read("compatibility/matrix.json")
        require(value["status"] == "passed" and value["baseline"].startswith("77c21973"), "Compatibility failed")
        require(len(value["cases"]) >= 189 and value["http_call_count"] > 0, "Compatibility incomplete")
        require(all(row["status"] == "passed" and not row["differences"] for row in value["cases"]), "Field differences")
        require(not any(value.get(field) for field in ("gaps", "missing_required_cases", "terminal_failures", "unsuccessful_required_cases")), "Missing cases")
    def browser():
        value = read("full-browser/report.json")
        require(value["status"] == "passed" and value["startup"] == "formal_app_main_with_harness_v3", "Full lifespan browser failed")
        for host in ("staffdeck", "pilotdeck"):
            require(WORKFLOW <= set(value[host]["mainWorkflow"]["workflow"]), "Incomplete browser flow")
            require(value[host]["requests"], "Missing requests")
            for view in ("desktop", "mobile"):
                require((root / f"full-browser/{host}-main-{view}.png").is_file(), "Missing screenshot")
        require(len(value["coverage"]) == 4 and all(row.startswith("real_") for row in value["coverage"].values()), "Cancel/discovery missing")
        require(all(value["staffdeck"]["persistence"]["hostResources"].get(field) for field in ("tools", "skills", "receipts")), "Host publication missing")
        management = {row["operation"]: row["result"] for row in value["pilotdeck"]["runtimeManagement"]}
        require(management["confirm_discovery"]["status"] == management["get_discovery"]["status"] == "reviewed" and management["export_discovery"], "Runtime suggestion review/export missing")
        require(not any(row["forced"] for row in value["lifecycle"].values()), "Forced shutdown")
        require("Application shutdown complete" in (root / "full-browser/staffService.log").read_text(), "Shutdown missing")
    def model():
        value = read("pd-model/report.json")
        require(value["status"] == "passed", "PD real model failed")
        require(not any(row["forced"] for row in value["lifecycle"]), "Forced PD model shutdown")
        require(value["isolation"].get("staffdeck_source") is False and value["isolation"].get("staffdeck_service") is False and value["isolation"].get("pilotdeck_from_build"), "PD artifact isolation missing")
        require(WORKFLOW <= set(value["browser"]["workflow"]), "PD model browser flow incomplete")
        for name in ("pilotdeck-desktop.png", "pilotdeck-mobile.png", "km.log", "pilotdeck.log", "gateway.log"):
            require((root / "pd-model" / name).is_file(), "PD model evidence missing")
        require(value.get("correlatedModelCalls") and len(value["correlatedModelCalls"]) == len(value["modelCalls"]), "Model correlation missing")
        require(all(row["config_id"] == value["selectedModel"]["id"] and row["provider"] and row["model"] and row["result_keys"] for row in value["correlatedModelCalls"]), "Provider result missing")
        finished = next(row for row in value["agentEvents"] if row["type"] == "tool_call_finished" and row.get("toolName") == "knowledge_query")
        require(finished["ok"] and finished["data"]["evidence_pack"], "Real Agent failed")
        require(not any(any(word in row["phase"] for word in ("fallback", "failed", "invalid", "lexical")) for row in finished["data"]["route_trace"]), "Agent fallback")
        require(any(row["operation"] == "get_job" and row["result"]["status"] == "succeeded" for row in value["runtime"]), "Ingest terminal missing")
        require(len({row["operation"] for row in value["runtime"]}) >= 30, "Runtime workflow incomplete")
    def upgrade():
        value = read("upgrade/report.json")
        result = value["upgrade"]
        require(value["status"] == result["status"] == "passed", "Upgrade failed")
        require(result["before"] == result["after"], "Host artifacts changed")
        for host in ("sd", "pd"):
            require(result["initialVersions"][host]["version"] == "upgrade-before" and result["afterVersions"][host]["version"] == "upgrade-after", "UI version missing")
            require(WORKFLOW <= set(result[host]["workflow"]), "Upgraded workflow incomplete")
            for view in ("desktop", "mobile"):
                require((root / f"upgrade/{host}-upgraded-{view}.png").is_file(), "Upgrade screenshot missing")
    def artifact():
        value = read("artifact/report.json")
        require(value["status"] == "passed" and not value["isolation"]["host_imports"], "Independent artifact failed")
        require(not read("artifact/runtime-imports.json"), "Runtime host imports")
        require(WORKFLOW <= set(value["browser"]["workflow"]), "Independent artifact browser incomplete")
    def cleanup():
        value = read("cleanup.json")
        require(value["status"] == "passed" and not value["violations"], "Cleanup failed")
    for name, action in (("regression", regression), ("compatibility", compatibility), ("full_browser", browser), ("pd_model", model), ("upgrade", upgrade), ("artifact", artifact), ("cleanup", cleanup)):
        check(name, action)
    return {"status": "complete" if all(row["status"] == "passed" for row in gates.values()) else "not_accepted", "gates": gates}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.evidence_dir)
    (args.evidence_dir / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "complete" else 1)
