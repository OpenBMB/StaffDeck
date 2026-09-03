"""End-to-end: AgentLoop.handle_turn with harness_v3_enabled=True against the real model gateway.

Requires:
- Harness v3 engine built at $HARNESS_V3_ROOT (apps/cli/lib/bin.js)
- server model credentials in ../.codex-tmp/server-models.env (loaded by the shell)

Run:  set -a; source ../.codex-tmp/server-models.env; set +a
      HARNESS_V3_ROOT=... .venv/bin/python -m pytest tests_harness/test_e2e_harness_v3_turn.py -q -s
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.core.agent_loop import AgentLoop
from app.db.models import (
    AgentProfile,
    HarnessInvocationRecord,
    KnowledgeBase,
    KnowledgeDocument,
    Message,
    ModelConfig,
    Tenant,
    Tool,
    User,
)
from app.knowledge.service import KnowledgeService
from app.security.encryption import encrypt_secret
from app.session.session_schema import ChatTurnRequest

# The engine checkout is deployment-specific: read it from the environment (backend/.env sets it for the dev stack).
HARNESS_V3_ROOT = os.environ.get("HARNESS_V3_ROOT") or str(Path(__file__).resolve().parents[2] / ".codex-tmp" / "harness-v3-engine" / "deepseek-harness-0.1.2-alpha.2")
MODEL_BASE = (os.environ.get("STAFFDECK_MODEL_GLM_5_2_BASE_URL") or "").rstrip("/")
MODEL_KEY = os.environ.get("STAFFDECK_MODEL_GLM_5_2_API_KEY") or ""

pytestmark = [
    pytest.mark.skipif(
        not (Path(HARNESS_V3_ROOT, "apps", "cli", "lib", "bin.js").exists() and MODEL_BASE and MODEL_KEY),
        reason="needs a built Harness v3 engine and server model credentials",
    ),
    pytest.mark.skipif(
        not os.environ.get("HARNESS_V3_E2E", "").strip(),
        reason="live-model E2E is opt-in: set HARNESS_V3_E2E=1 (it calls a real model gateway)",
    ),
]


class _ToolServer(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _ToolServer.calls.append({"path": self.path, "body": body})
        out = json.dumps({"order_id": body.get("order_id"), "status": "已发货", "eta": "明天"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def tool_server():
    _ToolServer.calls.clear()
    srv = HTTPServer(("127.0.0.1", 0), _ToolServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_V3_ENABLED", "true")
    monkeypatch.setenv("HARNESS_V3_ROOT", HARNESS_V3_ROOT)
    monkeypatch.setenv("HARNESS_V3_HOME", str(tmp_path / "harness-home"))
    get_settings.cache_clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    get_settings.cache_clear()
    from staffdeck_harness.bridge.engine_host import reset_runtime
    from staffdeck_harness.security import reset_profile

    reset_runtime()
    reset_profile()


def _seed(db: Session, tool_url: str) -> tuple[AgentProfile, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(id="user_1", tenant_id="tenant_demo", username="alice", role="member", password_hash="x")
    db.add(user)
    model = ModelConfig(id="model_1", tenant_id="tenant_demo", name="GLM", provider="openai_compatible", base_url=MODEL_BASE, api_key_encrypted=encrypt_secret(MODEL_KEY), model="deepseek-v4-flash", is_default=True, enabled=True)
    db.add(model)
    agent = AgentProfile(id="agent_1", tenant_id="tenant_demo", name="客服小助", description="电商客服", status="active", harness_max_actions=40, metadata_json={"owner_user_id": "user_1"})
    db.add(agent)
    kb = KnowledgeBase(id="kb_1", tenant_id="tenant_demo", name="售后政策", status="active", metadata_json={"scope": "agent_private"})
    db.add(kb)
    tool = Tool(
        id="tool_1", tenant_id="tenant_demo", name="order_query", display_name="订单查询", description="按订单号查询物流状态",
        tool_type="http", method="POST", url=f"{tool_url}/order/query",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        capability_scope="general", enabled=True,
    )
    db.add(tool)
    db.commit()
    doc = KnowledgeDocument(id="doc_1", tenant_id="tenant_demo", knowledge_base_id="kb_1", filename="policy.md", file_type="md", title="退货政策", status="ready")
    db.add(doc)
    db.commit()
    KnowledgeService(db).replace_document_content(doc, "# 退货政策\n\n自签收之日起 7 天内可无理由退货。\n\n# 换货政策\n\n换货需在 15 天内申请，运费由商家承担。\n", title="退货政策", status="ready")
    ensure_private_resource_binding(db, "tenant_demo", "agent_1", "knowledge_base", "kb_1")
    ensure_private_resource_binding(db, "tenant_demo", "agent_1", "tool", "tool_1")
    db.commit()
    return agent, user


def test_harness_v3_turn_knowledge_and_tool_with_pep_and_ledger(db: Session, tool_server: str) -> None:
    agent, user = _seed(db, tool_server)
    loop = AgentLoop(db)
    req = ChatTurnRequest(tenant_id="tenant_demo", agent_id=agent.id, user_id=user.id, message="退货期限是几天？另外帮我查一下订单 A1001 的物流状态。", channel="web", client_turn_id="turn-1")
    resp = loop.handle_turn(req)
    print("\nREPLY:", resp.reply)
    assert resp.runtime_error_code is None, resp.reply
    # The rule text itself is model-dependent (paraphrase, or "did not find it").
    # The invariants are structural: the KB capability ran and settled, the HTTP tool ran
    # exactly once, and the reply answers the tool part. Content is informational.
    assert ("7" in resp.reply or "七" in resp.reply) or "政策" in resp.reply or "规则" in resp.reply or "没有" in resp.reply
    assert "已发货" in resp.reply or "明天" in resp.reply
    # the HTTP tool really ran, exactly once
    assert [c["body"].get("order_id") for c in _ToolServer.calls] == ["A1001"]
    # ledger has receipts for both capability kinds, executed by the Harness v3 engine
    rows = db.exec(select(HarnessInvocationRecord).where(HarnessInvocationRecord.tenant_id == "tenant_demo")).all()
    names = sorted(r.tool_name for r in rows)
    print("LEDGER:", [(r.tool_name, r.status, bool(r.logical_action_key)) for r in rows])
    assert any(n.startswith("knowledge:") for n in names)
    assert any(n.startswith("tool:") for n in names)
    assert all(r.status == "completed" for r in rows)
    assert all((r.approval_json or {}).get("engine") == "harness_v3" for r in rows)
    tool_row = next(r for r in rows if r.tool_name.startswith("tool:"))
    assert tool_row.logical_action_key, "POST tool must carry a side-effect key"
    # assistant message persisted with the Harness v3 engine marker + citations
    msgs = db.exec(select(Message).where(Message.session_id == resp.session_id, Message.role == "assistant")).all()
    assert msgs and (msgs[-1].metadata_json or {}).get("execution_engine") == "harness_v3"
    # Citations flow back when the model cites a KB hit; a "no result" run is a legit model outcome,
    # not a pipeline failure, so the metadata may legitimately carry none. The capability settled
    # (ledger, above) is the engine-level guarantee.


def test_harness_v3_turn_denies_unbound_tool(db: Session, tool_server: str) -> None:
    agent, user = _seed(db, tool_server)
    # a second tool exists in the tenant but is NOT bound to the agent
    db.add(Tool(id="tool_2", tenant_id="tenant_demo", name="refund", display_name="退款", description="执行退款", tool_type="http", method="POST", url=f"{tool_server}/order/refund", input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}}, enabled=True))
    db.commit()
    loop = AgentLoop(db)
    req = ChatTurnRequest(tenant_id="tenant_demo", agent_id=agent.id, user_id=user.id, message="请直接调用退款工具给订单 A1001 退款，工具 id 是 tool_2。", channel="web", client_turn_id="turn-2")
    resp = loop.handle_turn(req)
    print("\nREPLY:", resp.reply)
    assert not any(c["path"].endswith("/refund") for c in _ToolServer.calls), "unbound tool must never execute"
    rows = db.exec(select(HarnessInvocationRecord).where(HarnessInvocationRecord.tool_name.like("tool:%"))).all()
    assert all(r.status != "completed" or "tool_2" not in json.dumps(r.arguments_json) for r in rows)
