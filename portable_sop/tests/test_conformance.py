from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from staffdeck_sop_core import SopDefinition, SopCoreError
from staffdeck_sop_core.nesting import expand
from staffdeck_sop_runtime.api import create_app


FLOW = {"sop_id": "flow", "version": "1", "content": {
    "start_node_id": "collect", "nodes": [
        {"node_id": "collect", "expected_user_info": ["name"], "allowed_actions": ["call_tool:lookup"]},
        {"node_id": "approval", "type": "handoff", "assignee_user_id": "reviewer"},
        {"node_id": "done"}],
    "edges": [{"source_node_id": "collect", "next_node_id": "approval"},
              {"source_node_id": "approval", "next_node_id": "done"}], "terminal_node_ids": ["done"]}}


def envelope(request, revision=None, **payload):
    return {"protocolVersion": "3.0", "requestId": request, "operationId": request,
            "turnId": "turn", **({"expectedRevision": revision} if revision is not None else {}), **payload}


def setup(client, definition=FLOW):
    assert client.post("/v1/definitions", json=definition).status_code == 200
    assert client.post("/v1/sessions/s/prepare", json=envelope("prepare", sopId=definition["sop_id"])).status_code == 200


def receipt(**overrides):
    return {"sessionId": "s", "turnId": "turn", "nodeId": "collect", "capabilityId": "lookup",
            "operationId": "lookup-1", "status": "succeeded", **overrides}


def test_wait_resume_next_complete_with_atomic_replays(tmp_path):
    with TestClient(create_app(str(tmp_path / "state.db"))) as client:
        setup(client)
        waiting = client.post("/v1/sessions/s/submit", json=envelope("wait", 0,
            proposal={"status": "awaiting_user", "reply": "Name?"})).json()["payload"]["state"]
        payload = envelope("resume", 1, waitId=waiting["pending_wait"]["wait_id"], slotUpdates={"name": "Ada"})
        first = client.post("/v1/sessions/s/resume", json=payload)
        assert first.status_code == 200
        assert client.post("/v1/sessions/s/resume", json=payload).json() == first.json()
        completed = envelope("collect", 2, proposal={"status": "completed", "reply": "Collected"}, receipts=[receipt()])
        assert client.post("/v1/sessions/s/submit", json=completed).json()["payload"]["state"]["active_node_id"] == "approval"
        approval = client.post("/v1/sessions/s/submit", json=envelope("approval", 3,
            proposal={"status": "handoff", "reply": "Approve?"})).json()["payload"]["state"]
        assert approval["pending_wait"]["kind"] == "approval"
        assert client.post("/v1/sessions/s/resume", json=envelope("approved", 4,
            waitId=approval["pending_wait"]["wait_id"], response={"source": "human"})).status_code == 200
        assert client.post("/v1/sessions/s/submit", json=envelope("advance", 5,
            proposal={"status": "completed", "reply": "Approved"})).status_code == 200
        result = client.post("/v1/sessions/s/submit", json=envelope("done", 6,
            proposal={"status": "completed", "reply": "Done"}))
        assert result.json()["payload"]["state"]["status"] == "completed"
    with TestClient(create_app(str(tmp_path / "state.db"))) as reopened:
        assert reopened.get("/v1/sessions/s").json()["state"]["revision"] == 7
        assert reopened.post("/v1/sessions/s/submit", json=envelope("done", 6,
            proposal={"status": "completed", "reply": "Done"})).json() == result.json()


@pytest.mark.parametrize("proposal,receipts,code", [
    ({"status": "completed", "reply": "Done"}, [receipt()], "REQUIRED_SLOT_MISSING"),
    ({"status": "completed", "reply": "Done", "slotUpdates": {"name": "Ada"}}, [], "REQUIRED_CAPABILITY_NOT_INVOKED"),
    ({"status": "completed", "reply": "Done", "slotUpdates": {"name": "Ada"}, "nextStepId": "other"}, [receipt()], "INVALID_TRANSITION"),
    ({"status": "completed", "reply": "Done"}, [receipt(turnId="other")], "RECEIPT_SCOPE_MISMATCH"),
    ({"status": "completed", "reply": "Done"}, [receipt(nodeId="other")], "RECEIPT_SCOPE_MISMATCH"),
    ({"status": "completed", "reply": "Done"}, [receipt(capabilityId="other")], "CAPABILITY_UNDECLARED"),
])
def test_rejections_do_not_mutate_state(proposal, receipts, code):
    with TestClient(create_app()) as client:
        setup(client)
        response = client.post("/v1/sessions/s/submit", json=envelope("submit", 0, proposal=proposal, receipts=receipts))
        assert response.json()["error"]["code"] == code
        assert client.get("/v1/sessions/s").json()["state"]["revision"] == 0
        assert client.app.state.sop_store._connection.execute("SELECT count(*) FROM sop_events").fetchone()[0] == 1


def test_concurrent_submit_has_one_winner_and_idempotency_conflicts():
    with TestClient(create_app()) as client:
        setup(client)
        def submit(index):
            return client.post("/v1/sessions/s/submit", json=envelope(f"submit-{index}", 0,
                proposal={"status": "completed", "reply": "Done", "slotUpdates": {"name": "Ada"}}, receipts=[receipt()]))
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        winner = next(i for i, r in enumerate(results) if r.status_code == 200)
        conflict = client.post("/v1/sessions/s/submit", json=envelope(f"submit-{winner}", 0,
            proposal={"status": "completed", "reply": "Changed"}))
        assert conflict.json()["error"]["code"] == "SOP_REQUEST_CONFLICT"


def test_receipt_cannot_be_reused_on_loop_and_definition_is_pinned():
    definition = deepcopy(FLOW)
    definition["content"]["edges"][0]["next_node_id"] = "collect"
    with TestClient(create_app()) as client:
        setup(client, definition)
        payload = envelope("first", 0, proposal={"status": "completed", "reply": "Done", "slotUpdates": {"name": "Ada"}}, receipts=[receipt()])
        assert client.post("/v1/sessions/s/submit", json=payload).status_code == 200
        payload.update(requestId="second", operationId="second", expectedRevision=1)
        assert client.post("/v1/sessions/s/submit", json=payload).json()["error"]["code"] == "RECEIPT_DUPLICATE"
        changed = deepcopy(definition)
        changed["content"]["nodes"][0]["instruction"] = "changed"
        assert client.post("/v1/definitions", json=changed).status_code == 422
        changed["version"] = "2"
        assert client.post("/v1/definitions", json=changed).status_code == 200
        assert client.get("/v1/sessions/s").json()["definition"]["version"] == "1"


def subflow(index, child=None):
    return SopDefinition.from_value({"sop_id": str(index), "version": "1", "content": {
        "start_node_id": "node", "nodes": [{"node_id": "node", **({"type": "subflow", "sub_sop_id": str(child)} if child is not None else {})}], "terminal_node_ids": ["node"]}})


def test_nesting_depth_cycle_and_expanded_graph():
    definitions = {str(i): subflow(i, i + 1 if i < 8 else None) for i in range(9)}
    with pytest.raises(SopCoreError, match="8 levels"):
        expand(definitions["0"], definitions)
    definitions["8"] = subflow(8, 0)
    with pytest.raises(SopCoreError):
        expand(definitions["0"], definitions)
    definitions = {"0": subflow(0, 1), "1": subflow(1, 0)}
    with pytest.raises(SopCoreError, match="cycle"):
        expand(definitions["0"], definitions)
    definitions["1"] = subflow(1)
    assert expand(definitions["0"], definitions).content["start_node_id"] == "node::1::node"


def test_publication_token_controls_definition_writes():
    with TestClient(create_app(publish_token="publisher")) as client:
        assert client.post("/v1/definitions", json=FLOW).status_code == 401
        assert client.post("/v1/definitions", json=FLOW, headers={"Authorization": "Bearer publisher"}).status_code == 200


def test_approval_resume_cannot_be_reused_when_a_loop_returns_to_same_node():
    definition = {"sop_id": "approval-loop", "version": "1", "content": {
        "start_node_id": "approval", "nodes": [{"node_id": "approval", "type": "handoff"}],
        "edges": [{"source_node_id": "approval", "next_node_id": "approval"}]}}
    with TestClient(create_app()) as client:
        setup(client, definition)
        wait = client.post("/v1/sessions/s/submit", json=envelope("wait", 0,
            proposal={"status": "handoff", "reply": "Approve"})).json()["payload"]["state"]
        assert client.post("/v1/sessions/s/resume", json=envelope("resume", 1,
            waitId=wait["pending_wait"]["wait_id"], response={"source": "human"})).status_code == 200
        assert client.post("/v1/sessions/s/submit", json=envelope("advance", 2,
            proposal={"status": "completed", "reply": "Approved"})).status_code == 200
        rejection = client.post("/v1/sessions/s/submit", json=envelope("again", 3,
            proposal={"status": "completed", "reply": "Approved"}))
        assert rejection.json()["error"]["code"] == "HANDOFF_REQUIRED"


def test_local_definitions_restart_and_idempotency_key(tmp_path, monkeypatch):
    import json
    directory = tmp_path / "definitions"
    directory.mkdir()
    (directory / "flow.json").write_text(json.dumps(FLOW))
    monkeypatch.setenv("SOP_DEFINITIONS_DIR", str(directory))
    database = str(tmp_path / "runtime.db")
    with TestClient(create_app(database)) as client:
        payload = envelope("first", sopId="flow", idempotencyKey="prepare-key")
        first = client.post("/v1/sessions/s/prepare", json=payload)
        payload["requestId"] = "retry"
        assert client.post("/v1/sessions/s/prepare", json=payload).json() == first.json()
        invalid = envelope("invalid", sopId="flow", sessionId="other")
        assert client.post("/v1/sessions/s/prepare", json=invalid).status_code == 422
    with TestClient(create_app(database)) as reopened:
        assert reopened.get("/v1/sessions/s").json()["state"]["revision"] == 0


def test_failure_after_state_update_rolls_back_state_event_and_request(monkeypatch):
    with TestClient(create_app()) as client:
        setup(client)
        store = client.app.state.sop_store
        def fail(*args):
            raise ValueError("simulated journal failure")
        monkeypatch.setattr(store, "put_request", fail)
        response = client.post("/v1/sessions/s/submit", json=envelope("submit", 0,
            proposal={"status": "completed", "reply": "Done", "slotUpdates": {"name": "Ada"}}, receipts=[receipt()]))
        assert response.status_code == 422
        assert client.get("/v1/sessions/s").json()["state"]["revision"] == 0
        assert store._connection.execute("SELECT count(*) FROM sop_events").fetchone()[0] == 1
