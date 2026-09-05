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
                _chunk(cid, {"tool_calls": [{"index": 0, "id": None, "function": {"name": None, "arguments": args}}]}),  # gateway-style null continuation
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


class ContinuityModel:
    """The nonce exists only in a prior assistant response, never in later user input."""
    def __init__(self):
        self.requests = []
        self.restored = []

    def reply(self, body):
        self.requests.append(body)
        messages = body.get("messages", [])
        text = json.dumps(messages, ensure_ascii=False)
        # DSH also appends a role=user runtime-policy snapshot. Select the actual SD
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


@pytest.mark.parametrize("cold", [False, True])
def test_real_dsh_sop_context_survives_user_turns_and_worker_replacement(db, fake_model, cold):
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


def test_real_dsh_general_loop_keeps_context_across_new_task_frames(db, fake_model):
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


def test_full_coordinator_keeps_sop_instance_suspended_then_advances_and_completes(db, fake_model, monkeypatch):
    from app.db.models import Skill, HarnessAgentLoopRecord, HarnessTaskFrameRecord
    from app.session.session_schema import TurnPlan, PlannedTaskFrame
    from staffdeck_harness.runtime.model_phases import EngineTurnPlanner
    from staffdeck_harness.bridge.engine_host import reset_runtime

    _, base = fake_model
    agent, user = _seed(db, base)
    script = ContinuityModel()
    _ModelHandler.model = script
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


def test_real_dsh_budget_exit_preserves_completed_tool_results(db, fake_model):
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
