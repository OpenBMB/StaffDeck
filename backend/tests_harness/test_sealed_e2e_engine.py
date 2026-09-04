"""Sealed end-to-end: the real Node engine, the real MCP round-trip, a scripted fake model.

The unit suite never starts the engine, so wiring mistakes between the bridge and the engine
(``dsh_home`` vs ``DSH_HOME``, launch args, the MCP header, tool-call chunk shapes) were only
caught by hand on a live stack. This test boots the actual engine subprocess from
``HARNESS_V3_ROOT`` and drives a complete turn — plan → step with a real ``knowledge_search``
MCP call → ``finish_task`` — while the *model* is a local OpenAI-compatible fake that answers
from a script. No provider credentials, no network, deterministic.

It runs whenever the engine checkout is present (CI builds it); it is skipped, not failed,
when it is not, and `HARNESS_V3_SEALED_E2E=0` disables it explicitly.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_private_resource_binding
from app.config import get_settings
from app.core.agent_loop import AgentLoop
from app.db.models import AgentProfile, HarnessInvocationRecord, KnowledgeBase, KnowledgeDocument, Message, ModelConfig, Tenant, User
from app.knowledge.service import KnowledgeService
from app.security.encryption import encrypt_secret
from app.session.session_schema import ChatTurnRequest

HARNESS_V3_ROOT = os.environ.get("HARNESS_V3_ROOT") or str(Path(__file__).resolve().parents[2] / ".codex-tmp" / "harness-v3-engine" / "deepseek-harness-0.1.2-alpha.2")

pytestmark = [
    pytest.mark.skipif(not Path(HARNESS_V3_ROOT, "apps", "cli", "lib", "bin.js").exists(), reason="Harness v3 engine checkout not built at HARNESS_V3_ROOT"),
    pytest.mark.skipif(os.environ.get("HARNESS_V3_SEALED_E2E", "1").strip() == "0", reason="disabled via HARNESS_V3_SEALED_E2E=0"),
]


# --------------------------------------------------------------------------- scripted model

def _sse(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _chunk(cid: str, delta: dict[str, Any], finish: str | None = None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk", "created": 1, "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


class ScriptedModel:
    """Decides each reply from the *request*: this is the model's whole brain for the test.

    - plan phase (no tool result yet, stage input says TurnPlanner) → a conversation TaskFrame
    - step phase, first call → knowledge_search tool call
    - step phase, after a tool result → finish_task with a reply quoting [1]
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def reply(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        self.requests.append(body)
        text = json.dumps(body.get("messages") or [], ensure_ascii=False)
        cid = f"chatcmpl-{len(self.requests)}"
        if '"phase": "TurnPlanner"' in text or "TurnPlanner" in text and "output_contract" in text:
            plan = {"decision": "answer_only", "confidence": 0.9, "user_intent": "询问退货期限", "reason": "咨询，无匹配 SOP", "task_frames": [{"kind": "conversation", "decision": "answer_only", "user_intent": "询问退货期限", "requirements": ["回答退货期限"]}], "task_updates": []}
            return [_chunk(cid, {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}), _chunk(cid, {}, "stop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})]
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages") or [])
        tools = {t["function"]["name"] for t in body.get("tools") or [] if isinstance(t, dict) and t.get("function")}
        if not has_tool_result and "mcp__staffdeck__knowledge_search" in tools:
            args = json.dumps({"query": "退货期限"}, ensure_ascii=False)
            return [
                _chunk(cid, {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_kb", "type": "function", "function": {"name": "mcp__staffdeck__knowledge_search", "arguments": ""}}]}),
                _chunk(cid, {"tool_calls": [{"index": 0, "id": None, "function": {"name": None, "arguments": args}}]}),  # gateway-style null continuation
                _chunk(cid, {}, "tool_calls", {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}),
            ]
        if "mcp__staffdeck__finish_task" in tools:
            args = json.dumps({"status": "completed", "reply_fragment": "自签收之日起 7 天内可无理由退货 [1]。", "task_summary": "回答了退货期限"}, ensure_ascii=False)
            return [
                _chunk(cid, {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fin", "type": "function", "function": {"name": "mcp__staffdeck__finish_task", "arguments": args}}]}),
                _chunk(cid, {}, "tool_calls", {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39}),
            ]
        return [_chunk(cid, {"role": "assistant", "content": "好的。"}), _chunk(cid, {}, "stop")]


class _ModelHandler(BaseHTTPRequestHandler):
    model: ScriptedModel

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        chunks = self.model.reply(body)
        if body.get("stream", False):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(_sse(c))
            self.wfile.write(b"data: [DONE]\n\n")
            return
        # non-stream: fold the chunks into one completion
        content, tool_calls, finish = "", [], "stop"
        for c in chunks:
            d = c["choices"][0]["delta"]
            content += d.get("content") or ""
            for tc in d.get("tool_calls") or []:
                if tc.get("id"):
                    tool_calls.append({"id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or ""}})
                elif tool_calls:
                    tool_calls[-1]["function"]["arguments"] += tc["function"].get("arguments") or ""
            finish = c["choices"][0]["finish_reason"] or finish
        msg: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out = json.dumps({"id": "cmpl", "object": "chat.completion", "model": "fake", "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def fake_model():
    model = ScriptedModel()
    _ModelHandler.model = model
    srv = HTTPServer(("127.0.0.1", 0), _ModelHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield model, f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_V3_ENABLED", "true")
    monkeypatch.setenv("HARNESS_V3_ROOT", HARNESS_V3_ROOT)
    monkeypatch.setenv("HARNESS_V3_HOME", str(tmp_path / "harness-home"))
    monkeypatch.setenv("HARNESS_V3_FALLBACK_TO_V2", "false")  # a broken engine must FAIL this test, not hide behind v2
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


def _seed(db: Session, model_base: str) -> tuple[AgentProfile, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(id="user_1", tenant_id="tenant_demo", username="alice", role="member", password_hash="x")
    db.add(user)
    db.add(ModelConfig(id="model_1", tenant_id="tenant_demo", name="Fake", provider="openai_compatible", base_url=model_base, api_key_encrypted=encrypt_secret("fake-key"), model="fake", is_default=True, enabled=True))
    agent = AgentProfile(id="agent_1", tenant_id="tenant_demo", name="客服小助", description="电商客服", status="active", harness_max_actions=10, metadata_json={"owner_user_id": "user_1"})
    db.add(agent)
    db.add(KnowledgeBase(id="kb_1", tenant_id="tenant_demo", name="售后政策", status="active", metadata_json={"scope": "agent_private"}))
    db.commit()
    doc = KnowledgeDocument(id="doc_1", tenant_id="tenant_demo", knowledge_base_id="kb_1", filename="policy.md", file_type="md", title="退货政策", status="ready")
    db.add(doc)
    db.commit()
    KnowledgeService(db).replace_document_content(doc, "# 退货政策\n\n自签收之日起 7 天内可无理由退货。\n", title="退货政策", status="ready")
    ensure_private_resource_binding(db, "tenant_demo", "agent_1", "knowledge_base", "kb_1")
    db.commit()
    return agent, user


def test_sealed_turn_on_real_engine_with_scripted_model(db: Session, fake_model) -> None:
    model, base = fake_model
    agent, user = _seed(db, base)
    loop = AgentLoop(db)
    req = ChatTurnRequest(tenant_id="tenant_demo", agent_id=agent.id, user_id=user.id, message="退货期限是几天？", channel="web", client_turn_id="turn-1")
    resp = loop.handle_turn(req)
    assert resp.runtime_error_code is None, resp.reply
    assert "7" in resp.reply and "[1]" in resp.reply, resp.reply

    # Every model call went through the bridge's gateway to the fake (the API key it saw is the
    # activation token, never the ModelConfig key) and both phases ran on the engine.
    ops = [r for r in model.requests]
    assert len(ops) >= 3, f"expected plan + step + finish calls, got {len(ops)}"
    assert all("fake-key" not in json.dumps(r) for r in ops)

    # The capability really went through MCP → CapabilityHost → ledger.
    rows = db.exec(select(HarnessInvocationRecord).where(HarnessInvocationRecord.tenant_id == "tenant_demo")).all()
    assert [r.tool_name for r in rows] == ["knowledge:knowledge.search/v1"] or any(r.tool_name.startswith("knowledge") for r in rows)
    assert all(r.status == "completed" for r in rows)
    assert all((r.approval_json or {}).get("engine") == "harness_v3" for r in rows)

    # Persisted assistant message carries the engine marker and the citation that flowed back.
    msgs = db.exec(select(Message).where(Message.session_id == resp.session_id, Message.role == "assistant")).all()
    md = msgs[-1].metadata_json or {}
    assert md.get("execution_engine") == "harness_v3"
    assert md.get("knowledge_citations"), "citations must flow back from the host"
