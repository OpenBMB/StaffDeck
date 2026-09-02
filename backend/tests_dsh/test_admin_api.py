from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.db import get_session
from app.db.models import AgentProfile, HarnessInvocationRecord, ModelConfig, Tenant, User, new_id, utc_now
from app.security.auth import create_access_token, hash_password
from app.security.encryption import encrypt_secret
from staffdeck_dsh.api.admin import router
from staffdeck_dsh.modules import reset_registry
from staffdeck_dsh.security import reset_profile


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DSH_ENABLED", "false")
    monkeypatch.setenv("DSH_ADMIN_API_ENABLED", "true")
    monkeypatch.setenv("ULTRARAG_DOTENV", str(tmp_path / ".env"))
    get_settings.cache_clear()
    reset_registry()
    reset_profile()
    yield
    get_settings.cache_clear()
    reset_registry()
    reset_profile()


@pytest.fixture
def ctx(env):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    with db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(User(id="admin", tenant_id="tenant_demo", username="admin", role="admin", password_hash=hash_password("x")))
        db.add(AgentProfile(id="agent_1", tenant_id="tenant_demo", name="A", status="active", metadata_json={"owner_user_id": "admin"}))
        db.add(ModelConfig(id="m1", tenant_id="tenant_demo", name="GLM", provider="openai_compatible", base_url="http://x/v1", api_key_encrypted=encrypt_secret("k"), model="glm", is_default=True, enabled=True))
        db.commit()
        headers = {"Authorization": f"Bearer {create_access_token(db.get(User, 'admin'))}"}
    app = FastAPI()
    app.dependency_overrides[get_session] = lambda: Session(engine)
    app.include_router(router)
    with TestClient(app) as c:
        yield c, headers, db
    db.close()


def test_status_and_modules(ctx) -> None:
    c, headers, db = ctx
    r = c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["security_profile"] == "OSS_LOCAL"
    assert body["modules_total"] >= 20
    assert body["default_engine"] in {"legacy", "dsh"}
    mods = c.get("/api/enterprise/dsh/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert any(m["module_id"] == "engine.legacy" for m in mods)
    assert all(m["slot"] for m in mods)
    assert any(m["guarded"] and m["module_id"].startswith("channel.") for m in mods)


def test_requires_auth(ctx) -> None:
    c, _, _ = ctx
    assert c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}).status_code == 401


def test_snapshot_and_staff_engine_roundtrip(ctx) -> None:
    c, headers, db = ctx
    snap = c.get("/api/enterprise/dsh/snapshot", params={"tenant_id": "tenant_demo", "agent_id": "agent_1"}, headers=headers)
    assert snap.status_code == 200, snap.text
    assert snap.json()["snapshot_id"] and snap.json()["proxy_tools"]
    r = c.get("/api/enterprise/dsh/staff/agent_1/engine", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200 and r.json()["effective_engine"] in {"legacy", "dsh"}
    r = c.put("/api/enterprise/dsh/staff/agent_1/engine", json={"tenant_id": "tenant_demo", "engine": "dsh"}, headers=headers)
    assert r.status_code == 200 and r.json()["engine"] == "dsh" and r.json()["effective_engine"] == "dsh"
    assert (db.get(AgentProfile, "agent_1").metadata_json or {}).get("execution_engine") == "dsh"
    # effective_engine honors the persisted override
    from staffdeck_dsh.api.admin import effective_engine_for

    assert effective_engine_for(get_settings(), db.get(AgentProfile, "agent_1")) == "dsh"


def test_ledger_unknown_and_reconcile(ctx) -> None:
    c, headers, db = ctx
    row = HarnessInvocationRecord(id=new_id("hinvoke"), tenant_id="tenant_demo", session_id="s1", task_id="t1", run_id="r1", call_id="c1", tool_name="tool:tool.invoke/v1", request_digest="d", logical_action_key="sha256:x", status="outcome_unknown", arguments_json={"a": 1}, started_at=utc_now())
    db.add(row)
    db.commit()
    ru = c.get("/api/enterprise/dsh/ledger/unknown", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert ru.status_code == 200 and len(ru.json()) == 1 and ru.json()[0]["status"] == "outcome_unknown"
    r = c.post(f"/api/enterprise/dsh/ledger/{row.id}/reconcile", json={"tenant_id": "tenant_demo", "status": "failed"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert c.get("/api/enterprise/dsh/ledger/unknown", params={"tenant_id": "tenant_demo"}, headers=headers).json() == []
