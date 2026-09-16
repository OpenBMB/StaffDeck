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
        if any(m.get("role") == "tool" and m.get("tool_call_id") == "call_fin" for m in body.get("messages") or []):
            return [_chunk(cid, {"role": "assistant", "content": "已完成。"}), _chunk(cid, {}, "stop")]
        if not has_tool_result and "mcp__staffdeck__knowledge_search" in tools:
            args = json.dumps({"query": "退货期限"}, ensure_ascii=False)
            return [
                _chunk(cid, {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_kb", "type": "function", "function": {"name": "mcp__staffdeck__knowledge_search", "arguments": ""}}]}),
                _chunk(cid, {"tool_calls": [{"index": 0, "id": None, "function": {"name": getattr(self, 'continuation_name', None), "arguments": args}}]}),
                _chunk(cid, {}, "tool_calls", {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}),
            ]
        if has_tool_result:
            return [_chunk(cid, {"role": "assistant", "content": "自签收之日起 7 天内可无理由退货 [1]。"}), _chunk(cid, {}, "stop")]
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
    from staffdeck_harness.modules.registry import reset_registry
    reset_registry()
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_V3_ENABLED", "true")
    monkeypatch.setenv("HARNESS_V3_ROOT", HARNESS_V3_ROOT)
    monkeypatch.setenv("HARNESS_V3_HOME", str(tmp_path / "harness-home"))
    monkeypatch.setenv("HARNESS_V3_FALLBACK_TO_V2", "false")  # a broken engine must FAIL this test, not hide behind v2
    get_settings.cache_clear()
    # Real MCP/model callbacks run on different threads: use production-like independent
    # connections, not a StaticPool sharing one SQLite connection across all callbacks.
    engine = create_engine(f"sqlite:///{tmp_path / 'sealed.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    get_settings.cache_clear()
    from staffdeck_harness.bridge.engine_host import reset_runtime
    from staffdeck_harness.security import reset_profile

    reset_runtime()
    reset_profile()
    reset_registry()


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


def test_unknown_engine_tools_are_visible_and_stop_after_three_attempts(db, fake_model, monkeypatch):
    from app.db.models import AgentEvent
    model, base = fake_model
    original = model.reply
    attempted = []
    def reply(body):
        if not body.get('tools'):
            return original(body)
        attempted.append(True)
        return [_chunk('unknown', {'tool_calls': [{'index': 0, 'id': f'unknown-{len(attempted)}',
            'type': 'function', 'function': {'name': 'nonexistent_function', 'arguments': '{}'}}]}),
            _chunk('unknown', {}, 'tool_calls')]
    monkeypatch.setattr(model, 'reply', reply)
    agent, user = _seed(db, base)
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id='tenant_demo', agent_id=agent.id,
        user_id=user.id, message='退货期限是几天？', channel='web', client_turn_id='unknown-tool-test'))
    rows = db.exec(select(AgentEvent).where(AgentEvent.session_id == response.session_id)).all()
    frames = [row.payload_json for row in rows if row.event_type == 'task_frame_finished']
    failures = [row.payload_json for row in rows if row.event_type == 'harness_tool_result']
    assert len(attempted) == 3
    assert len(failures) == 3 and all(row['error']['code'] == 'UNKNOWN_TOOL' for row in failures)
    assert frames[-1]['action_count'] == 3
    assert frames[-1]['status'] != 'completed'


@pytest.mark.parametrize('continuation_name', [None, ''])
def test_sealed_turn_on_real_engine_with_scripted_model(db: Session, fake_model, continuation_name) -> None:
    model, base = fake_model
    model.continuation_name = continuation_name
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
    assert all(not any("submit_step_result" in tool["function"]["name"] or "finish_task" in tool["function"]["name"] for tool in request.get("tools", [])) for request in ops)

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


@pytest.mark.parametrize("channel", ["wechat", "feishu"])
def test_real_engine_guest_turn_does_not_require_web_identity_for_memory(db, fake_model, monkeypatch, channel):
    from staffdeck_harness.composition.local_sources import LocalIdentitySource
    from staffdeck_harness.security.oss_local import LocalIdentity
    from staffdeck_harness.contracts.security import SecurityContext
    from staffdeck_harness.contracts.runtime_services import ChannelExecutionScope
    from app.memory.service import MemoryService

    model, base = fake_model
    agent, user = _seed(db, base)
    user.source = channel
    db.add(user)
    db.commit()
    contexts = []
    def resolve(self, context, identity):
        assert (context.tenant_id, context.user_id, context.staff_id) == (user.tenant_id, user.id, agent.id)
        assert context.session_id and context.channel == channel
        contexts.append(context)
        scope = ChannelExecutionScope(user.tenant_id, user.id, agent.id, context.session_id,
            "test-binding", 1, "test-account", "test-event", "test-namespace")
        return SecurityContext(user.id, user.tenant_id, provider="channel_guest", actor_user_id=user.id,
            agent_id=agent.id, session_id=context.session_id, attributes={"channel_scope": scope})
    monkeypatch.setattr(LocalIdentitySource, "resolve", resolve)
    monkeypatch.setattr(LocalIdentity, "from_user", lambda *a, **kw: pytest.fail("guest became a web user"))
    monkeypatch.setattr(MemoryService, "context_memories", lambda *a, **kw: pytest.fail("guest read personal memory"))
    monkeypatch.setattr(MemoryService, "capture_turn", lambda *a, **kw: pytest.fail("guest captured personal memory"))
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
        user_id=user.id, message="退货期限是几天？", channel=channel, client_turn_id="guest-memory-turn"))
    assert response.runtime_error_code is None, response.reply
    assert "7" in response.reply and "[1]" in response.reply
    assert len(model.requests) >= 3 and len(contexts) >= 3


def test_real_engine_unreadable_history_is_storage_error_not_model_error(db, fake_model):
    import os
    from pathlib import Path
    from app.db.models import AgentEvent
    if os.geteuid() == 0:
        pytest.skip("OS permission regression must run as an unprivileged service user")
    model, base = fake_model
    agent, user = _seed(db, base)
    blocked = Path(get_settings().harness_v3_home) / user.tenant_id / "sessions" / "unreadable-history"
    blocked.mkdir(parents=True)
    blocked.chmod(0)
    try:
        response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
            user_id=user.id, message="退货期限是几天？", client_turn_id="storage-permission-test"))
        assert response.runtime_error_code == "ENGINE_STORAGE_PERMISSION_DENIED", response.reply
        assert "MODEL_UPSTREAM_UNAVAILABLE" not in response.reply and "检查模型配置" not in response.reply
        assert str(blocked) not in response.reply
        assert model.requests == [], "local persistence failed before any model request"
        errors = db.exec(select(AgentEvent).where(AgentEvent.session_id == response.session_id,
            AgentEvent.event_type == "error_occurred")).all()
        assert errors[-1].payload_json["code"] == "ENGINE_STORAGE_PERMISSION_DENIED"
    finally:
        blocked.chmod(0o700)
    from staffdeck_harness.bridge.engine_host import reset_runtime
    reset_runtime()
    recovered = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
        user_id=user.id, message="退货期限是几天？", client_turn_id="storage-permission-recovered"))
    assert recovered.runtime_error_code is None, recovered.reply
    assert "7" in recovered.reply and model.requests


@pytest.mark.parametrize("detached", [False, True])
def test_real_engine_repairs_bad_tool_json_without_replaying_business_action(db, fake_model, monkeypatch, detached):
    from app.db.models import Tool, ExternalBusinessTask
    from app.tools.tool_schema import ToolResult
    from app.tools.external_tasks import poll_due_external_tasks
    model, base = fake_model
    agent, user = _seed(db, base)
    tool = Tool(id="json-tool", tenant_id=user.tenant_id, name="json_submit", tool_type="http",
        method="POST", url="http://not-contacted.invalid/submit", enabled=True,
        input_schema={"type":"object", "properties":{"value":{"type":"string"}}, "required":["value"]},
        config_json={"execution":{"timeout_seconds":10, "execution_mode":"detached" if detached else "sync"}})
    db.add(tool)
    ensure_private_resource_binding(db, user.tenant_id, agent.id, "tool", tool.id)
    db.commit()
    sends = []
    def execute(self, row, arguments, **kwargs):
        sends.append(dict(arguments))
        return ToolResult(tool_name=row.name, success=True, data={"submitted":True})
    monkeypatch.setattr("app.tools.tool_executor.ToolExecutor.execute_sync_http", execute)
    original = model.reply
    phase_calls = []
    def scripted(body):
        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        if "TurnPlanner" in text and "output_contract" in text:
            return original(body)
        phase_calls.append(body)
        cid = f"json-{len(phase_calls)}"
        repairing = "上一条工具请求因参数 JSON/schema 无效" in text
        submitted = any(m.get("role") == "tool" and m.get("tool_call_id") == "fixed-call" for m in body.get("messages", []))
        if submitted:
            return [_chunk(cid, {"role":"assistant", "content":"提交成功。"}), _chunk(cid, {}, "stop")]
        if len(phase_calls) == 1 or repairing:
            args = json.dumps({"tool_id":tool.id,"arguments":{"value":"once"}}) if repairing else '{"tool_id":'
            return [_chunk(cid, {"role":"assistant","tool_calls":[{"index":0,"id":"fixed-call" if repairing else "bad-call",
                "type":"function","function":{"name":"mcp__staffdeck__tool_invoke","arguments":args}}]}), _chunk(cid, {}, "tool_calls")]
        return [_chunk(cid, {"role":"assistant", "content":"参数错误，无法提交。"}), _chunk(cid, {}, "stop")]
    model.reply = scripted
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
        user_id=user.id, message="请提交 once", client_turn_id="json-repair"))
    assert response.runtime_error_code is None, response.reply
    assert any("上一条工具请求因参数 JSON/schema 无效" in json.dumps(r, ensure_ascii=False) for r in phase_calls)
    tasks = db.exec(select(ExternalBusinessTask)).all()
    if detached:
        assert len(tasks) == 1 and tasks[0].status == "queued"
        assert sends == [], "foreground must not submit HTTP"
        poll_due_external_tasks(db)
        db.refresh(tasks[0])
        assert tasks[0].status == "completed", tasks[0].error_json
    else:
        assert tasks == []
    assert sends == [{"value":"once"}]


def test_async_sop_waits_then_resumes_same_checkpoint_without_resubmission(db, fake_model, monkeypatch):
    from app.db.models import Tool, Skill, ExternalBusinessTask, HarnessTaskFrameRecord, HarnessAgentLoopRecord
    from app.tools.tool_schema import ToolResult
    from app.tools.external_tasks import poll_due_external_tasks
    from app.session.session_schema import TurnPlan, PlannedTaskFrame
    from staffdeck_harness.runtime.model_phases import EngineTurnPlanner
    from staffdeck_harness.bridge.engine_host import reset_runtime
    model, base = fake_model
    agent, user = _seed(db, base)
    tool = Tool(id="async-sop-tool", tenant_id=user.tenant_id, name="async_sop_tool", method="POST",
        url="http://not-contacted.invalid", tool_type="http", enabled=True,
        config_json={"execution":{"timeout_seconds":10,"execution_mode":"detached"}})
    sop = Skill(id="async-sop", tenant_id=user.tenant_id, skill_id="async-flow", name="Async flow", status="published",
        content_json={"start_node_id":"invoke", "terminal_node_ids":["invoke"], "nodes":[
            {"node_id":"invoke", "name":"Submit", "allowed_actions":["call_tool:async_sop_tool"],
             "capability_refs":{"tool_ids":[tool.id],"required_tool_ids":[tool.id]}}], "edges":[]})
    db.add_all([tool,sop])
    db.commit()
    for kind, identifier in (("tool", tool.id), ("skill", sop.id)):
        ensure_private_resource_binding(db,user.tenant_id,agent.id,kind,identifier)
    db.commit()
    sends = []
    monkeypatch.setattr("app.tools.tool_executor.ToolExecutor.execute_sync_http",
        lambda self, row, args: sends.append(1) or ToolResult(tool_name=row.name, success=True,
            data={"marker":"ASYNC-COMPLETE-NONCE"}))
    def plan(self, message, session, *args, **kwargs):
        decision = "continue_active" if session.active_skill_id else "start_new_task"
        return TurnPlan(decision=decision, task_frames=[PlannedTaskFrame(task_id="async-frame", kind="sop",
            decision=decision, target_skill_id=sop.skill_id, target_step_id="invoke")])
    monkeypatch.setattr(EngineTurnPlanner, "plan", plan)
    def reply(body):
        model.requests.append(body)
        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        done = "ASYNC-COMPLETE-NONCE" in text
        name = "submit_step_result" if done else "tool_invoke"
        args = {"status":"completed", "reply_fragment":"ASYNC-COMPLETE-NONCE 已完成"} if done else {"tool_id":tool.id,"arguments":{}}
        if any(m.get("role")=="tool" and m.get("tool_call_id")=="finish-async" for m in body.get("messages", [])):
            return [_chunk("end", {"content":"ASYNC-COMPLETE-NONCE 已完成"}), _chunk("end", {}, "stop")]
        return [_chunk("async", {"tool_calls":[{"index":0,"id":"finish-async" if done else "submit-async","type":"function",
            "function":{"name":"mcp__staffdeck__"+name,"arguments":json.dumps(args)}}]}), _chunk("async", {}, "tool_calls")]
    model.reply = reply
    def turn(index, sid=None):
        return AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
            user_id=user.id, session_id=sid, message="继续", client_turn_id=f"async-turn-{index}"))
    first = turn(1)
    assert first.runtime_error_code is None, first.reply
    frame = db.exec(select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.session_id==first.session_id)).one()
    task = db.exec(select(ExternalBusinessTask)).one()
    logical = db.get(HarnessAgentLoopRecord,frame.agent_loop_id)
    assert frame.status == "waiting_external_task" and logical.status == "suspended"
    assert task.id in json.dumps(logical.checkpoint_json) and sends == []
    second = turn(2, first.session_id)
    db.refresh(frame)
    assert second.runtime_error_code is None and frame.status == "waiting_external_task"
    assert len(db.exec(select(ExternalBusinessTask)).all()) == 1 and sends == []
    poll_due_external_tasks(db)
    db.refresh(frame)
    db.refresh(task)
    assert task.status == "completed", task.error_json
    assert frame.status == "ready_to_resume" and frame.step_id == "invoke"
    reset_runtime()
    third = turn(3, first.session_id)
    db.refresh(frame)
    assert third.runtime_error_code is None, third.reply
    assert frame.status == "completed" and "ASYNC-COMPLETE-NONCE" in third.reply
    assert sends == [1]


def test_plan_failure_keeps_trace_reports_failure_and_does_not_hold_writer(db, fake_model, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import AgentEvent
    from staffdeck_harness.bridge.model_gateway import ModelGateway
    model, base = fake_model
    agent, user = _seed(db, base)
    concurrent_writes = []
    class FailingDriver:
        def stream(self, wire):
            # A separate worker must be able to write while a plan waits on its model.
            with Session(db.get_bind()) as other:
                other.connection().exec_driver_sql("PRAGMA busy_timeout=100")
                other.add(AgentEvent(tenant_id=user.tenant_id, session_id="probe",
                    event_type="regression_concurrent_write", payload_json={}))
                other.commit()
                concurrent_writes.append(True)
            raise ConnectionError("synthetic provider connection refused")
    monkeypatch.setattr(ModelGateway, "_default_client", staticmethod(lambda config: SimpleNamespace(
        model="fake", base_url=base, driver=FailingDriver(), api_protocol="openai_chat_completions",
        temperature=0.1, thinking_mode="disabled")))
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
        user_id=user.id, message="connection failure regression", client_turn_id="failure-probe"))
    assert concurrent_writes
    assert response.runtime_error_code == "LLM_ERROR"
    events = db.exec(select(AgentEvent).where(AgentEvent.session_id == response.session_id)).all()
    assert any(e.event_type == "llm_call_failed" for e in events)
    assert any(e.event_type == "error_occurred" for e in events)


def test_real_engine_uses_external_staff_and_catalog_without_local_resource_rows(db, fake_model, monkeypatch):
    """Load the replacement through registration, then traverse the complete AgentLoop path."""
    import sys
    from types import ModuleType, SimpleNamespace
    from staffdeck_harness.contracts.staff import StaffComposition, SessionPolicy, CapabilityBindingView
    from staffdeck_harness.contracts.sources import ResourceDescriptor
    from staffdeck_harness.contracts.security import ResourceRef
    from staffdeck_harness.contracts.invocation import ModuleResult
    from staffdeck_harness.contracts.manifest import SlotName, ModuleKind
    from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, install_registry, manifest
    from staffdeck_harness.composition import staff as local_staff
    from staffdeck_harness.composition.local_sources import LocalResourceCatalog

    model, base = fake_model
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(id="user_1", tenant_id="tenant_demo", username="alice", role="member", password_hash="x")
    db.add(user)
    db.add(ModelConfig(id="model_1", tenant_id="tenant_demo", name="Fake", provider="openai_compatible",
        base_url=base, api_key_encrypted=encrypt_secret("fake-key"), model="fake", is_default=True, enabled=True))
    db.commit()
    ref = ResourceRef("agent", "remote-agent", "tenant_demo", {"owner_user_id": user.id})
    kb_ref = ResourceRef("knowledge_base", "remote-kb", "tenant_demo",
                         {"binding_status": "active", "private_to_agent": True})
    composition = StaffComposition("tenant_demo", "remote-agent", "Remote staff", False, "active", "客服",
        {"default": "model_1"}, SessionPolicy(),
        (CapabilityBindingView("knowledge_base", "remote-kb", "install-kb", kb_ref, "售后政策",
            metadata={"provider_module_id": "sealed.knowledge", "resource_digest": "kb-v1"}),),
        (), (), None, (), ref)
    calls = []

    class StaffSource:
        def reference(self, context):
            return ref

        def resolve(self, context):
            return composition

        def model(self, context, model_id=None, role="default"):
            from app.llm.model_config_resolver import resolve_model_config_for_runtime
            return resolve_model_config_for_runtime(db, context.tenant_id, "model_1")

    class Catalog:
        def resolve(self, context, resource_type, resource_id, operation):
            assert (resource_type, resource_id) == ("knowledge_base", "remote-kb")
            return ResourceDescriptor(kb_ref, "售后政策", operation, digest="kb-v1")

    class Knowledge:
        def invoke(self, context, inv):
            calls.append(inv.context.agent_id)
            return ModuleResult.ok({"text": "自签收之日起 7 天内可无理由退货。"})

    plugin = ModuleType("sealed_sources_plugin")
    def register(registry, ctx):
        for name, slot, adapter in (
            ("sealed.staff", SlotName.STAFF_SOURCE, StaffSource()),
            ("sealed.sop_source", SlotName.SOP_SOURCE, SimpleNamespace(resolve=lambda c, s: (), reference=lambda *a: None)),
            ("sealed.catalog", SlotName.RESOURCE_CATALOG, Catalog()),
        ):
            registry.install(manifest(name, name, kind=ModuleKind.TRUSTED, slots=[slot]),
                SimpleNamespace(build=lambda services, p=adapter: p), slot=slot)
        registry.install(manifest("sealed.knowledge", "Remote knowledge", kind=ModuleKind.CODE,
            slots=[SlotName.STAFF_CAPABILITY], provides=["knowledge.search/v1"],
            policy_actions=["knowledge.search/v1"], metadata={"catalog_module_id": "sealed.catalog"}),
            Knowledge(), slot=SlotName.STAFF_CAPABILITY)
    plugin.register = register
    monkeypatch.setitem(sys.modules, "sealed_sources_plugin", plugin)
    monkeypatch.setenv("HARNESS_MODULES", "sealed_sources_plugin:register")
    monkeypatch.setenv("HARNESS_DISABLED_MODULES", "source.staff.local,source.sop.local,knowledge.local,team.provider")
    get_settings.cache_clear()
    reg = discover_and_install(ModuleRegistry(), get_settings())
    reg.seal()
    install_registry(reg)
    def forbidden(*args, **kwargs):
        pytest.fail("replacement path read local resource implementation")
    monkeypatch.setattr(local_staff, "project_staff", forbidden)
    monkeypatch.setattr(LocalResourceCatalog, "resolve", forbidden)
    from app.api.chat import chat_turn
    response = chat_turn(ChatTurnRequest(tenant_id="tenant_demo", agent_id="remote-agent",
        user_id=user.id, message="退货期限是几天？", channel="web", client_turn_id="external-turn"),
        current_user=user, db=db)
    assert response.runtime_error_code is None, response.reply
    assert "7" in response.reply and calls == ["remote-agent"]
    assert len(model.requests) >= 3
    assert db.exec(select(AgentProfile)).all() == []
    assert db.exec(select(KnowledgeBase)).all() == []


@pytest.mark.parametrize("native", [False, True])
def test_web_sse_entry_projects_only_public_reply_before_completion(db, fake_model, monkeypatch, native):
    """Exercise chat_stream's actual background worker and persisted SSE relay, not a sink stub."""
    import asyncio
    from app.api import chat
    from app.db.models import AgentEvent

    model, base = fake_model
    agent, user = _seed(db, base)
    if native:
        original = model.reply

        def reply(body):
            if "TurnPlanner" in json.dumps(body.get("messages"), ensure_ascii=False):
                return original(body)
            model.requests.append(body)
            text = json.dumps({"action": "finish", "status": "awaiting_user", "reply_fragment": "请补充订单日期。", "slot_updates": {}}, ensure_ascii=False)
            return [_chunk("native", {"content": c}) for c in text] + [_chunk("native", {}, "stop")]

        model.reply = reply
    monkeypatch.setattr(chat, "engine", db.get_bind())
    monkeypatch.setattr(chat, "_schedule_session_title_summary", lambda *a, **k: None)
    response = chat.chat_stream(ChatTurnRequest(
        tenant_id="tenant_demo", agent_id=agent.id, user_id=user.id,
        message="退货期限是几天？", channel="web", client_turn_id="web-stream-regression",
    ), current_user=user, db=db)

    async def consume():
        return [part async for part in response.body_iterator]

    chunks = asyncio.run(consume())
    frames = []
    for chunk in chunks:
        block = chunk.decode() if isinstance(chunk, bytes) else chunk
        lines = block.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), "")
        data = next((line[6:] for line in lines if line.startswith("data: ")), "{}")
        frames.append((event, json.loads(data)))
    final = next(data for event, data in frames if event == "complete")
    public = ""
    for event, data in frames:
        if event == "stream_delta":
            public += data["content"]
        elif event == "stream_replace":
            public = data["content"]
        assert '"decision"' not in public and '"action"' not in public
    assert public == final["reply"]
    assert "model_text_delta" not in [event for event, _ in frames]
    rows = db.exec(select(AgentEvent).where(AgentEvent.session_id == final["session_id"])).all()
    deltas = [r for r in rows if r.event_type == "stream_delta"]
    finished = next(r for r in rows if r.event_type == "task_frame_finished")
    assert deltas
    if native:
        assert public == "请补充订单日期。"
        assert len(deltas) == 1
    else:
        assert min(r.created_at for r in deltas) < finished.created_at
        assert "7" in public


def test_real_engine_interrupts_unavailable_capability_loop_and_keeps_checkpoint(db, fake_model):
    from app.db.models import HarnessAgentLoopRecord

    model, base = fake_model
    agent, user = _seed(db, base)
    original = model.reply

    def unavailable(body):
        if "TurnPlanner" in json.dumps(body.get("messages"), ensure_ascii=False):
            return original(body)
        model.requests.append(body)
        call_id = f"unavailable-{len(model.requests)}"
        return [_chunk(call_id, {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                "function": {"name": "mcp__staffdeck__capability_invoke", "arguments": json.dumps({
                    "operation": "missing.operation/v1", "resource_id": "missing", "arguments": {}})}}]}),
                _chunk(call_id, {}, "tool_calls")]

    model.reply = unavailable
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id="tenant_demo", agent_id=agent.id,
        user_id=user.id, message="执行模拟任务", channel="web", client_turn_id="stuck-loop"))
    assert "暂停自动重试" in response.reply
    assert response.runtime_error_code is None
    assert len(model.requests) <= 5, "do not burn 32 model calls on an unavailable operation"
    assert not db.exec(select(HarnessInvocationRecord)).all(), "no external business action was executed"
    loops = db.exec(select(HarnessAgentLoopRecord)).all()
    assert loops and any(row.checkpoint_json for row in loops)


def test_image_turn_uses_v3_for_planning_and_execution_without_legacy_fallback(db, fake_model, monkeypatch):
    import base64
    from app.core.turn_coordinator import HarnessV2Engine
    from app.session.session_schema import ChatAttachmentRead
    from app.db.models import HarnessAgentLoopRecord

    def retired(*a, **k):
        raise AssertionError("image requests must never open the retired engine")

    monkeypatch.setattr(HarnessV2Engine, "__init__", retired)
    model, base = fake_model
    agent, user = _seed(db, base)
    encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    data_url = "data:image/png;base64," + encoded
    response = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id="tenant_demo", agent_id=agent.id,
        user_id=user.id, message="根据这张图片回答问题", channel="web", client_turn_id="image-v3",
        attachments=[ChatAttachmentRead(id="image1", filename="pixel.png", kind="image", content_type="image/png",
                                       size=len(base64.b64decode(encoded)), data_url=data_url)]))
    assert response.runtime_error_code is None, response.reply
    assert len(model.requests) >= 2
    phase_requests = [request for request in model.requests if any(
        marker in json.dumps(request["messages"], ensure_ascii=False)
        for marker in ("TurnPlanner", "平台系统工具")
    )]
    assert len(phase_requests) >= 2
    for index, request in enumerate(phase_requests):
        assert any(part.get("type") == "image_url" and part["image_url"]["url"] == data_url
                   for msg in request["messages"] if isinstance(msg.get("content"), list)
                   for part in msg["content"] if isinstance(part, dict)), {
                       "request": index, "roles": [m.get("role") for m in request["messages"]],
                       "types": [type(m.get("content")).__name__ for m in request["messages"]],
                       "planner": "TurnPlanner" in json.dumps(request["messages"]),
                   }
    for row in db.exec(select(HarnessAgentLoopRecord)).all():
        assert encoded not in json.dumps(row.checkpoint_json)


@pytest.mark.parametrize("streaming", [False, True])
def test_real_skill_test_modes_use_one_v3_chain_and_never_legacy_runner(db, fake_model, monkeypatch, streaming):
    import asyncio
    from app.api import general_skills as api
    from app.db.models import GeneralSkill
    from app.general_skills.runner import GeneralSkillRunner
    from app.general_skills.schema import GeneralSkillRunRequest

    model, base = fake_model
    agent, user = _seed(db, base)
    skill = GeneralSkill(id="gs-real", tenant_id="tenant_demo", slug="shared-test", name="Shared test",
        skill_markdown="# Shared test\nRead this instruction and answer SKILL-V3-OK.", status="published")
    db.add(skill)
    db.commit()
    ensure_private_resource_binding(db, "tenant_demo", agent.id, "general_skill", skill.id)
    db.commit()

    def retired(*a, **k):
        raise AssertionError("GeneralSkillRunner cannot be used by either endpoint")

    monkeypatch.setattr(GeneralSkillRunner, "run", retired)
    monkeypatch.setattr(api, "engine", db.get_bind())
    original = model.reply

    def reply(body):
        if "TurnPlanner" in json.dumps(body.get("messages")):
            return original(body)
        model.requests.append(body)
        if not any(m.get("role") == "tool" for m in body["messages"]):
            return [_chunk("skill", {"tool_calls": [{"index": 0, "id": "skill-read", "type": "function",
                "function": {"name": "mcp__staffdeck__general_skill_read",
                    "arguments": json.dumps({"skill_id": skill.id, "query": "follow instructions"})}}]}),
                    _chunk("skill", {}, "tool_calls")]
        return [_chunk("reply", {"content": "SKILL-V3-OK"}), _chunk("reply", {}, "stop")]

    model.reply = reply
    request = GeneralSkillRunRequest(tenant_id="tenant_demo", agent_id=agent.id,
                                     query="follow instructions", model_config_id="model_1")
    result = (api.run_general_skill_stream if streaming else api.run_general_skill)(
        skill.slug, request, db=db, current_user=user)
    if streaming:
        async def consume():
            return [chunk async for chunk in result.body_iterator]
        chunks = asyncio.run(consume())
        text = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "SKILL-V3-OK" in text and '"execution_engine": "harness_v3"' in text
    else:
        assert result.reply == "SKILL-V3-OK"
    assert model.requests
    invocations = db.exec(select(HarnessInvocationRecord)).all()
    assert any(row.status == "completed" and "general_skill" in row.tool_name for row in invocations)
    assert all((row.approval_json or {}).get("engine") == "harness_v3" for row in invocations)


class ContinuityModel:
    """The nonce exists only in a prior assistant response, never in later user input."""
    def __init__(self):
        self.requests = []
        self.restored = []

    def reply(self, body):
        self.requests.append(body)
        messages = body.get("messages", [])
        text = json.dumps(messages, ensure_ascii=False)
        # The engine also appends a role=user runtime-policy snapshot. Select the actual SD
        # execution input, not whichever auxiliary context block happens to be last.
        user = next((m for m in reversed(messages) if m.get("role") == "user" and "# 本次用户输入" in json.dumps(m.get("content", ""), ensure_ascii=False)), {})
        current = json.dumps(user.get("content", ""), ensure_ascii=False)
        tools = {t["function"]["name"] for t in body.get("tools", [])}
        if messages and messages[-1].get("role") == "tool":
            return [_chunk("done", {"role": "assistant", "content": "已提交。"}), _chunk("done", {}, "stop")]
        is_sop = "mcp__staffdeck__submit_step_result" in tools
        if "SECOND_INPUT" in current:
            assert "NONCE-731" in text, "prior execution context was lost"
            self.restored.append("恢复的执行上下文" in current)
            status, reply = "completed", "confirmed NONCE-731"
        elif "OTHER_SOP" in current:
            assert "NONCE-731" not in text, "another SOP's history leaked"
            status, reply = "completed", "isolated workflow"
        else:
            status, reply = "awaiting_user", "NONCE-731, please confirm"
        if not is_sop:
            return [_chunk("general", {"role": "assistant", "content": reply}), _chunk("general", {}, "stop")]
        args = json.dumps({"status": status, "reply_fragment": reply, "slot_updates": {"confirmed": status == "completed"}})
        return [_chunk("sop", {"role": "assistant", "tool_calls": [{"index": 0, "id": f"control-{len(self.requests)}", "type": "function", "function": {"name": "mcp__staffdeck__submit_step_result", "arguments": args}}]}), _chunk("sop", {}, "tool_calls")]


@pytest.mark.parametrize('submission_mode', ['valid', 'repair', 'pause_then_repair', 'exhaust', 'supervised_exhaust', 'repair_exhausted', 'resume_repair', 'resume_legacy'])
def test_sop_tool_receipt_survives_next_node_serialization(db, fake_model, monkeypatch, submission_mode):
    from app.db.models import Skill, Tool
    from app.tools import ToolExecutor
    from app.tools.tool_schema import ToolResult
    model, base = fake_model
    agent, user = _seed(db, base)
    tool=Tool(id="receipt-tool", tenant_id=user.tenant_id, name="receipt-tool", method="GET", url="http://unused.invalid")
    sop=Skill(id="receipt-sop",tenant_id=user.tenant_id,skill_id="receipt-flow",name="Receipt flow",status="published",
        content_json={"start_node_id":"invoke","terminal_node_ids":["reply"],"nodes":[
            {"node_id":"invoke","name":"Call", "allowed_actions":["call_tool:receipt-tool"],
             "capability_refs":{"tool_ids":[tool.id],"required_tool_ids":[tool.id]}},
            {"node_id":"reply","name":"Reply","type":"response","instruction":"SERIALIZED-RECEIPT-REPLY-NODE","allowed_actions":["answer_user"]}],
            "edges":[{"source_node_id":"invoke","next_node_id":"reply"}]})
    db.add_all([tool,sop])
    db.commit()
    for kind, identifier in (("tool",tool.id),("skill",sop.id)):
        ensure_private_resource_binding(db,user.tenant_id,agent.id,kind,identifier)
    db.commit()
    calls=[]
    final_phases=[]
    script_trace=[]
    bad_submissions=[]
    repair_requests=[]
    resumed=[]
    supervised=[]
    tool_receipts=[]
    def execute(self, tenant, call, **kwargs):
        calls.append(tenant.name)
        return ToolResult(tool_name=tenant.name,success=True,data={"marker":"real-receipt"})
    monkeypatch.setattr(ToolExecutor,"execute_sync_http",execute)
    def reply(body):
        model.requests.append(body)
        messages=body.get("messages",[])
        tool_receipts.extend(m.get('content') for m in messages if m.get('role') == 'tool' and m.get('tool_call_id') == 'tool-call')
        repair_input = next((m.get('content') for m in messages if isinstance(m.get('content'),str)
            and '"phase": "sop_result_repair"' in m['content']),None)
        if repair_input:
            assert not body.get('tools'), 'Dedicated repair must expose no business tools'
            payload=json.loads(repair_input[repair_input.index('{"phase": "sop_result_repair"'):])
            repair_requests.append(payload)
            raw=payload['raw_submission']
            assert ': reply' in raw
            answer = raw if submission_mode=='repair_exhausted' or (
                submission_mode in {'resume_repair','resume_legacy'} and not resumed) else raw.replace(': reply', ': "reply"')
            return [_chunk('repaired',{'role':'assistant','content':answer}),_chunk('repaired',{},'stop')]
        actual=next((m for m in reversed(messages) if m.get("role")=="user" and "# 本次用户输入" in str(m.get("content"))),{})
        current=json.dumps(actual.get("content",""),ensure_ascii=False).replace('\\','')
        final_node="SERIALIZED-RECEIPT-REPLY-NODE" in current
        script_trace.append({"final_node":final_node,"roles":[m.get("role") for m in messages],
            "last_call":messages[-1].get("tool_call_id") if messages else None,
            "current":current[-600:],"tools":[t['function']['name'] for t in body.get('tools',[])]})
        def call(name,args,identifier):
            return [_chunk(identifier,{"tool_calls":[{"index":0,"id":identifier,"type":"function",
                "function":{"name":name,"arguments":json.dumps(args)}}]}),_chunk(identifier,{},"tool_calls")]
        if messages and messages[-1].get("role")=="tool" and str(messages[-1].get("tool_call_id","")).startswith("submit"):
            return [_chunk("done",{"content":"RECEIPT-END-OK" if final_node else "tool done"}),_chunk("done",{},"stop")]
        if final_node:
            final_phases.append(True)
            return call("mcp__staffdeck__submit_step_result",{"status":"completed","reply_fragment":"RECEIPT-END-OK"},"submit-final")
        if any(m.get("role")=="tool" and m.get("tool_call_id")=="tool-call" for m in messages):
            if submission_mode == 'supervised_exhaust' and not supervised:
                supervised.append(True)
                return [_chunk('stop-before-submit',{'content':'已获得执行结果。'}),_chunk('stop-before-submit',{},'stop')]
            if (submission_mode == 'pause_then_repair' and bad_submissions
                    and '当前 SOP 业务执行结果已保留，只修正步骤完成 JSON' not in json.dumps(messages,ensure_ascii=False)):
                return [_chunk('pause',{'content':'结果 JSON 格式错误。'}),_chunk('pause',{},'stop')]
            if submission_mode in {'exhaust','supervised_exhaust','repair_exhausted','resume_repair','resume_legacy'} or (submission_mode in {'repair','pause_then_repair'} and not bad_submissions):
                bad_submissions.append(True)
                chunks=call("mcp__staffdeck__submit_step_result",{},"bad-submit-"+str(len(bad_submissions)))
                chunks[0]['choices'][0]['delta']['tool_calls'][0]['function']['arguments']='{"status":"completed","reply_fragment":"done","next_step_id": reply}'
                return chunks
            return call("mcp__staffdeck__submit_step_result",{"status":"completed","next_step_id":"reply","reply_fragment":"tool done"},"submit-next")
        return call("mcp__staffdeck__tool_invoke",{"tool_id":tool.id,"arguments":{}},"tool-call")
    model.reply=reply
    response=AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id,agent_id=agent.id,user_id=user.id,
        message="/sop receipt-flow check receipt continuation",client_turn_id="receipt-test"))
    assert calls == [tool.name], tool_receipts
    if submission_mode in {'repair_exhausted','resume_repair','resume_legacy'}:
        from app.db.models import HarnessTaskFrameRecord, HarnessAgentLoopRecord
        frame=db.exec(select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.session_id==response.session_id)).one()
        assert frame.status == 'queued', response.reply
        assert frame.result_json['error']['code'] == 'SOP_RESULT_REPAIR_EXHAUSTED'
        assert 'real-receipt' in json.dumps(db.get(HarnessAgentLoopRecord,frame.agent_loop_id).checkpoint_json)
        assert calls == [tool.name]
        assert '能力绑定' not in response.reply and '补充信息' not in response.reply
        assert len(repair_requests)==2, 'Counter exhaustion must enter dedicated Runtime repair'
        if submission_mode=='repair_exhausted':
            return
        logical=db.get(HarnessAgentLoopRecord,frame.agent_loop_id)
        assert logical.checkpoint_json.get('pending_result_submission')
        if submission_mode=='resume_legacy':
            logical.checkpoint_json={k:v for k,v in logical.checkpoint_json.items() if k!='pending_result_submission'}
            db.add(logical)
            db.commit()
        from app.session.session_schema import TurnPlan,PlannedTaskFrame
        from staffdeck_harness.runtime.model_phases import EngineTurnPlanner
        monkeypatch.setattr(EngineTurnPlanner,'plan',lambda *a,**k:TurnPlan(decision='continue_active',
            task_frames=[PlannedTaskFrame(task_id=frame.task_id,kind='sop',decision='continue_active',
                target_skill_id=sop.skill_id,target_step_id='invoke')]))
        resumed.append(True)
        response=AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id,agent_id=agent.id,user_id=user.id,
            session_id=response.session_id,message='继续修正结果',client_turn_id='resume-result-only'))
        assert len(repair_requests)==3
    if submission_mode in {'exhaust','supervised_exhaust','pause_then_repair'}:
        assert len(repair_requests)==1
    if submission_mode=='exhaust':
        assert len(bad_submissions)>=3
    assert response.runtime_error_code is None,(response.reply,script_trace[-4:])
    assert calls==[tool.name] and final_phases
    assert "RECEIPT-END-OK" in response.reply and response.session_state.active_skill_id is None


@pytest.mark.parametrize("cold", [False, True])
def test_real_engine_sop_context_survives_user_turns_and_worker_replacement(db, fake_model, cold):
    from app.db.models import ChatSession, HarnessTaskFrameRecord, HarnessAgentLoopRecord
    from app.core.task_frame_store import TaskFrameStore
    from app.core.task_request_compiler import TaskRequirement
    from staffdeck_harness.bridge.engine_host import get_runtime, reset_runtime
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent, HarnessV3TurnContext
    from staffdeck_harness.composition.staff import project_staff
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.security.oss_local import build_oss_local_profile
    from staffdeck_harness.security.profile import Guard
    from staffdeck_harness.modules.registry import get_registry

    _, base = fake_model
    agent, user = _seed(db, base)
    script = ContinuityModel()
    _ModelHandler.model = script
    session = ChatSession(id="session-continuity", tenant_id=user.tenant_id, user_id=user.id, agent_id=agent.id)
    frame = HarnessTaskFrameRecord(tenant_id=user.tenant_id, session_id=session.id, source_turn_id="first", task_id="sop-frame", kind="sop", skill_id="workflow", step_id="confirm")
    db.add_all([session, frame])
    db.commit()
    store = TaskFrameStore(db)
    logical = store.ensure_agent_loop(frame)
    db.commit()
    settings = get_settings()
    reg = get_registry(settings)
    snapshot = CompositionCompiler(hooks=()).compile(project_staff(db, user.tenant_id, agent.id))
    profile = build_oss_local_profile()
    model = db.get(ModelConfig, "model_1")
    traces = []

    def run(text, turn_id, loop_id=logical.id, frame_id="sop-frame", kind="sop", cp=None):
        req = TaskRequirement(task_frame_id=frame_id, execution_loop_id=loop_id, kind=kind,
                              goal="continuity test", current_user_message=text,
                              sop_context={"step": {"node_id": "confirm"}} if kind == "sop" else {})
        turn = HarnessV3TurnContext(db, snapshot, profile.identity.from_user(user), Guard("test", profile),
                                   user.tenant_id, agent.id, user.id, session.id, turn_id, "web", "run-test", frame_id,
                                   module_registry=reg)
        runner = HarnessV3TaskAgent(get_runtime(settings), turn)
        return runner.run(req, model, lambda *a: None, max_actions=5, checkpoint=cp,
                          trace_sink=lambda event, payload: traces.append((event, payload)))

    first = run("FIRST_INPUT", "first")
    assert first.status == "awaiting_user", first
    assert "NONCE-731" in first.reply_fragment
    store.finish_agent_loop_for_frame(frame, result_status=first.status, checkpoint=first.loop_checkpoint, last_run_id=None)
    db.commit()
    db.refresh(logical)
    assert logical.status == "suspended"
    old_engine_session = next(p["engine_session_id"] for e, p in traces if e == "harness_v3_context_bound")
    if cold:
        reset_runtime()
    second = run("SECOND_INPUT", "second", cp=dict(logical.checkpoint_json))
    assert second.status == "completed" and "NONCE-731" in second.reply_fragment, json.dumps(script.requests[-2:])[-7000:]
    store.finish_agent_loop_for_frame(frame, result_status=second.status, checkpoint=second.loop_checkpoint, last_run_id=None)
    db.commit()
    db.refresh(logical)
    assert logical.status == "completed"
    new_session = [p["engine_session_id"] for e, p in traces if e == "harness_v3_context_bound"][-1]
    assert (new_session != old_engine_session) is cold
    assert script.restored[-1] is cold
    other = run("OTHER_SOP", "third", loop_id="separate-sop", frame_id="other-frame")
    assert other.status == "completed", json.dumps(script.requests[-2:])[-7000:]
    assert len(db.exec(select(HarnessAgentLoopRecord)).all()) == 1, "transport must not create business loops"
    assert not any(e == "harness_action_created" and p.get("action") == "tool" and "submit" in str(p.get("tool_name")) for e, p in traces)


def test_real_engine_general_loop_keeps_context_across_new_task_frames(db, fake_model):
    # The pure context tests cover scope fencing. This uses the real SDK and model wire,
    # with no completion control in either request's tool list.
    from app.db.models import ChatSession
    from app.core.task_request_compiler import TaskRequirement
    from staffdeck_harness.bridge.engine_host import get_runtime, reset_runtime
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent, HarnessV3TurnContext
    from staffdeck_harness.composition.staff import project_staff
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.security.oss_local import build_oss_local_profile
    from staffdeck_harness.security.profile import Guard
    from staffdeck_harness.modules.registry import get_registry

    _, base = fake_model
    agent, user = _seed(db, base)
    script = ContinuityModel()
    _ModelHandler.model = script
    db.add(ChatSession(id="general-session", tenant_id=user.tenant_id, user_id=user.id, agent_id=agent.id))
    db.commit()
    settings = get_settings()
    reg = get_registry(settings)
    snap = CompositionCompiler(hooks=()).compile(project_staff(db, user.tenant_id, agent.id))
    profile = build_oss_local_profile()
    checkpoint = None
    for index, text in enumerate(["FIRST_INPUT", "SECOND_INPUT"]):
        frame_id = f"general-frame-{index}"
        turn = HarnessV3TurnContext(db, snap, profile.identity.from_user(user), Guard("test", profile),
                                   user.tenant_id, agent.id, user.id, "general-session", f"turn-{index}", "web", "run", frame_id, module_registry=reg)
        req = TaskRequirement(task_frame_id=frame_id, execution_loop_id="general-loop", kind="conversation", goal="test", current_user_message=text)
        result = HarnessV3TaskAgent(get_runtime(settings), turn).run(req, db.get(ModelConfig, "model_1"), lambda *a: None, checkpoint=checkpoint)
        assert result.status == "completed" and "NONCE-731" in result.reply_fragment
        checkpoint = result.loop_checkpoint
        reset_runtime()
    assert all(not any("submit_step_result" in t["function"]["name"] for t in r.get("tools", [])) for r in script.requests)


@pytest.mark.parametrize("replace_sop_runtime", [False, True])
def test_full_coordinator_keeps_sop_instance_suspended_then_advances_and_completes(db, fake_model, monkeypatch, replace_sop_runtime):
    from app.db.models import Skill, HarnessAgentLoopRecord, HarnessTaskFrameRecord
    from app.session.session_schema import TurnPlan, PlannedTaskFrame
    from staffdeck_harness.runtime.model_phases import EngineTurnPlanner
    from staffdeck_harness.bridge.engine_host import reset_runtime

    def legacy_sop_forbidden(*args, **kwargs):
        raise AssertionError("Harness v3 orchestration must not call legacy AgentLoop SOP methods")

    for name in ("_apply_step_result", "_finalize_execution_after_reply", "_default_next_step",
                 "_drop_unavailable_skill_state", "_list_published_skills", "_get_active_skill"):
        monkeypatch.setattr(AgentLoop, name, legacy_sop_forbidden)

    _, base = fake_model
    agent, user = _seed(db, base)
    script = ContinuityModel()
    _ModelHandler.model = script
    replacement_calls = []
    if replace_sop_runtime:
        import sys
        from types import ModuleType
        from staffdeck_harness.sop.lifecycle import SopRuntime
        from staffdeck_harness.contracts.manifest import SlotName, ModuleKind
        from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, install_registry, manifest

        class AlternateRuntime(SopRuntime):
            def after_execution(self, *args, **kwargs):
                replacement_calls.append(args[3].task_frame_id)
                return super().after_execution(*args, **kwargs)

        class AlternateProvider:
            def build(self, ports):
                assert not hasattr(ports, "db")
                return AlternateRuntime(None, ports.events, create_handoff=ports.create_handoff)

        plugin = ModuleType("sealed_sop_plugin")
        def register(registry, ctx):
            registry.install(manifest("sealed.sop", "Alternative SOP runtime", kind=ModuleKind.TRUSTED,
                                      slots=[SlotName.RUNTIME_SOP], provides=["sop.lifecycle/v2"],
                                      policy_actions=["sop.execute/v1"]),
                             AlternateProvider(), slot=SlotName.RUNTIME_SOP)
        plugin.register = register
        monkeypatch.setitem(sys.modules, "sealed_sop_plugin", plugin)
        monkeypatch.setenv("HARNESS_MODULES", "sealed_sop_plugin:register")
        monkeypatch.setenv("HARNESS_DISABLED_MODULES", "sop.runtime")
        get_settings.cache_clear()
        reg = discover_and_install(ModuleRegistry(), get_settings())
        reg.seal()
        assert not reg.get("sop.runtime").enabled
        install_registry(reg)
    skill = Skill(id="workflow-row", tenant_id=user.tenant_id, skill_id="continuity", name="Continuity",
                  status="published", content_json={"start_node_id": "collect", "goal": ["Confirm and finish"],
                  "nodes": [{"node_id": "collect", "name": "Collect confirmation", "expected_user_info": ["confirmed"]},
                            {"node_id": "review", "name": "Complete review", "expected_user_info": []}],
                  "edges": [{"source_node_id": "collect", "next_node_id": "review", "condition": "slots_complete"}]})
    db.add(skill)
    db.commit()
    ensure_private_resource_binding(db, user.tenant_id, agent.id, "skill", skill.id)
    db.commit()

    def plan(_self, message, session, *args, **kwargs):
        decision = "continue_active" if session.active_skill_id else "start_new_task"
        return TurnPlan(decision=decision, confidence=1, task_frames=[PlannedTaskFrame(
            task_id="same-sop", kind="sop", decision=decision, target_skill_id=skill.skill_id,
            target_step_id=session.active_step_id or "collect", user_intent="continuity test")])
    monkeypatch.setattr(EngineTurnPlanner, "plan", plan)
    first = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
                      user_id=user.id, channel="web", message="FIRST_INPUT", client_turn_id="sop-first"))
    assert first.runtime_error_code is None, first.reply
    frame = db.exec(select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.session_id == first.session_id)).one()
    logical = db.get(HarnessAgentLoopRecord, frame.agent_loop_id)
    assert frame.status == "awaiting_user" and logical.status == "suspended"
    loop_id = logical.id
    reset_runtime()
    second = AgentLoop(db).handle_turn(ChatTurnRequest(tenant_id=user.tenant_id, agent_id=agent.id,
                      user_id=user.id, session_id=first.session_id, channel="web", message="SECOND_INPUT", client_turn_id="sop-second"))
    assert second.runtime_error_code is None, second.reply
    db.refresh(frame)
    db.refresh(logical)
    assert frame.agent_loop_id == loop_id and frame.status == "completed", second
    assert logical.status == "completed"
    assert "NONCE-731" in json.dumps(logical.checkpoint_json)
    if replace_sop_runtime:
        assert replacement_calls == ["same-sop"] * 3


def test_real_engine_budget_exit_preserves_completed_tool_results(db, fake_model):
    from app.db.models import ChatSession
    from app.core.task_request_compiler import TaskRequirement
    from staffdeck_harness.bridge.engine_host import get_runtime
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent, HarnessV3TurnContext
    from staffdeck_harness.composition.staff import project_staff
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.security.oss_local import build_oss_local_profile
    from staffdeck_harness.security.profile import Guard
    from staffdeck_harness.modules.registry import get_registry
    _, base = fake_model
    agent, user = _seed(db, base)
    db.add(ChatSession(id="budget-session", tenant_id=user.tenant_id, user_id=user.id, agent_id=agent.id))
    db.commit()
    settings = get_settings()
    registry = get_registry(settings)
    snapshot = CompositionCompiler(hooks=()).compile(project_staff(db, user.tenant_id, agent.id))
    profile = build_oss_local_profile()
    turn = HarnessV3TurnContext(db, snapshot, profile.identity.from_user(user), Guard("test", profile),
                               user.tenant_id, agent.id, user.id, "budget-session", "budget-turn", "web", "run-budget", "frame-budget", module_registry=registry)
    req = TaskRequirement(task_frame_id="frame-budget", execution_loop_id="budget-loop", kind="sop", goal="query knowledge", current_user_message="query")
    result = HarnessV3TaskAgent(get_runtime(settings), turn).run(req, db.get(ModelConfig, "model_1"), lambda *a: None, max_actions=1)
    assert result.status == "action_budget" and result.action_count == 1
    assert result.loop_checkpoint["transcript"] and result.loop_checkpoint["capability_results"]
    assert result.loop_checkpoint["capability_results"][0]["success"]
