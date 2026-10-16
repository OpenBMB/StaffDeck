from fastapi.testclient import TestClient
from staffdeck_sop_runtime.api import create_app

DEFINITION = {
    "sop_id": "simple",
    "version": "1.0.0",
    "name": "Simple",
    "content": {
        "start_node_id": "start",
        "nodes": [
            {"node_id": "start", "type": "response", "allowed_actions": ["answer_user"]}
        ],
        "terminal_node_ids": ["start"],
    },
}


def test_new_sessions_use_published_child_version_instead_of_latest():
    child = {
        "sop_id": "child",
        "version": "1",
        "content": {
            "start_node_id": "done",
            "nodes": [{"node_id": "done", "instruction": "original"}],
        },
    }
    parent = {
        "sop_id": "parent",
        "version": "1",
        "content": {
            "start_node_id": "sub",
            "nodes": [
                {
                    "node_id": "sub",
                    "type": "subflow",
                    "sub_sop_id": "child",
                    "sub_sop_version": "1",
                }
            ],
        },
    }
    with TestClient(create_app()) as client:
        assert client.post("/v1/definitions", json=child).status_code == 200
        assert client.post("/v1/definitions", json=parent).status_code == 200
        child["version"] = "2"
        child["content"]["nodes"][0]["instruction"] = "changed"
        assert client.post("/v1/definitions", json=child).status_code == 200
        prepared = client.post(
            "/v1/sessions/pinned/prepare", json=envelope("p", sopId="parent")
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["payload"]["step"]["instruction"] == "original"


def envelope(request_id: str, **payload):
    return {
        "protocolVersion": "3.0",
        "requestId": request_id,
        "operationId": request_id,
        "turnId": "turn-1",
        **payload,
    }


def test_runtime_owns_session_state_and_replays_request():
    client = TestClient(create_app())
    assert client.post("/v1/definitions", json=DEFINITION).status_code == 200
    first = client.post(
        "/v1/sessions/s1/prepare", json=envelope("prepare-1", sopId="simple")
    )
    assert first.status_code == 200
    assert first.json()["payload"]["state"]["revision"] == 0

    payload = envelope(
        "submit-1",
        expectedRevision=0,
        proposal={"status": "completed", "reply": "Done"},
    )
    submitted = client.post("/v1/sessions/s1/submit", json=payload)
    assert submitted.status_code == 200
    assert submitted.json()["payload"]["state"]["revision"] == 1
    replay = client.post("/v1/sessions/s1/submit", json=payload)
    assert replay.status_code == 200
    assert replay.json() == submitted.json()


def test_runtime_rejects_stale_revision_and_survives_store_reopen(tmp_path):
    path = str(tmp_path / "sop.sqlite")
    client = TestClient(create_app(path))
    client.post("/v1/definitions", json=DEFINITION)
    client.post("/v1/sessions/s1/prepare", json=envelope("prepare-1", sopId="simple"))
    good = client.post(
        "/v1/sessions/s1/submit",
        json=envelope(
            "submit-1",
            expectedRevision=0,
            proposal={"status": "completed", "reply": "Done"},
        ),
    )
    assert good.status_code == 200
    stale = client.post(
        "/v1/sessions/s1/submit",
        json=envelope(
            "submit-2",
            expectedRevision=0,
            proposal={"status": "completed", "reply": "Again"},
        ),
    )
    assert stale.status_code == 409

    reopened = TestClient(create_app(path))
    current = reopened.get("/v1/sessions/s1")
    assert current.status_code == 200
    assert current.json()["state"]["revision"] == 1


def test_nested_capability_requirements_keep_expanded_node_scope_after_restart(
    tmp_path,
):
    path = str(tmp_path / "nested.sqlite")
    child = {
        "sop_id": "child",
        "version": "1",
        "name": "Child",
        "content": {
            "skill_id": "child",
            "start_node_id": "work",
            "terminal_node_ids": ["work"],
            "nodes": [
                {
                    "node_id": "work",
                    "type": "tool",
                    "allowed_actions": ["call_tool:lookup"],
                }
            ],
            "capability_requirements": [
                {
                    "kind": "tool",
                    "capability_id": "lookup",
                    "required": True,
                    "node_id": "work",
                }
            ],
        },
    }
    parent = {
        "sop_id": "parent",
        "version": "1",
        "name": "Parent",
        "content": {
            "start_node_id": "sub",
            "terminal_node_ids": ["sub"],
            "nodes": [{"node_id": "sub", "type": "subflow", "sub_sop_id": "child"}],
        },
    }
    with TestClient(create_app(path)) as client:
        assert client.post("/v1/definitions", json=child).status_code == 200
        assert client.post("/v1/definitions", json=parent).status_code == 200
        prepared = client.post(
            "/v1/sessions/nested/prepare", json=envelope("p", sopId="parent")
        )
        assert prepared.status_code == 200, prepared.text
        node_id = prepared.json()["payload"]["state"]["active_node_id"]
    with TestClient(create_app(path)) as client:
        definition = client.get("/v1/sessions/nested").json()["definition"]
        assert {r["node_id"] for r in definition["capability_requirements"]} == {
            node_id
        }
        submitted = client.post(
            "/v1/sessions/nested/submit",
            json=envelope(
                "s",
                expectedRevision=0,
                proposal={"status": "completed", "reply": "Done"},
                receipts=[
                    {
                        "capability_id": "lookup",
                        "operation_id": "call",
                        "node_id": node_id,
                        "session_id": "nested",
                        "turn_id": "turn-1",
                        "status": "succeeded",
                    }
                ],
            ),
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["payload"]["state"]["status"] == "completed"
