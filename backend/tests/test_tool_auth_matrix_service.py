from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import Tenant, Tool
from app.api.tools import probe_tool
from app.tools.tool_schema import ToolProbeRequest
from app.db.models import User
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


def _wait_for_port(port: int) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("auth matrix service did not start")


@pytest.fixture(scope="module")
def auth_matrix_url() -> str:
    port = 18081
    process = subprocess.Popen(
        [sys.executable, "-m", "mock_servers.auth_matrix_service", "--port", str(port)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=5)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()
    return db


@pytest.mark.parametrize(
    ("name", "path", "auth", "headers"),
    [
        ("customer.basic", "/basic", {"type": "basic", "basic": {"username": "demo", "password": "secret"}}, {}),
        ("customer.bearer", "/bearer", {"type": "bearer", "token": "bearer-secret"}, {}),
        ("customer.api_key", "/api-key", {"X-API-Key": "api-key-secret"}, {}),
        ("customer.custom", "/custom", {"X-Customer-Token": "customer-secret"}, {"X-Tenant-ID": "tenant_demo"}),
    ],
)
def test_real_auth_matrix_service_accepts_tool_configuration(
    auth_matrix_url: str, name: str, path: str, auth: dict, headers: dict
) -> None:
    with _session() as db:
        db.add(
            Tool(
                tenant_id="tenant_demo",
                name=name,
                method="POST",
                url=auth_matrix_url + path,
                auth_json=auth,
                headers_json=headers,
                enabled=True,
            )
        )
        db.commit()
        result = ToolExecutor(db).execute(
            "tenant_demo", ToolCall(name=name, arguments={"source": "现场"})
        )

    assert result.success is True, result.error
    assert result.data["ok"] is True
    assert result.data["scheme"] in {"basic", "bearer", "api-key", "custom"}


def test_real_auth_matrix_service_accepts_probe_configuration(auth_matrix_url: str) -> None:
    with _session() as db:
        user = User(
            id="user_member",
            tenant_id="tenant_demo",
            username="member",
            role="member",
            password_hash="test",
        )
        db.add(user)
        db.commit()
        result = probe_tool(
            ToolProbeRequest(
                tenant_id="tenant_demo",
                name="customer.custom",
                method="POST",
                url=auth_matrix_url + "/custom",
                auth={"X-Customer-Token": "customer-secret"},
                headers={"X-Tenant-ID": "tenant_demo"},
                sample_arguments={"source": "probe"},
            ),
            db,
            user,
        )

    assert result.success is True, result.error
    assert result.status_code == 200
    assert result.data_preview["scheme"] == "custom"
