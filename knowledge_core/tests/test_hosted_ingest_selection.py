from knowledge_core.api import create_app
from support import full_runtime as Runtime
from knowledge_core.ports import LocalParser, ModelConfig
from fastapi.testclient import TestClient
import base64
import time


def test_async_ingestion_uses_uploaded_model_selection(tmp_path):
    scopes = []
    class Capabilities:
        def scoped(self, tenant_id, space_id, model_id):
            scopes.append((tenant_id, space_id, model_id))
            return Scoped()
    class Scoped(LocalParser):
        model_config = ModelConfig("hosted", "selected", "")
        def generate_json(self, config, prompt, payload):
            return {"buckets": [], "discoveries": []}
    runtime = Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}", capabilities=Capabilities())
    with TestClient(create_app(runtime)) as client:
        base = client.post("/api/km/v1/knowledge-bases", json={"space_id": "local", "name": "Policy"}).json()
        job = client.post(f"/api/km/v1/knowledge-bases/{base['id']}/documents", json={"space_id": "local", "filename": "policy.md", "content_base64": base64.b64encode(b"# Expense\n\nKeep original receipts.").decode(), "model_id": "chosen-config"}).json()
        for _ in range(100):
            result = client.get(f"/api/km/v1/jobs/{job['id']}").json()
            if result["status"] in {"failed", "succeeded"}: break
            time.sleep(.02)
        assert result["status"] == "succeeded", result
        assert ("local", "local", "chosen-config") in scopes
