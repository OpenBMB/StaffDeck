"""Exercise the complete host HTTP process with a real external model."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

root = Path(os.environ["SOP_ACCEPTANCE_ROOT"])
credentials = json.loads((root / "credentials.json").read_text())
origin = os.environ.get("SOP_ACCEPTANCE_HOST", "http://127.0.0.1:26390")
tenant, agent = credentials["sop-actor"]["tenantId"], credentials["agentId"]
card = json.loads((root / "release-definition.json").read_text())
results = []


def call(method, path, body=None, *, public=False, reviewer=False, extra=None):
    token = (
        credentials["management"]["key"]
        if public
        else credentials["sop-reviewer" if reviewer else "sop-actor"]["token"]
    )
    response = httpx.request(
        method,
        origin + path,
        json=body,
        timeout=300,
        headers={"Authorization": f"Bearer {token}", **(extra or {})},
    )
    if not response.is_success:
        raise AssertionError(
            f"{method} {path}: {response.status_code} {response.text[:1500]}"
        )
    return response.json()


def record(name, action):
    try:
        evidence = action()
        results.append({"case": name, "status": "PASS", "evidence": evidence})
    except Exception as error:  # noqa: BLE001 - persist acceptance failures before exiting
        results.append({"case": name, "status": "FAIL", "error": str(error)})
    filename = (
        "staffdeck-capabilities.json"
        if os.environ.get("SOP_ACCEPTANCE_CAPABILITIES") == "true"
        else "staffdeck-http.json"
    )
    (root / filename).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(name, results[-1]["status"], flush=True)


def management():
    with sqlite3.connect(root.parent / "staffdeck.sqlite") as db:
        before = {
            table: db.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ["skills", "skill_versions", "api_sop_drafts"]
        }
    child = {
        **card,
        "skill_id": "acceptance_child",
        "name": "Acceptance child",
        "nodes": [
            {
                "node_id": "done",
                "type": "respond",
                "name": "Done",
                "instruction": "Reply CHILD_COMPLETE",
            }
        ],
        "start_node_id": "done",
        "terminal_node_ids": ["done"],
        "edges": [],
        "required_info": [],
    }
    path = f"/api/enterprise/skills?agent_id={agent}"
    child["skill_id"] += f"_{time.time_ns()}"
    created = call("POST", path, {"tenant_id": tenant, "content": child})
    changed = call(
        "PUT",
        f"/api/enterprise/skills/{child['skill_id']}?agent_id={agent}",
        {"tenant_id": tenant, "content": {**child, "description": "Edited over HTTP"}},
    )
    published = call(
        "POST",
        f"/api/enterprise/skills/{child['skill_id']}/publish?tenant_id={tenant}&agent_id={agent}",
    )
    parent = {
        **child,
        "skill_id": f"acceptance_parent_{time.time_ns()}",
        "name": "Acceptance parent",
        "nodes": [
            {
                "node_id": "child",
                "type": "subflow",
                "name": "Child",
                "sub_sop_id": child["skill_id"],
            },
            child["nodes"][0],
        ],
        "start_node_id": "child",
        "edges": [{"source_node_id": "child", "next_node_id": "done"}],
    }
    draft = call(
        "POST", f"/api/v1/agents/{agent}/sops", {"content": parent}, public=True
    )
    call(
        "PATCH",
        f"/api/v1/agents/{agent}/sops/{parent['skill_id']}?draft_id={draft['id']}",
        [{"op": "replace", "path": "/description", "value": "Public edited parent"}],
        public=True,
        extra={
            "If-Match": draft["etag"],
            "Content-Type": "application/json-patch+json",
        },
    )
    parent_publication = call(
        "POST",
        f"/api/v1/sops/{parent['skill_id']}:publish?agent_id={agent}",
        {"draft_id": draft["id"]},
        public=True,
    )
    versions = call(
        "GET",
        f"/api/v1/sops/{parent['skill_id']}/versions?agent_id={agent}",
        public=True,
    )
    assert versions["data"]
    with sqlite3.connect(root.parent / "staffdeck.sqlite") as db:
        legacy = {
            table: db.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ["skills", "skill_versions", "api_sop_drafts"]
        }
        assert legacy == before, {"before": before, "after": legacy}
    return {
        "internalId": created["skill_id"],
        "editedDescription": changed["content"]["description"],
        "version": published["version"],
        "parent": parent_publication["sop"]["skill_id"],
        "legacyCounts": legacy,
    }


def business():
    sop_id = "release_acceptance"
    if os.environ.get("SOP_ACCEPTANCE_CAPABILITIES") == "true":
        sop_id = f"capability_acceptance_{time.time_ns()}"
        capability_node = {
            "node_id": "verify",
            "name": "Release verification",
            "type": "action",
            "instruction": "Call general_skill.approval-guide, release_ticket_lookup, and knowledge_search for the owner approval policy. Quote RELEASE_OWNER_1014 from the retrieved evidence. Only finish after all three calls succeed.",
            "capability_refs": {
                "general_skill_ids": ["acceptance-guide"],
                "required_general_skill_ids": ["acceptance-guide"],
                "tool_ids": ["acceptance-tool"],
                "required_tool_ids": ["acceptance-tool"],
                "knowledge_base_ids": ["acceptance-policy"],
                "required_knowledge_base_ids": ["acceptance-policy"],
            },
        }
        definition = {
            **card,
            "skill_id": sop_id,
            "nodes": [card["nodes"][0], capability_node, *card["nodes"][1:]],
            "edges": [
                {"source_node_id": "collect", "next_node_id": "verify"},
                {"source_node_id": "verify", "next_node_id": "approval"},
                card["edges"][1],
            ],
        }
        call(
            "POST",
            f"/api/enterprise/skills?agent_id={agent}",
            {"tenant_id": tenant, "content": definition},
        )
        call(
            "POST",
            f"/api/enterprise/skills/{sop_id}/publish?tenant_id={tenant}&agent_id={agent}",
        )
    session = call(
        "POST",
        "/api/chat/sessions",
        {"tenant_id": tenant, "agent_id": agent, "title": "SOP v3 real acceptance"},
    )
    session_id = session["id"]

    def turn(message):
        result = call(
            "POST",
            "/api/chat/turn",
            {
                "tenant_id": tenant,
                "agent_id": agent,
                "session_id": session_id,
                "model_config_id": credentials["modelConfigId"],
                "message": message,
            },
        )
        (root / f"sd-turn-{len(turns)}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False)
        )
        turns.append(result)
        return result

    turns = []
    turn(
        f"/sop {sop_id} Start the release acceptance procedure. Ask me for the ticket."
    )
    turn("The release ticket is SOP-1014. Continue and request operator approval.")
    handoffs = call(
        "GET", f"/api/chat/handoffs?tenant_id={tenant}&status=all", reviewer=True
    )
    wait = next(
        row
        for row in handoffs
        if row["session_id"] == session_id and row["status"] == "pending"
    )
    reply = call(
        "POST",
        f"/api/chat/handoffs/{wait['id']}/reply",
        {
            "tenant_id": tenant,
            "reply": "Approved SOP-1014. Complete the release and return RELEASE_ACCEPTANCE_COMPLETE.",
        },
        reviewer=True,
    )
    runtime = os.environ.get("SOP_RUNTIME_ENDPOINT", "http://127.0.0.1:26391")
    with sqlite3.connect(root.parent / "runtime.sqlite") as db:
        ids = [
            row[0]
            for row in db.execute("select session_id from sop_sessions")
            if session_id in row[0]
        ]
    assert ids, "Host did not create Runtime-owned state"
    for _ in range(180):
        states = [
            httpx.get(
                runtime
                + "/v1/sessions/"
                + __import__("urllib.parse", fromlist=["quote"]).quote(id, safe="")
            ).json()["state"]
            for id in ids
        ]
        if all(state["status"] == "completed" for state in states):
            return {
                "sessionId": session_id,
                "runtimeStates": states,
                "handoffId": wait["id"],
                "replyStatus": reply["status"],
            }
        time.sleep(1)
    raise AssertionError(f"Human continuation did not complete: {states}")


if os.environ.get("SOP_ACCEPTANCE_CAPABILITIES") != "true":
    record("management-internal-public-nesting", management)
record("real-model-business-handoff", business)
raise SystemExit(1 if any(result["status"] != "PASS" for result in results) else 0)
