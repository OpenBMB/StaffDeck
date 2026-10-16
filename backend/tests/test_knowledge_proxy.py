from types import SimpleNamespace
import asyncio
import json
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


@pytest.mark.parametrize("method,path,body,query", [
    ("POST", "knowledge-bases", {"space_id": "sd:tenant:private", "name": "Injected"},
     b"space_id=sd:tenant:owned"),
    ("POST", "query", {"space_id": "sd:tenant:private", "query": "secret"},
     b"space_id=sd:tenant:owned"),
    ("GET", "models", {}, b"space_id=sd:tenant:owned&space_id=sd:tenant:private"),
    ("POST", "knowledge-bases", {"space_id": []}, b""),
])
def test_proxy_rejects_conflicting_body_scope(monkeypatch, method, path, body, query):
    monkeypatch.setattr(proxy, "enabled", lambda: True)
    calls = []

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.agent_id, self.space_id = "owned", "sd:tenant:owned"
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [] if args[0] == "GET" else httpx.Response(201, json={})

    monkeypatch.setattr(proxy, "StaffDeckKnowledgeAdapter", Adapter)
    request = Request({"type": "http", "method": method, "path": "/api/km/v1/" + path,
                       "query_string": query, "headers": []})
    with pytest.raises(HTTPException) as raised:
        proxy._proxy(path, request, json.dumps(body).encode(),
                     SimpleNamespace(get=lambda *args: None), SimpleNamespace(tenant_id="tenant"))
    assert raised.value.status_code == 422
    assert not calls


@pytest.mark.parametrize("path,method,body", [
    ("knowledge-bases", "POST", {"name": "Default space"}),
    ("query", "POST", {"query": "receipts"}),
    ("models", "GET", None),
])
def test_proxy_always_forwards_authorized_host_space(monkeypatch, path, method, body):
    monkeypatch.setattr(proxy, "enabled", lambda: True)
    calls = []

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.agent_id, self.space_id = "overall", "sd:tenant:overall"
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [] if args[0] == "GET" and args[1] == "/knowledge-bases" else httpx.Response(200, json={})

    monkeypatch.setattr(proxy, "StaffDeckKnowledgeAdapter", Adapter)
    request = Request({"type": "http", "method": method, "path": "/api/km/v1/" + path,
                       "query_string": b"", "headers": []})
    proxy._proxy(path, request, json.dumps(body).encode() if body else b"",
                 SimpleNamespace(get=lambda *args: None), SimpleNamespace(tenant_id="tenant"))
    args, kwargs = calls[-1]
    assert kwargs["scoped"] is True
    if body is not None:
        assert args[2]["space_id"] == "sd:tenant:overall"


def test_proxy_body_scopes_use_real_host_permissions(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlmodel import Session, SQLModel, create_engine
    from app.db.models import AgentProfile, Tenant, User
    from app.knowledge import http_adapter
    from knowledge_core.api import create_app
    from knowledge_core.configuration import Configuration
    from knowledge_core.identity import Principal, StaticBearerIdentity
    from knowledge_core.runtime import Runtime

    host_engine = create_engine(f"sqlite:///{tmp_path / 'host.sqlite'}")
    SQLModel.metadata.create_all(host_engine)
    runtime = Runtime(f"sqlite:///{tmp_path / 'km.sqlite'}",
                      configuration=Configuration('full', {'authentication': True}),
                      identity=StaticBearerIdentity({'host': Principal('host', 'tenant')}))
    monkeypatch.setenv("STAFFDECK_KM_URL", "http://knowledge")
    monkeypatch.setenv("STAFFDECK_KM_BEARER", "host")
    with TestClient(create_app(runtime)) as client, Session(host_engine) as db:
        user = User(id="member", tenant_id="tenant", username="member", password_hash="", role="member")
        db.add(Tenant(id="tenant", name="Tenant"))
        db.add(user)
        for name in ("owned", "private"):
            db.add(AgentProfile(id=name, tenant_id="tenant", name=name,
                                metadata_json={"owner_user_id": user.id if name == "owned" else "other"}))
            assert client.post("/api/km/v1/spaces", json={"id": "sd:tenant:" + name, "name": name},
                               headers={"Authorization": "Bearer host"}).status_code == 201
        db.commit()
        monkeypatch.setattr(http_adapter.httpx, "request", lambda method, url, **kwargs:
                            client.request(method, url.removeprefix("http://knowledge"),
                                           **{key: value for key, value in kwargs.items() if key != "timeout"}))
        request = Request({"type": "http", "method": "POST", "path": "/api/km/v1/knowledge-bases",
                           "query_string": b"space_id=sd:tenant:owned", "headers": []})
        with pytest.raises(HTTPException) as raised:
            proxy._proxy("knowledge-bases", request,
                         b'{"name":"Injected","space_id":"sd:tenant:private"}', db, user)
        assert raised.value.status_code == 422
        response = proxy._proxy("knowledge-bases", request, b'{"name":"Owned base"}', db, user)
        assert response.status_code == 201
        headers = {"Authorization": "Bearer host"}
        assert len(client.get("/api/km/v1/knowledge-bases?space_id=sd:tenant:owned", headers=headers).json()) == 1
        assert client.get("/api/km/v1/knowledge-bases?space_id=sd:tenant:private", headers=headers).json() == []
    host_engine.dispose()
