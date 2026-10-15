import base64
from concurrent.futures import Future

from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.kernel import storage as s
from sqlalchemy import select
from knowledge_core.ports import ModelConfig
from support import full_runtime as Runtime
from test_independent_workflow import wait_job


class DeferredExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args):
        self.pending.append((fn, args))
        return Future()

    def shutdown(self, wait=True):
        pass

    def run(self):
        fn, args = self.pending.pop(0)
        fn(*args)


def upload(client):
    base = client.post("/api/km/v1/knowledge-bases", json={"name": "Capabilities"}).json()
    job = client.post(f"/api/km/v1/knowledge-bases/{base['id']}/documents", json={
        "filename": "policy.md", "content_base64": base64.b64encode(
            b"# Expense policy\n\nExpense reimbursement requires receipts."
        ).decode(),
    }).json()
    return base, job


def test_model_failure_falls_back_to_lexical_retrieval(tmp_path):
    class FailedModel:
        def generate_json(self, config, prompt, payload):
            raise RuntimeError("model unavailable")

    runtime = Runtime(f"sqlite:///{tmp_path / 'fallback.sqlite'}", model=FailedModel(),
                      model_config=ModelConfig("http://unused", "unused", "unused"))
    with TestClient(create_app(runtime)) as client:
        base, job = upload(client)
        assert wait_job(client, job["id"])["status"] == "succeeded"
        response = client.post("/api/km/v1/query", json={"query": "Expense receipts"})
        assert response.status_code == 200, response.text
        assert response.json()["chunks"]


def test_hosted_capabilities_use_job_scope_and_selected_model(tmp_path, monkeypatch):
    import httpx
    from knowledge_core.http_capabilities import HttpCapabilities

    calls = []
    def hosted(url, **kwargs):
        calls.append((url, kwargs))
        value = kwargs["json"]
        assert kwargs["headers"]["Authorization"] == "Bearer capability-token"
        assert value["tenant_id"] == "local" and value["space_id"] == "local"
        if url.endswith("/parse"):
            return httpx.Response(200, json={"text": "# Expense policy\n\nKeep expense receipts.", "file_type": "md"}, request=httpx.Request("POST", url))
        assert value["model_id"] == "selected"
        return httpx.Response(503, json={"detail": "Model unavailable"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", hosted)
    runtime = Runtime(f"sqlite:///{tmp_path / 'hosted.sqlite'}",
        capabilities=HttpCapabilities("http://host/capabilities", "capability-token"))
    with TestClient(create_app(runtime)) as client:
        base = client.post("/api/km/v1/knowledge-bases", json={"name": "Hosted"}).json()
        job = client.post(f"/api/km/v1/knowledge-bases/{base['id']}/documents", json={
            "filename": "policy.md", "content_base64": base64.b64encode(b"original").decode(), "model_id": "selected",
        }).json()
        assert wait_job(client, job["id"])["status"] == "succeeded"
        found = client.post("/api/km/v1/query", json={"query": "Expense receipts", "model_id": "selected"})
        assert found.status_code == 200 and found.json()["chunks"]
        assert any(url.endswith("/parse") for url, _ in calls)
        assert any(url.endswith("/model") for url, _ in calls)


def test_cancel_before_execution_clears_payload(tmp_path):
    executor = DeferredExecutor()
    runtime = Runtime(f"sqlite:///{tmp_path / 'cancel.sqlite'}", executor=executor)
    with TestClient(create_app(runtime)) as client:
        base, job = upload(client)
        response = client.post(f"/api/km/v1/jobs/{job['id']}/cancel")
        assert response.status_code == 200
        executor.run()
        result = client.get(f"/api/km/v1/jobs/{job['id']}").json()
        assert result["status"] == "cancelled"
        assert result["finished_at"]
        assert "content_base64" not in result["metadata"]
        assert client.get(f"/api/km/v1/knowledge-bases/{base['id']}/documents").json() == []


def test_background_authorization_failure_is_terminal(tmp_path):
    from knowledge_core.identity import SpaceAuthorization

    class RevocableAuthorization(SpaceAuthorization):
        denied = False

        def authorize(self, principal, space_id, action):
            if self.denied:
                raise RuntimeError("authorization unavailable")
            super().authorize(principal, space_id, action)

    executor, authorization = DeferredExecutor(), RevocableAuthorization()
    runtime = Runtime(f"sqlite:///{tmp_path / 'permission.sqlite'}", executor=executor,
                      authorization=authorization)
    with TestClient(create_app(runtime)) as client:
        _, job = upload(client)
        authorization.denied = True
        executor.run()
        with runtime.engine.connect() as db:
            result = db.execute(select(s.jobs).where(s.jobs.c.id == job['id'])).mappings().one()
            assert result['status'] == 'failed'
            assert result['error'] == 'authorization unavailable'
            assert result['finished_at'] and result['payload'] is None


def test_frozen_service_reads_but_rejects_writes(tmp_path):
    import sqlite3

    url = f"sqlite:///{tmp_path / 'frozen.sqlite'}"
    with TestClient(create_app(Runtime(url))) as client:
        _, job = upload(client)
        assert wait_job(client, job["id"])["status"] == "succeeded"
    def snapshot():
        with sqlite3.connect(tmp_path / 'frozen.sqlite') as db:
            return list(db.iterdump())

    before = snapshot()
    with TestClient(create_app(Runtime(url, read_only=True))) as client:
        assert client.get("/api/km/v1/spaces").status_code == 200
        assert client.get("/api/km/v1/knowledge-bases").status_code == 200
        assert client.post("/api/km/v1/query", json={"query": "Expense receipts"}).json()["chunks"]
        assert client.post("/api/km/v1/knowledge-bases", json={"name": "Denied"}).status_code == 503
    assert snapshot() == before


def test_frozen_empty_database_does_not_persist_default_identity_space(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.sqlite'}"
    runtime = Runtime(url)
    runtime.initialize()
    runtime.close()
    frozen = Runtime(url, read_only=True)
    with TestClient(create_app(frozen)) as client:
        assert client.get("/api/km/v1/spaces").json()[0]["id"] == "local"
        assert client.get("/api/km/v1/knowledge-bases").json() == []
        with frozen.engine.connect() as db:
            assert db.execute(select(frozen.plugins['spaces'].spaces.c.id)).scalar() == 'local'
