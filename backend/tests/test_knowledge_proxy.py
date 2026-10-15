from types import SimpleNamespace
import asyncio
import httpx

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import knowledge_proxy as proxy


def test_readiness_rejects_missing_endpoint(monkeypatch):
    monkeypatch.delenv("STAFFDECK_KM_URL", raising=False)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy.readiness(SimpleNamespace()))
    assert raised.value.status_code == 503


@pytest.mark.parametrize("access_status,unreachable,expected", [(200, False, 200), (401, False, 503), (200, True, 503)])
def test_readiness_checks_connection_and_service_identity(monkeypatch, access_status, unreachable, expected):
    monkeypatch.setenv("STAFFDECK_KM_URL", "http://km")
    monkeypatch.setenv("STAFFDECK_KM_BEARER", "service-token")
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if unreachable: raise httpx.ConnectError("unavailable")
            if url.endswith('/config'):
                return httpx.Response(200, json={'api_protocol': 'knowledge-core/v1', 'modules': {name: True for name in ('spaces', 'catalog', 'versions', 'concepts', 'discovery')}}, request=httpx.Request('GET', url))
            return httpx.Response(access_status if url.endswith("/identity") else 200)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", Client)
    if expected == 200:
        assert asyncio.run(proxy.readiness(SimpleNamespace()))["status"] == "ready"
    else:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(proxy.readiness(SimpleNamespace()))
        assert raised.value.status_code == expected
    if not unreachable:
        assert calls[-1] == ("http://km/api/km/v1/identity", {"headers": {"Authorization": "Bearer service-token"}})


def test_branch_projection_preserves_client_revision_and_rejects_missing_revision(monkeypatch):
    monkeypatch.setattr(proxy, "enabled", lambda: True)
    calls = []
    base = {"id": "base", "name": "Shared", "description": "", "status": "active", "updated_at": "shared-revision",
            "metadata": {}, "branch": {"head_version_id": "head", "status": "active"}}
    head = {"id": "head", "name": "Employee", "description": "", "updated_at": "head-revision", "capability_scope": "general"}

    class Adapter:
        def __init__(self, *args, **kwargs): self.agent_id = "employee"
        def require_base(self, *args): pass
        def call(self, method, path, value=None):
            calls.append((method, path, value))
            if method == "PUT":
                if value["expected_updated_at"] != head["updated_at"]:
                    raise HTTPException(409, "Concurrent edit")
                head["name"] = value["name"]
            if path == "/knowledge-bases":
                return [{key: value for key, value in base.items() if key != "branch"}]
            return [head] if path.endswith("/versions") else base

    monkeypatch.setattr(proxy, "StaffDeckKnowledgeAdapter", Adapter)
    db = SimpleNamespace(get=lambda *args: SimpleNamespace(is_overall=False))
    user = SimpleNamespace(tenant_id="tenant")
    request = lambda method: Request({"type": "http", "method": method, "path": "/api/km/v1/knowledge-bases/base", "query_string": b"space_id=sd:tenant:employee", "headers": []})
    row = proxy._proxy("knowledge-bases/base", request("GET"), b"", db, user)
    assert row["name"] == "Employee" and row["updated_at"] == "head-revision"
    assert proxy._proxy("knowledge-bases", request("GET"), b"", db, user)[0]["name"] == "Employee"
    for body, status in [(b'{"name":"Changed"}', 422), (b'{"name":"Changed","expected_updated_at":"stale"}', 409)]:
        with pytest.raises(HTTPException) as raised:
            proxy._proxy("knowledge-bases/base", request("PUT"), body, db, user)
        assert raised.value.status_code == status
    response = proxy._proxy("knowledge-bases/base", request("PUT"), b'{"name":"Changed","expected_updated_at":"head-revision"}', db, user)
    assert response.status_code == 200
    assert any(call[0] == "PUT" and call[2]["expected_updated_at"] == "head-revision" for call in calls)
