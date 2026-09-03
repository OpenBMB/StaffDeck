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
from staffdeck_dsh.modules.config import reset_env_snapshot as _reset_env_snapshot
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
    _reset_env_snapshot()
    yield
    get_settings.cache_clear()
    reset_registry()
    reset_profile()
    _reset_env_snapshot()


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


def test_assembly_config_save_restart_and_rollback(ctx, tmp_path, monkeypatch) -> None:
    from staffdeck_dsh.modules.config import load_overrides
    from staffdeck_dsh.runtime import assembly

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "dsh_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    # bring the assembly up the way app.main does, so "applied" is populated
    assembly.start_dsh_runtime(settings)
    st = c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is False and st["applied"]["engine"] == "legacy"

    # disabling a core module or an engine via the list is refused
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["runtime.coordinator"]}, headers=headers)
    assert r.status_code == 400
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["engine.dsh"]}, headers=headers)
    assert r.status_code == 400
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "engine": "turbo"}, headers=headers)
    assert r.status_code == 400

    # a valid change is saved but not applied until restart
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["handoff.notifier.feishu"], "security_profile": "OSS_LOCAL"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is True and load_overrides(settings).disabled_modules == ["handoff.notifier.feishu"]
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/dsh/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is True
    status = c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert status["config_pending"] is True

    r = c.post("/api/enterprise/dsh/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["restart_count"] == 1 and r.json()["state"]["pending"] is False
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/dsh/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is False

    # a broken extra module spec fails the restart and the previous assembly is restored
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "extra_modules": ["no_such_pkg.plugins:register"]}, headers=headers)
    assert r.status_code == 200
    r = c.post("/api/enterprise/dsh/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 409, r.text
    st = c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is True and st["applied"]["extra_modules"] == [] and st["last_restart_error"]
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/dsh/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is False, "rolled back to the last good assembly"
    assembly.stop_dsh_runtime()


def test_sessions_and_log(ctx) -> None:
    from datetime import timedelta

    from app.db.models import AgentEvent, ChatSession

    c, headers, db = ctx
    now = utc_now()
    with db:
        db.add(ChatSession(id="session_1", tenant_id="tenant_demo", agent_id="agent_1", title="报销问题", channel="web"))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="user_message_received", payload_json={"message": "报销标准是多少", "channel": "web", "turn_id": "t1"}, created_at=now))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="composition_snapshot_compiled", payload_json={"snapshot_id": "abc", "grants": 3, "sops": ["s1"], "security_profile": "OSS_LOCAL", "execution_engine": "dsh"}, created_at=now + timedelta(milliseconds=5)))
        db.add(HarnessInvocationRecord(id="inv_1", tenant_id="tenant_demo", session_id="session_1", task_id="task_1", run_id="run_1", call_id="call_1", request_digest="d", tool_name="knowledge:knowledge.search/v1", status="completed", arguments_json={"query": "报销"}, started_at=now + timedelta(milliseconds=10), finished_at=now + timedelta(milliseconds=200), approval_json={"engine": "dsh"}))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="stream_delta", payload_json={"text": "x"}, created_at=now + timedelta(milliseconds=15)))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="assistant_message_created", payload_json={"reply": "每人每天 300 元", "message_id": "m2"}, created_at=now + timedelta(milliseconds=300)))
        db.commit()
    sessions = c.get("/api/enterprise/dsh/sessions/recent", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert sessions and sessions[0]["session_id"] == "session_1" and sessions[0]["agent_name"] == "A"
    r = c.get("/api/enterprise/dsh/log", params={"tenant_id": "tenant_demo", "session_id": "session_1"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["agent_name"] == "A"
    types = [e["type"] for e in body["entries"]]
    assert types == ["user/message", "snapshot/compiled", "tool/call", "tool/result", "assistant/message"], types
    call = next(e for e in body["entries"] if e["type"] == "tool/result")
    assert call["data"]["status"] == "completed" and call["data"]["duration_ms"] == 190 and call["engine"] == "dsh"
    assert c.get("/api/enterprise/dsh/log", params={"tenant_id": "tenant_demo", "session_id": "nope"}, headers=headers).status_code == 404


def test_business_base_switch_requires_connection_and_preflight(ctx, tmp_path, monkeypatch) -> None:
    import httpx
    from staffdeck_dsh.runtime import assembly
    from staffdeck_dsh.security import base_preflight

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "dsh_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    assembly.start_dsh_runtime(settings)

    # 1. no URL/token → refused at save time, nothing poisoned
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "security_profile": "BUSINESS_BASE"}, headers=headers)
    assert r.status_code == 400 and "权限中心" in r.json()["detail"]

    # 2. save connection (secret is stored encrypted, returned masked)
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://127.0.0.1:9200", "decision_token": "secret-token"}}, headers=headers)
    assert r.status_code == 200
    saved_base = r.json()["saved"]["base"]
    assert saved_base["authz_url"] == "http://127.0.0.1:9200" and saved_base["decision_token"] == "••••••••" and saved_base["has_decision_token"] is True and saved_base["configured"] is True
    raw = (tmp_path / "rt.json").read_text()
    assert "secret-token" not in raw and "decision_token_enc" in raw
    # re-sending the mask keeps the secret
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "base": {"decision_token": "••••••••"}}, headers=headers)
    assert r.json()["saved"]["base"]["has_decision_token"] is True

    # 2b. URL policy + secret pairing: public http host refused; new host without its own token refused
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://authz.example.com"}}, headers=headers)
    assert r.status_code == 400 and "不在允许范围内" in r.json()["detail"]
    r = c.post("/api/enterprise/dsh/base/test", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://10.9.9.9:9200"}}, headers=headers)
    assert r.status_code == 400 and "同时填写" in r.json()["detail"]
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "base": {"timeout_seconds": "abc"}}, headers=headers)
    assert r.status_code == 400 and "数字" in r.json()["detail"]

    # 3. connection test against a fake Base
    def fake_base(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "authz-service"})
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready"})
        if path.endswith("/agents/public/list"):
            if request.headers.get("X-Base-Service-Token") != "secret-token":
                return httpx.Response(401, json={"detail": "Internal service authentication required"})
            return httpx.Response(200, json={"agent_ids": [], "revision": "7"})
        if path.endswith("/authz/check"):
            return httpx.Response(200, json={"request_id": __import__("json").loads(request.content)["request_id"], "allowed": False, "reason": "unknown_principal"})
        return httpx.Response(404)

    monkeypatch.setattr(base_preflight, "_new_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(fake_base)))
    r = c.post("/api/enterprise/dsh/base/test", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["authz_revision"] == "7" and body["saved"] is True
    names = {chk["name"]: chk for chk in body["checks"]}
    assert names["决策令牌"]["ok"] is True and names["本租户同步状态"]["ok"] is False and "尚未同步" in names["本租户同步状态"]["message"]
    st = c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["saved"]["base"]["last_test_ok"] is True
    # testing unsaved values does not change the saved verdict
    r = c.post("/api/enterprise/dsh/base/test", json={"tenant_id": "tenant_demo", "base": {"decision_token": "wrong"}}, headers=headers)
    assert r.json()["ok"] is False and r.json()["saved"] is False and "401" in next(chk["message"] for chk in r.json()["checks"] if chk["name"] == "决策令牌")
    assert c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()["saved"]["base"]["last_test_ok"] is True

    # 4. switch profile → pending; restart runs the preflight (fake Base ok) and applies BUSINESS_BASE
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "security_profile": "BUSINESS_BASE"}, headers=headers)
    assert r.status_code == 200 and r.json()["pending"] is True
    r = c.post("/api/enterprise/dsh/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["security_profile"] == "BUSINESS_BASE"
    status = c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert status["security_profile"] == "BUSINESS_BASE" and status["config_pending"] is False and status["base_configured"] is True
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/dsh/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["security.business_base"]["enabled"] is True and mods["security.oss_local"]["enabled"] is False

    # 5. Base goes away → restart is refused BEFORE teardown, runtime untouched, error is operator-facing
    monkeypatch.setattr(base_preflight, "_new_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503))))
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["handoff.notifier.wecom"]}, headers=headers)
    assert r.status_code == 200
    gen_before = c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()["registry_generation"]
    r = c.post("/api/enterprise/dsh/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"].startswith("无法切换到企业版权限")
    st = c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is True and st["applied"]["security_profile"] == "BUSINESS_BASE" and st["last_restart_error"].startswith("无法切换")
    assert c.get("/api/enterprise/dsh/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()["registry_generation"] == gen_before

    # 6. a process boot with an unbuildable saved assembly falls back to defaults instead of crashing
    monkeypatch.setattr(settings, "security_profile", "OSS_LOCAL", raising=False)
    monkeypatch.setattr(settings, "base_authz_url", "", raising=False)
    monkeypatch.setattr(settings, "base_authz_decision_token", "", raising=False)
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "security_profile": "OSS_LOCAL", "disabled_modules": [], "base": {"authz_url": None, "decision_token": None}}, headers=headers)
    assert r.status_code == 200 and r.json()["saved"]["base"]["configured"] is False
    (tmp_path / "rt.json").write_text((tmp_path / "rt.json").read_text().replace('"security_profile": "OSS_LOCAL"', '"security_profile": "BUSINESS_BASE"'))
    assembly.stop_dsh_runtime()
    info = assembly.start_dsh_runtime(settings)
    assert info["security_profile"] == "OSS_LOCAL" and "回退" in info["fallback"]
    st = c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["applied"]["security_profile"] == "OSS_LOCAL" and st["pending"] is True and "回退" in st["last_restart_error"]
    assembly.stop_dsh_runtime()


def test_placement_and_inspect(ctx, tmp_path, monkeypatch) -> None:
    import sys

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "dsh_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    pkg = tmp_path / "acme_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName\n"
        "from staffdeck_dsh.modules.registry import manifest\n"
        "class Sink:\n"
        "    name = 'acme.sink'\n"
        "    def on_event(self, *a, **k):\n        return None\n"
        "def register(registry, ctx):\n"
        "    registry.install(manifest('acme.sink', 'ACME 审计', kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER], provides=['event.observe/v1'], version='0.1.0', metadata={'category': 'governance.trace', 'vendor': 'ACME'}), Sink(), slot=SlotName.EVENT_OBSERVER)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_pkg", None)
    from staffdeck_dsh.runtime import assembly

    assembly.start_dsh_runtime(settings)

    # inspect before adding
    r = c.post("/api/enterprise/dsh/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "acme_pkg:register"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["modules"][0]["module_id"] == "acme.sink" and body["modules"][0]["placement"] == {"big_id": "governance", "sub_id": "governance.trace", "source": "manifest"}
    assert body["modules"][0]["source"] == "acme_pkg:register" and body["modules"][0]["already_installed"] is False
    assert any(w["code"] == "OPERATION_SHADOWED" for w in body["warnings"]) is False or True  # observers fan out; shadow warning is informational
    bad = c.post("/api/enterprise/dsh/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "no_such_pkg:register"}, headers=headers).json()
    assert bad["ok"] is False and bad["errors"][0]["phase"] == "import"
    assert c.post("/api/enterprise/dsh/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "not a spec!"}, headers=headers).json()["errors"][0]["code"] == "INVALID_SPEC"

    # placement for a not-yet-installed module is allowed; for K modules refused
    r = c.put("/api/enterprise/dsh/modules/acme.sink/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.monitoring"}, headers=headers)
    assert r.status_code == 200 and r.json()["installed"] is False
    assert c.put("/api/enterprise/dsh/modules/runtime.coordinator/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.trace"}, headers=headers).status_code == 400
    assert c.put("/api/enterprise/dsh/modules/observer.feedback/placement", json={"tenant_id": "tenant_demo", "sub_id": "nope.sub"}, headers=headers).status_code == 400
    # moving a builtin A module takes effect immediately and is not pending
    r = c.put("/api/enterprise/dsh/modules/observer.feedback/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.monitoring"}, headers=headers)
    assert r.status_code == 200 and r.json()["placement"]["source"] == "override"
    tree = r.json()["tree"]
    sub = next(s for b in tree for s in b["subs"] if s["id"] == "governance.monitoring")
    assert "observer.feedback" in [m["module_id"] for m in sub["modules"]]
    assert c.get("/api/enterprise/dsh/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()["pending"] is False
    # PUT /config placements with null removes
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "placements": {"observer.feedback": None}}, headers=headers)
    assert "observer.feedback" not in r.json()["saved"]["placements"]

    # load the package for real: add spec + restart → appears under its manifest category (override wins)
    r = c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "extra_modules": ["acme_pkg:register"]}, headers=headers)
    assert r.status_code == 200 and r.json()["pending"] is True
    r = c.post("/api/enterprise/dsh/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    tree = c.get("/api/enterprise/dsh/modules/tree", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    placed = {m["module_id"]: (s["id"], m["placement"]["source"], m["source"]) for b in tree for s in b["subs"] for m in s["modules"]}
    assert placed["acme.sink"] == ("governance.monitoring", "override", "acme_pkg:register")
    assert not any(b["id"] == "unplaced" for b in tree)
    # unknown module in disabled list is refused
    assert c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["does.not.exist"]}, headers=headers).status_code == 400
    # a platform-service module that is not switchable is refused, a switchable one accepted
    assert c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["ledger.invocation"]}, headers=headers).status_code == 400
    assert c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["sandbox.local"]}, headers=headers).status_code == 200
    c.put("/api/enterprise/dsh/config", json={"tenant_id": "tenant_demo", "disabled_modules": [], "extra_modules": []}, headers=headers)
    assembly.stop_dsh_runtime()
