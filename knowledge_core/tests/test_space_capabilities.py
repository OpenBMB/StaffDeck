import base64
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.http_capabilities import CapabilityBinding, SpaceCapabilities
from support import full_runtime as Runtime


def test_overlapping_bindings_and_missing_credentials_fail_at_start(monkeypatch):
    monkeypatch.setenv("KM_HOST_A", "a")
    row = CapabilityBinding("local", "a", "http://host-a/km", "KM_HOST_A")
    with pytest.raises(ValueError, match="Overlapping"):
        SpaceCapabilities([row, row])
    monkeypatch.delenv("KM_HOST_A")
    with pytest.raises(ValueError, match="credential unavailable"):
        SpaceCapabilities([row])


def test_background_and_query_route_to_the_space_provider(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("KM_HOST_A", "a-token")
    monkeypatch.setenv("KM_HOST_B", "b-token")
    capabilities = SpaceCapabilities([
        CapabilityBinding("local", space, "http://host-" + space + "/km", "KM_HOST_" + space.upper())
        for space in ("a", "b")
    ])
    assert capabilities.scoped("other", "a") is None
    def request(url, **kwargs):
        value = kwargs["json"]
        space = value["space_id"]
        assert url.startswith("http://host-" + space + "/km/")
        assert kwargs["headers"]["Authorization"] == "Bearer " + space + "-token"
        assert value["model_id"] == space + "-model"
        calls.append((space, url.rsplit("/", 1)[-1]))
        result = {"text": "# Receipt\n\nKeep the original receipt.", "file_type": "md"} if url.endswith("/parse") else {"buckets": [], "discoveries": []}
        return httpx.Response(200, json=result, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", request)
    with TestClient(create_app(Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}", capabilities=capabilities))) as client:
        for space in ("a", "b"):
            assert client.post("/api/km/v1/spaces", json={"id": space, "name": space}).status_code == 201
            base = client.post("/api/km/v1/knowledge-bases", json={"space_id": space, "name": space}).json()
            job = client.post(f"/api/km/v1/knowledge-bases/{base['id']}/documents", json={"space_id": space, "filename": "receipt.md", "content_base64": base64.b64encode(b"source").decode(), "model_id": space + "-model"}).json()
            for _ in range(100):
                result = client.get(f"/api/km/v1/jobs/{job['id']}", params={"space_id": space}).json()
                if result["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(.02)
            assert result["status"] == "succeeded", result
            response = client.post("/api/km/v1/query", json={"space_id": space, "query": "Receipt", "model_id": space + "-model"})
            assert response.status_code == 200
    assert all((space, operation) in calls for space in ("a", "b") for operation in ("parse", "model"))
