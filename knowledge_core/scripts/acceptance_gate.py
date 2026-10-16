"""Fail closed on missing, skipped or unsuccessful acceptance evidence."""

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


WORKFLOW = {"create", "upload", "edit", "bucket-edit", "chunk-edit", "wiki-edit", "search", "citation",
            "copy", "publish", "rollback", "sync", "rename", "hide", "restore", "graph", "okf-lint", "okf-export", "okf-import", "mobile"}


def evaluate(root):
    gates = {}

    def read(relative):
        path = root / relative
        if not path.is_file(): raise ValueError(f"Missing evidence: {relative}")
        return json.loads(path.read_text())

    def require(condition, message):
        if not condition: raise ValueError(message)

    def check(name, fn):
        try:
            fn()
            gates[name] = {"status": "passed"}
        except (ValueError, KeyError, TypeError, OSError, ET.ParseError, StopIteration, IndexError) as exc:
            gates[name] = {"status": "not_accepted", "reason": str(exc)}

    def matrix():
        value = read("compatibility/matrix.json")
        require(value.get("baseline", "").startswith("77c21973"), "Wrong primary baseline")
        require(value.get("status") == "passed" and value.get("cases"), "Recursive matrix failed or incomplete")
        require(not value.get("missing_required_cases") and not value.get("gaps") and not value.get("unsuccessful_required_cases") and not value.get("terminal_failures"), "Matrix coverage incomplete")
        require(value.get("http_call_count", 0) > 0, "No independent KM HTTP execution")
        require(all(case.get("status") == "passed" and not case.get("differences") for case in value["cases"]), "Field differences remain")
        for file in ["baseline.json", "current.json", "baseline.log", "current.log", "km-service.log"]:
            require((root / "compatibility" / file).is_file(), "Matrix evidence missing: " + file)

    def browser():
        value = read("full-browser/report.json")
        require(value.get("status") == "passed" and not value.get("error") and not value.get("requiredFailures"), "Full browser run failed")
        require(value.get("startup") == "formal_app_main_with_harness_v3", "Full StaffDeck lifespan not used")
        for surface in ["staffdeck", "pilotdeck"]:
            require(WORKFLOW <= set(value[surface]["mainWorkflow"]["workflow"]), surface + " workflow incomplete")
            require(value[surface].get("requests"), surface + " request evidence missing")
            for viewport in ["desktop", "mobile"]:
                require((root / f"full-browser/{surface}-main-{viewport}.png").is_file(), "Browser screenshot missing")
        require(all(str(state).startswith("real_") for state in value["coverage"].values()) and len(value["coverage"]) == 4, "Cancel/publication coverage incomplete")
        resources = value["staffdeck"]["persistence"]["hostResources"]
        require(resources.get("tools") and resources.get("skills") and resources.get("receipts"), "Actual Skill/Tool evidence missing")
        require(not any(item.get("forced") for item in value["lifecycle"].values()), "Forced process termination")
        staff_log = root / "full-browser/staffService.log"
        require(staff_log.is_file() and "Application shutdown complete" in staff_log.read_text(), "StaffDeck normal shutdown evidence missing")

    def model():
        value = read("hosted-model-final/report.json")
        require(value.get("status") == "passed" and not value.get("error") and value["realModel"]["status"] == "passed", "Real hosted model run failed")
        require(not any(row.get("forced") for row in value["lifecycle"].values()), "Hosted process forced termination")
        selected = value["realModel"]["selectedModel"]
        require(selected == value["modelProbe"]["selected_model_id"], "Selected model mismatch")
        requests = value["sd"]["requests"]
        for suffix in ["/documents", "/query"]:
            require(any(row.get("path", "").endswith(suffix) and row.get("body", {}).get("model_id") == selected for row in requests if isinstance(row.get("body"), dict)), "Selected model not propagated to " + suffix)
        calls = value["realModel"]["modelCalls"]
        require(len(calls) >= 4, "Inference result evidence missing")
        host_log = (root / "hosted-model-final/knowledgeService.log").read_text()
        core_log = (root / "hosted-model-final/kmOwnerService.log").read_text()
        for call in calls:
            identifier = call.split("call_id=", 1)[1].split()[0]
            require(call in host_log and identifier in core_log, "Uncorrelated inference result")
        reply = next(row for row in reversed(requests) if row.get("path", "").endswith("/query") and row.get("responseStatus") == 200)
        result = json.loads(reply["responseBody"])
        phases = [row["phase"] for row in result.get("route_trace", [])]
        require("document_route" in phases and result.get("evidence_pack"), "No valid model retrieval")
        require(not any(any(word in phase for word in ["fallback", "failed", "invalid", "document_route_lexical"]) for phase in phases), "Model retrieval used fallback")
        for file in ["knowledgeService.log", "kmOwnerService.log", "model-probe.json", "staffdeck-km-desktop.png", "staffdeck-km-mobile.png"]:
            require((root / "hosted-model-final" / file).is_file(), "Hosted evidence missing: " + file)

    def regression():
        value = read("regression/report.json")
        require(value["status"] == "passed", "Regression command failed")
        for group in ["core", "compatibility"]:
            require(value["groups"][group]["status"] == "passed", group + " regression incomplete")
            tree = ET.parse(root / f"regression/{group}.xml")
            suites = list(tree.getroot().iter("testsuite"))
            require(sum(int(s.get("tests", 0)) for s in suites) > 0, "No tests executed")
            require(not any(int(s.get(field, 0)) for s in suites for field in ["skipped", "failures", "errors"]), "Skipped/failed regression")
        artifact = read("independent-final/report.json")
        require(artifact["status"] == "passed" and not artifact["isolation"]["host_imports"], "Latest artifact isolation failed")
        require(read("independent-final/runtime-imports.json") == [], "Actual service loaded host modules")
        require(len(artifact["isolation"]["tables"]) == 14 and all(table.startswith("knowledge_") for table in artifact["isolation"]["tables"]), "Non-KM storage present")
        require(WORKFLOW <= set(artifact["browser"]["workflow"]), "Artifact workflow incomplete")
        for viewport in ["desktop", "mobile"]:
            require((root / f"independent-final/independent-{viewport}.png").is_file(), "Artifact screenshot missing")
        require("Application shutdown complete" in (root / "independent-final/service.log").read_text(), "Artifact normal shutdown missing")

    check("full_lifespan_browser", browser)
    check("recursive_compatibility", matrix)
    check("real_hosted_model", model)
    check("core_and_artifact", regression)
    return {"status": "complete" if all(row["status"] == "passed" for row in gates.values()) else "not_accepted", "gates": gates}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence_dir.resolve()
    report = evaluate(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "acceptance.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
