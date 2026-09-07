from __future__ import annotations

import pytest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.db import get_session
from app.db.models import AgentProfile, HarnessInvocationRecord, ModelConfig, Tenant, User, new_id, utc_now
from app.security.auth import create_access_token, hash_password
from app.security.encryption import encrypt_secret
from staffdeck_harness.api.admin import router
from staffdeck_harness.modules import reset_registry
from staffdeck_harness.modules.config import reset_env_snapshot as _reset_env_snapshot
from staffdeck_harness.security import reset_profile


@pytest.fixture
def env(tmp_path, monkeypatch):
    from staffdeck_harness.runtime import assembly
    from staffdeck_harness.bridge import engine_host
    runtime = SimpleNamespace(mcp_url="http://127.0.0.1:9999/mcp", registry=[],
        worker_config=SimpleNamespace(harness_v3_root=tmp_path, harness_v3_home=tmp_path))
    monkeypatch.setattr(assembly, "get_runtime", lambda settings: runtime)
    monkeypatch.setattr(engine_host, "get_runtime", lambda settings: runtime)
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_V3_ENABLED", "false")
    monkeypatch.setenv("HARNESS_ADMIN_API_ENABLED", "true")
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
    r = c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["security_profile"] == "OSS_LOCAL"
    assert body["modules_total"] >= 20
    assert body["default_engine"] in {"harness_v2", "harness_v3"}
    mods = c.get("/api/enterprise/harness/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert any(m["module_id"] == "engine.harness_v3" for m in mods)
    assert not any(m["module_id"] == "engine.harness_v2" for m in mods)
    assert all(m["slot"] for m in mods)
    assert any(m["guarded"] and m["module_id"].startswith("channel.") for m in mods)


def test_requires_auth(ctx) -> None:
    c, _, _ = ctx
    assert c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}).status_code == 401


def test_snapshot_and_staff_engine_roundtrip(ctx) -> None:
    c, headers, db = ctx
    snap = c.get("/api/enterprise/harness/snapshot", params={"tenant_id": "tenant_demo", "agent_id": "agent_1"}, headers=headers)
    assert snap.status_code == 200, snap.text
    assert snap.json()["snapshot_id"] and snap.json()["proxy_tools"]
    r = c.get("/api/enterprise/harness/staff/agent_1/engine", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200 and r.json()["effective_engine"] in {"harness_v2", "harness_v3"}
    r = c.put("/api/enterprise/harness/staff/agent_1/engine", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers)
    assert r.status_code == 200 and r.json()["engine"] == "harness_v3" and r.json()["effective_engine"] == "harness_v3"
    assert (db.get(AgentProfile, "agent_1").metadata_json or {}).get("execution_engine") == "harness_v3"
    # effective_engine honors the persisted override
    from staffdeck_harness.api.admin import effective_engine_for

    assert effective_engine_for(get_settings(), db.get(AgentProfile, "agent_1")) == "harness_v3"


def test_ledger_unknown_and_reconcile(ctx) -> None:
    c, headers, db = ctx
    row = HarnessInvocationRecord(id=new_id("hinvoke"), tenant_id="tenant_demo", session_id="s1", task_id="t1", run_id="r1", call_id="c1", tool_name="tool:tool.invoke/v1", request_digest="d", logical_action_key="sha256:x", status="outcome_unknown", arguments_json={"a": 1}, started_at=utc_now())
    db.add(row)
    db.commit()
    ru = c.get("/api/enterprise/harness/ledger/unknown", params={"tenant_id": "tenant_demo"}, headers=headers)
    assert ru.status_code == 200 and len(ru.json()) == 1 and ru.json()[0]["status"] == "outcome_unknown"
    r = c.post(f"/api/enterprise/harness/ledger/{row.id}/reconcile", json={"tenant_id": "tenant_demo", "status": "failed"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert c.get("/api/enterprise/harness/ledger/unknown", params={"tenant_id": "tenant_demo"}, headers=headers).json() == []


def test_assembly_config_save_restart_and_rollback(ctx, tmp_path, monkeypatch) -> None:
    from staffdeck_harness.modules.config import load_overrides
    from staffdeck_harness.runtime import assembly

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "harness_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    # bring the assembly up the way app.main does, so "applied" is populated
    assembly.start_harness_runtime(settings)
    st = c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is False and st["applied"]["engine"] == "harness_v3"

    # disabling a core module or an engine via the list is refused
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["runtime.coordinator"]}, headers=headers)
    assert r.status_code == 400
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["engine.harness_v3"]}, headers=headers)
    assert r.status_code == 400
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "turbo"}, headers=headers)
    assert r.status_code == 400

    # a valid change is saved but not applied until restart
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["handoff.notifier.feishu"], "security_profile": "OSS_LOCAL"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is True and load_overrides(settings).disabled_modules == ["handoff.notifier.feishu"]
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/harness/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is True
    status = c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert status["config_pending"] is True

    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["restart_count"] == 1 and r.json()["state"]["pending"] is False
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/harness/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is False

    # a broken extra module spec fails the restart and the previous assembly is restored
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "extra_modules": ["no_such_pkg.plugins:register"]}, headers=headers)
    assert r.status_code == 200
    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 409, r.text
    st = c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is True and st["applied"]["extra_modules"] == [] and st["last_restart_error"]
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/harness/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["handoff.notifier.feishu"]["enabled"] is False, "rolled back to the last good assembly"
    assembly.stop_harness_runtime()


def test_sessions_and_log(ctx) -> None:
    from datetime import timedelta

    from app.db.models import AgentEvent, ChatSession

    c, headers, db = ctx
    now = utc_now()
    with db:
        db.add(ChatSession(id="session_1", tenant_id="tenant_demo", agent_id="agent_1", title="报销问题", channel="web"))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="user_message_received", payload_json={"message": "报销标准是多少", "channel": "web", "turn_id": "t1"}, created_at=now))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="composition_snapshot_compiled", payload_json={"snapshot_id": "abc", "grants": 3, "sops": ["s1"], "security_profile": "OSS_LOCAL", "execution_engine": "harness_v3"}, created_at=now + timedelta(milliseconds=5)))
        db.add(HarnessInvocationRecord(id="inv_1", tenant_id="tenant_demo", session_id="session_1", task_id="task_1", run_id="run_1", call_id="call_1", request_digest="d", tool_name="knowledge:knowledge.search/v1", status="completed", arguments_json={"query": "报销"}, started_at=now + timedelta(milliseconds=10), finished_at=now + timedelta(milliseconds=200), approval_json={"engine": "harness_v3"}))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="stream_delta", payload_json={"text": "x"}, created_at=now + timedelta(milliseconds=15)))
        db.add(AgentEvent(tenant_id="tenant_demo", session_id="session_1", event_type="assistant_message_created", payload_json={"reply": "每人每天 300 元", "message_id": "m2"}, created_at=now + timedelta(milliseconds=300)))
        db.commit()
    sessions = c.get("/api/enterprise/harness/sessions/recent", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert sessions and sessions[0]["session_id"] == "session_1" and sessions[0]["agent_name"] == "A"
    r = c.get("/api/enterprise/harness/log", params={"tenant_id": "tenant_demo", "session_id": "session_1"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["agent_name"] == "A"
    types = [e["type"] for e in body["entries"]]
    assert types == ["user/message", "snapshot/compiled", "tool/call", "tool/result", "assistant/message"], types
    call = next(e for e in body["entries"] if e["type"] == "tool/result")
    assert call["data"]["status"] == "completed" and call["data"]["duration_ms"] == 190 and call["engine"] == "harness_v3"
    assert c.get("/api/enterprise/harness/log", params={"tenant_id": "tenant_demo", "session_id": "nope"}, headers=headers).status_code == 404


def test_business_base_switch_requires_connection_and_preflight(ctx, tmp_path, monkeypatch) -> None:
    import httpx
    from staffdeck_harness.runtime import assembly
    from staffdeck_harness.security import base_preflight

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "harness_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    assembly.start_harness_runtime(settings)

    # 1. no URL/token → refused at save time, nothing poisoned
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "security_profile": "BUSINESS_BASE"}, headers=headers)
    assert r.status_code == 400 and "权限中心" in r.json()["detail"]

    # 2. save connection (secret is stored encrypted, returned masked)
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://127.0.0.1:9200", "decision_token": "secret-token"}}, headers=headers)
    assert r.status_code == 200
    saved_base = r.json()["saved"]["base"]
    assert saved_base["authz_url"] == "http://127.0.0.1:9200" and saved_base["decision_token"] == "••••••••" and saved_base["has_decision_token"] is True and saved_base["configured"] is True
    raw = (tmp_path / "rt.json").read_text()
    assert "secret-token" not in raw and "decision_token_enc" in raw
    # re-sending the mask keeps the secret
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "base": {"decision_token": "••••••••"}}, headers=headers)
    assert r.json()["saved"]["base"]["has_decision_token"] is True

    # 2b. URL policy + secret pairing: public http host refused; new host without its own token refused
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://authz.example.com"}}, headers=headers)
    assert r.status_code == 400 and "不在允许范围内" in r.json()["detail"]
    r = c.post("/api/enterprise/harness/base/test", json={"tenant_id": "tenant_demo", "base": {"authz_url": "http://10.9.9.9:9200"}}, headers=headers)
    assert r.status_code == 400 and "同时填写" in r.json()["detail"]
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "base": {"timeout_seconds": "abc"}}, headers=headers)
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
    r = c.post("/api/enterprise/harness/base/test", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["authz_revision"] == "7" and body["saved"] is True
    names = {chk["name"]: chk for chk in body["checks"]}
    assert names["决策令牌"]["ok"] is True and names["本租户同步状态"]["ok"] is False and "尚未同步" in names["本租户同步状态"]["message"]
    st = c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["saved"]["base"]["last_test_ok"] is True
    # testing unsaved values does not change the saved verdict
    r = c.post("/api/enterprise/harness/base/test", json={"tenant_id": "tenant_demo", "base": {"decision_token": "wrong"}}, headers=headers)
    assert r.json()["ok"] is False and r.json()["saved"] is False and "401" in next(chk["message"] for chk in r.json()["checks"] if chk["name"] == "决策令牌")
    assert c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()["saved"]["base"]["last_test_ok"] is True

    # 4. switch profile → pending; restart runs the preflight (fake Base ok) and applies BUSINESS_BASE
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "security_profile": "BUSINESS_BASE"}, headers=headers)
    assert r.status_code == 200 and r.json()["pending"] is True
    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["security_profile"] == "BUSINESS_BASE"
    status = c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert status["security_profile"] == "BUSINESS_BASE" and status["config_pending"] is False and status["base_configured"] is True
    mods = {m["module_id"]: m for m in c.get("/api/enterprise/harness/modules", params={"tenant_id": "tenant_demo"}, headers=headers).json()}
    assert mods["security.business_base"]["enabled"] is True and mods["security.oss_local"]["enabled"] is False

    # 5. Base goes away → restart is refused BEFORE teardown, runtime untouched, error is operator-facing
    monkeypatch.setattr(base_preflight, "_new_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503))))
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["handoff.notifier.wecom"]}, headers=headers)
    assert r.status_code == 200
    gen_before = c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()["registry_generation"]
    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 409 and r.json()["detail"].startswith("无法切换到企业版权限")
    st = c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert st["pending"] is True and st["applied"]["security_profile"] == "BUSINESS_BASE" and st["last_restart_error"].startswith("无法切换")
    assert c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()["registry_generation"] == gen_before

    # 6. a process boot with an unbuildable saved assembly falls back to defaults instead of crashing
    monkeypatch.setattr(settings, "security_profile", "OSS_LOCAL", raising=False)
    monkeypatch.setattr(settings, "base_authz_url", "", raising=False)
    monkeypatch.setattr(settings, "base_authz_decision_token", "", raising=False)
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "security_profile": "OSS_LOCAL", "disabled_modules": [], "base": {"authz_url": None, "decision_token": None}}, headers=headers)
    assert r.status_code == 200 and r.json()["saved"]["base"]["configured"] is False
    (tmp_path / "rt.json").write_text((tmp_path / "rt.json").read_text().replace('"security_profile": "OSS_LOCAL"', '"security_profile": "BUSINESS_BASE"'))
    assembly.stop_harness_runtime()
    with pytest.raises(assembly.AssemblyFailed, match="禁止自动降级"):
        assembly.start_harness_runtime(settings)
    assert assembly.assembly_state(settings)["applied"] is None
    assembly.stop_harness_runtime()


def test_placement_and_inspect(ctx, tmp_path, monkeypatch) -> None:
    import sys

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "harness_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    pkg = tmp_path / "acme_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from staffdeck_harness.contracts.manifest import ModuleKind, SlotName\n"
        "from staffdeck_harness.modules.registry import manifest\n"
        "class Sink:\n"
        "    name = 'acme.sink'\n"
        "    def on_event(self, *a, **k):\n        return None\n"
        "def register(registry, ctx):\n"
        "    registry.install(manifest('acme.sink', 'ACME 审计', kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER], provides=['event.observe/v1'], version='0.1.0', metadata={'category': 'governance.trace', 'vendor': 'ACME'}), Sink(), slot=SlotName.EVENT_OBSERVER)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_pkg", None)
    from staffdeck_harness.runtime import assembly

    assembly.start_harness_runtime(settings)

    # inspect before adding
    r = c.post("/api/enterprise/harness/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "acme_pkg:register"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["modules"][0]["module_id"] == "acme.sink" and body["modules"][0]["placement"] == {"big_id": "governance", "sub_id": "governance.trace", "source": "manifest"}
    assert body["modules"][0]["source"] == "acme_pkg:register" and body["modules"][0]["already_installed"] is False
    # acme.sink provides event.observe/v1, but EVENT_OBSERVER is a fan-out slot: every observer
    # receives events, so there is no "first wins" shadow — the dry-run must be warning-free here
    # (and must not fail the assembly).
    assert not any(w["code"] == "OPERATION_SHADOWED" for w in body["warnings"]), "observers fan out; adding one cannot shadow another"
    bad = c.post("/api/enterprise/harness/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "no_such_pkg:register"}, headers=headers).json()
    assert bad["ok"] is False and bad["errors"][0]["phase"] == "import"
    assert c.post("/api/enterprise/harness/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "not a spec!"}, headers=headers).json()["errors"][0]["code"] == "INVALID_SPEC"

    # placement for a not-yet-installed module is allowed; for K modules refused
    r = c.put("/api/enterprise/harness/modules/acme.sink/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.monitoring"}, headers=headers)
    assert r.status_code == 200 and r.json()["installed"] is False
    assert c.put("/api/enterprise/harness/modules/runtime.coordinator/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.trace"}, headers=headers).status_code == 400
    assert c.put("/api/enterprise/harness/modules/observer.feedback/placement", json={"tenant_id": "tenant_demo", "sub_id": "nope.sub"}, headers=headers).status_code == 400
    # moving a builtin A module takes effect immediately and is not pending
    r = c.put("/api/enterprise/harness/modules/observer.feedback/placement", json={"tenant_id": "tenant_demo", "sub_id": "governance.monitoring"}, headers=headers)
    assert r.status_code == 200 and r.json()["placement"]["source"] == "override"
    tree = r.json()["tree"]
    sub = next(s for b in tree for s in b["subs"] if s["id"] == "governance.monitoring")
    assert "observer.feedback" in [m["module_id"] for m in sub["modules"]]
    assert c.get("/api/enterprise/harness/config", params={"tenant_id": "tenant_demo"}, headers=headers).json()["pending"] is False
    # PUT /config placements with null removes
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "placements": {"observer.feedback": None}}, headers=headers)
    assert "observer.feedback" not in r.json()["saved"]["placements"]

    # load the package for real: add spec + restart → appears under its manifest category (override wins)
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "extra_modules": ["acme_pkg:register"]}, headers=headers)
    assert r.status_code == 200 and r.json()["pending"] is True
    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers)
    assert r.status_code == 200, r.text
    tree = c.get("/api/enterprise/harness/modules/tree", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    placed = {m["module_id"]: (s["id"], m["placement"]["source"], m["source"]) for b in tree for s in b["subs"] for m in s["modules"]}
    assert placed["acme.sink"] == ("governance.monitoring", "override", "acme_pkg:register")
    assert not any(b["id"] == "unplaced" for b in tree)
    # unknown module in disabled list is refused
    assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["does.not.exist"]}, headers=headers).status_code == 400
    # a platform-service module that is not switchable is refused, a switchable one accepted
    assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["ledger.invocation"]}, headers=headers).status_code == 400
    assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": ["sandbox.local"]}, headers=headers).status_code == 200
    c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "disabled_modules": [], "extra_modules": []}, headers=headers)
    assembly.stop_harness_runtime()


# --------------------------------------------------------------------------- authorization boundary

def _member_headers(db) -> dict[str, str]:
    """A same-tenant *member* (not admin) — the principal the review said could read too much."""

    if db.get(User, "member") is None:
        db.add(User(id="member", tenant_id="tenant_demo", username="bob", role="member", password_hash=hash_password("x")))
        db.commit()
    return {"Authorization": f"Bearer {create_access_token(db.get(User, 'member'))}"}


def _session(db, session_id: str, user_id: str):
    from app.db.models import ChatSession

    if db.get(ChatSession, session_id) is None:
        db.add(ChatSession(id=session_id, tenant_id="tenant_demo", user_id=user_id, agent_id="agent_1", title="t", status="active", channel="web"))
        db.commit()


def test_member_cannot_read_other_users_session_records(ctx) -> None:
    c, admin_headers, db = ctx
    member = _member_headers(db)
    _session(db, "sess_admin", "admin")
    db.add(HarnessInvocationRecord(id=new_id("hinvoke"), tenant_id="tenant_demo", session_id="sess_admin", task_id="t1", run_id="r1", call_id="c1", tool_name="tool:tool.invoke/v1", request_digest="d", status="completed", arguments_json={"secret": "value"}, started_at=utc_now()))
    db.commit()
    # admin sees everything, arguments included
    r = c.get("/api/enterprise/harness/ledger/recent", params={"tenant_id": "tenant_demo", "session_id": "sess_admin"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()[0]["arguments"] == {"secret": "value"}
    # a member is refused on a session they do not own
    for path in ("/api/enterprise/harness/ledger/recent", "/api/enterprise/harness/events/recent", "/api/enterprise/harness/log"):
        r = c.get(path, params={"tenant_id": "tenant_demo", "session_id": "sess_admin"}, headers=member)
        assert r.status_code == 403, (path, r.text)
    # and the tenant-wide session list is admin-only
    assert c.get("/api/enterprise/harness/sessions/recent", params={"tenant_id": "tenant_demo"}, headers=member).status_code == 403
    assert c.get("/api/enterprise/harness/sessions/recent", params={"tenant_id": "tenant_demo"}, headers=admin_headers).status_code == 200


def test_member_sees_own_session_with_arguments_redacted(ctx) -> None:
    c, _, db = ctx
    member = _member_headers(db)
    _session(db, "sess_member", "member")
    db.add(HarnessInvocationRecord(id=new_id("hinvoke"), tenant_id="tenant_demo", session_id="sess_member", task_id="t1", run_id="r1", call_id="c1", tool_name="tool:tool.invoke/v1", request_digest="d", status="completed", arguments_json={"order_id": "B1"}, started_at=utc_now(), finished_at=utc_now()))
    db.commit()
    r = c.get("/api/enterprise/harness/ledger/recent", params={"tenant_id": "tenant_demo", "session_id": "sess_member"}, headers=member)
    assert r.status_code == 200, r.text
    assert r.json()[0]["arguments"] == {"order_id": "<redacted>"}, "owner sees keys, never values"
    log = c.get("/api/enterprise/harness/log", params={"tenant_id": "tenant_demo", "session_id": "sess_member"}, headers=member)
    assert log.status_code == 200
    calls = [e for e in log.json()["entries"] if e["type"] == "tool/call"]
    assert calls and calls[0]["data"]["arguments"] == "<redacted>"


def test_global_mutators_require_operator_tenant(ctx, tmp_path, monkeypatch) -> None:
    """With ``harness_operator_tenants`` set, a tenant admin outside the list cannot change process-global state."""

    c, headers, db = ctx
    monkeypatch.setenv("STAFFDECK_HARNESS_RUNTIME_CONFIG", str(tmp_path / "rt.json"))
    monkeypatch.setenv("HARNESS_OPERATOR_TENANTS", "tenant_other")
    get_settings.cache_clear()
    try:
        assert get_settings().harness_operator_tenants == "tenant_other"
        r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers)
        assert r.status_code == 403, r.text
        assert c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers).status_code == 403
        assert c.post("/api/enterprise/harness/modules/inspect", json={"tenant_id": "tenant_demo", "spec": "x.y:register"}, headers=headers).status_code == 403
        # per-tenant/per-session administration is unaffected by the operator boundary
        assert c.put("/api/enterprise/harness/staff/agent_1/engine", json={"tenant_id": "tenant_demo", "engine": "default"}, headers=headers).status_code == 200
    finally:
        monkeypatch.delenv("HARNESS_OPERATOR_TENANTS", raising=False)
        get_settings.cache_clear()
    # unset (single-operator deployment): the acting admin's tenant may change the runtime.
    # (STAFFDECK_HARNESS_RUNTIME_CONFIG keeps the saved assembly in tmp_path, not the repo.)
    assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers).status_code == 200


def test_restart_reports_interrupted_turns_and_accepts_drain_timeout(ctx, tmp_path, monkeypatch) -> None:
    from staffdeck_harness.runtime import assembly

    c, headers, db = ctx
    settings = get_settings()
    monkeypatch.setattr(settings, "harness_runtime_config_path", str(tmp_path / "rt.json"), raising=False)
    assembly.start_harness_runtime(settings)
    r = c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo", "drain_timeout_seconds": 0}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json().get("interrupted_turns") == 0


def test_global_mutators_default_closed_when_process_hosts_several_tenants(ctx, tmp_path, monkeypatch) -> None:
    """With HARNESS_OPERATOR_TENANTS unset, a second tenant in the process closes the runtime to every admin."""

    c, headers, db = ctx
    monkeypatch.setenv("STAFFDECK_HARNESS_RUNTIME_CONFIG", str(tmp_path / "rt.json"))
    get_settings.cache_clear()
    assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers).status_code == 200, "single tenant: its admin is the operator"
    db.add(Tenant(id="tenant_other", name="Other"))
    db.commit()
    r = c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers)
    assert r.status_code == 403 and "HARNESS_OPERATOR_TENANTS" in r.text
    assert c.post("/api/enterprise/harness/restart", json={"tenant_id": "tenant_demo"}, headers=headers).status_code == 403
    # naming the operator tenant reopens it — for that tenant only
    monkeypatch.setenv("HARNESS_OPERATOR_TENANTS", "tenant_demo")
    get_settings.cache_clear()
    try:
        assert c.put("/api/enterprise/harness/config", json={"tenant_id": "tenant_demo", "engine": "harness_v3"}, headers=headers).status_code == 200
    finally:
        monkeypatch.delenv("HARNESS_OPERATOR_TENANTS", raising=False)
        get_settings.cache_clear()


def test_status_exposes_fallbacks(ctx) -> None:
    from staffdeck_harness.bridge import engine_host

    c, headers, _ = ctx
    engine_host.reset_fallback_state()
    engine_host._note_fallback(SimpleNamespace(events=None), SimpleNamespace(tenant_id="tenant_demo", session_id="s1", agent_id="agent_1"), "image_attachments", detail="带图片")
    body = c.get("/api/enterprise/harness/status", params={"tenant_id": "tenant_demo"}, headers=headers).json()
    assert body["fallback_count"] == 1 and body["last_fallback"]["reason"] == "image_attachments"
    engine_host.reset_fallback_state()
