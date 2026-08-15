from __future__ import annotations

import base64

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import Tenant, Tool
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_demo", name="Demo"))
    return db


def test_http_tool_auth_json_reaches_protected_service(monkeypatch) -> None:
    expected_basic = "Basic " + base64.b64encode(b"demo:secret").decode("ascii")
    seen: list[httpx.Headers] = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        if request.headers.get("authorization") != expected_basic:
            return httpx.Response(401, json={"detail": "unauthorized"}, request=request)
        if request.headers.get("x-tenant-id") != "tenant_demo":
            return httpx.Response(401, json={"detail": "missing tenant"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = httpx.MockTransport(service)

    class Client(httpx.Client):
        def __init__(self, *, timeout):
            super().__init__(transport=transport, timeout=timeout)

    monkeypatch.setattr(httpx, "Client", Client)
    with _session() as db:
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="protected.lookup",
                method="POST",
                url="https://customer.example.test/lookup",
                auth_json={"type": "basic", "basic": {"username": "demo", "password": "secret"}},
                headers_json={"X-Tenant-ID": "tenant_demo"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name="protected.lookup", arguments={"id": "A-1"})
        )

    assert result.success is True
    assert result.data == {"ok": True}
    assert seen and seen[0]["authorization"] == expected_basic
    assert seen[0]["x-tenant-id"] == "tenant_demo"


def test_custom_auth_json_is_sent_as_literal_headers(monkeypatch) -> None:
    def service(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != "vendor-secret":
            return httpx.Response(401, request=request)
        return httpx.Response(200, json={"accepted": True}, request=request)

    transport = httpx.MockTransport(service)

    class Client(httpx.Client):
        def __init__(self, *, timeout):
            super().__init__(transport=transport, timeout=timeout)

    monkeypatch.setattr(httpx, "Client", Client)
    with _session() as db:
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name="vendor.lookup",
                method="POST",
                url="https://customer.example.test/lookup",
                auth_json={"X-API-Key": "vendor-secret"},
                enabled=True,
            )
        )
        db.commit()

        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name="vendor.lookup", arguments={})
        )

    assert result.success is True
    assert result.data == {"accepted": True}
