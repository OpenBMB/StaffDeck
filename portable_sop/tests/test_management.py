from copy import deepcopy

from fastapi.testclient import TestClient
from staffdeck_sop_runtime.api import create_app


def content(version="1.0.0", sop_id="approval"):
    return {"skill_id": sop_id, "version": version, "name": "Approval",
            "start_node_id": "approval", "terminal_node_ids": ["approval"], "edges": [],
            "nodes": [{"node_id": "approval", "type": "handoff",
                       "assignee_ref": "pilotdeck:owner", "instruction": "Ask approval"}]}


def call(client, operation, **data):
    return client.post("/v1/management/call", json={"operation": operation, "input": data})


def publish(client, value):
    draft = call(client, "create", content=value).json()["result"]
    return call(client, "publish", sopId=value["skill_id"], draftId=draft["id"], expectedRevision=0)


def test_management_conflicts_versions_pointer_and_archive(tmp_path):
    path = str(tmp_path / "runtime.sqlite")
    with TestClient(create_app(path)) as client:
        draft = call(client, "create", content=content()).json()["result"]
        assert call(client, "replace_draft", sopId="approval", draftId=draft["id"],
                    expectedRevision=1, content=content()).status_code == 422
        assert call(client, "publish", sopId="approval", draftId=draft["id"], expectedRevision=0).is_success
        assert client.get("/v1/management/catalog").json()["definitions"][0]["content"]["nodes"][0]["assignee_user_id"] == "pilotdeck:owner"
        initial = {"protocolVersion": "3.0", "requestId": "initial", "operationId": "initial", "turnId": "t", "sopId": "approval"}
        assert client.post("/v1/sessions/pinned/prepare", json=initial).json()["payload"]["state"]["sop_version"] == "1.0.0"
        assert publish(client, content("2.0.0")).is_success
        assert client.post("/v1/sessions/fresh/prepare", json=initial).json()["payload"]["state"]["sop_version"] == "2.0.0"
        assert client.post("/v1/sessions/pinned/prepare", json={**initial, "requestId": "after-publish"}).json()["payload"]["state"]["sop_version"] == "1.0.0"
        changed = content()
        changed["name"] = "Changed"
        assert publish(client, changed).status_code == 422
        assert call(client, "rollback", sopId="approval", version="1.0.0").is_success
        envelope = {"protocolVersion": "3.0", "requestId": "prepare", "operationId": "prepare", "turnId": "t", "sopId": "approval"}
        assert client.post("/v1/sessions/original/prepare", json=envelope).json()["payload"]["state"]["sop_version"] == "1.0.0"
        assert call(client, "archive", sopId="approval").is_success
        assert client.post("/v1/sessions/new/prepare", json=envelope).status_code == 422
        assert client.post("/v1/sessions/original/prepare", json={**envelope, "requestId": "again"}).is_success
    with TestClient(create_app(path)) as client:
        assert client.get("/v1/sessions/original").json()["state"]["sop_version"] == "1.0.0"
        assert len(call(client, "list_versions", sopId="approval").json()["result"]["data"]) == 2


def test_management_children_and_transaction_rollback():
    with TestClient(create_app()) as client:
        parent = content(sop_id="parent")
        parent["nodes"] = [{"node_id": "approval", "type": "subflow", "sub_sop_id": "child"}]
        assert publish(client, parent).status_code == 422
        assert publish(client, content(sop_id="child")).is_success
        assert publish(client, parent).is_success
        definition = next(d for d in client.get("/v1/management/catalog").json()["definitions"] if d["sop_id"] == "parent")
        assert definition["content"]["nodes"][0]["sub_sop_version"] == "1.0.0"
        recursive = deepcopy(parent)
        recursive["skill_id"] = "cycle"
        recursive["nodes"][0]["sub_sop_id"] = "cycle"
        assert publish(client, recursive).status_code == 422


def test_generation_cancellation_conflict_and_replay():
    with TestClient(create_app()) as client:
        job = call(client, "job_create", input={"prompt": "approval"}).json()["result"]
        assert call(client, "job_start", jobId=job["id"], workerId="worker").is_success
        assert call(client, "job_chunk", jobId=job["id"], workerId="wrong", content="x").status_code == 422
        assert call(client, "job_chunk", jobId=job["id"], workerId="worker", content="x").is_success
        assert call(client, "job_complete", jobId=job["id"], workerId="worker", content=content()).is_success
        assert client.get("/v1/management/catalog").json()["definitions"] == []
        events = call(client, "job_events", jobId=job["id"], afterSequence=1).json()["result"]["data"]
        assert [e["sequence"] for e in events] == [2, 3, 4]
        cancelled = call(client, "job_create", input={"prompt": "approval"}).json()["result"]
        call(client, "job_cancel", jobId=cancelled["id"])
        assert call(client, "job_start", jobId=cancelled["id"], workerId="worker").status_code == 422


def test_management_requires_publication_credential():
    with TestClient(create_app(publish_token="private")) as client:
        assert call(client, "create", content=content()).status_code == 401


def test_worker_restart_interruption_and_failed_rewrite_preserve_draft(tmp_path):
    path = str(tmp_path / "runtime.sqlite")
    with TestClient(create_app(path)) as client:
        draft = call(client, "create", content=content()).json()["result"]
        job = call(client, "job_create", input={"sopId": "approval", "draftId": draft["id"], "etag": "0"}).json()["result"]
        call(client, "job_start", jobId=job["id"], workerId="old")
        invalid = content(); invalid["edges"] = [{"source_node_id": "approval", "next_node_id": "absent"}]
        assert call(client, "job_complete", jobId=job["id"], workerId="old", content=invalid).status_code == 422
        assert call(client, "get_draft", sopId="approval", draftId=draft["id"]).json()["result"] == draft
    with TestClient(create_app(path)) as client:
        recovered = call(client, "job_recover", workerId="new").json()["result"]["data"]
        assert [row["status"] for row in recovered] == ["interrupted"]
        assert call(client, "job_complete", jobId=job["id"], workerId="old", content=content()).status_code == 422


def test_publication_depth_and_delete_preserve_session_snapshot():
    with TestClient(create_app()) as client:
        assert publish(client, content(sop_id="level0")).is_success
        for depth in range(1, 8):
            value = content(sop_id=f"level{depth}")
            value["nodes"] = [{"node_id": "approval", "type": "subflow", "sub_sop_id": f"level{depth - 1}"}]
            assert publish(client, value).is_success
        value = content(sop_id="level8")
        value["nodes"] = [{"node_id": "approval", "type": "subflow", "sub_sop_id": "level7"}]
        assert publish(client, value).status_code == 422
        envelope = {"protocolVersion": "3.0", "requestId": "p", "operationId": "p", "turnId": "t", "sopId": "level0"}
        assert client.post("/v1/sessions/saved/prepare", json=envelope).is_success
        assert call(client, "delete", sopId="level0").is_success
        assert client.post("/v1/sessions/new/prepare", json=envelope).status_code == 422
        assert client.post("/v1/sessions/saved/prepare", json={**envelope, "requestId": "p2"}).is_success
