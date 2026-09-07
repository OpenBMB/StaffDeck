"""The final reply streams as it is generated unless a supervision hook may refuse it.

Regression: after the module runtime became unconditional, the coordinator buffered *every*
reply behind ``supervise_reply`` because it gated on "a registry exists" rather than on "a
supervision hook exists". ``supervision_required`` is that decision, pinned here:

- default interaction hooks + ordinary conversation → not supervised → stream live
- default hooks + active SOP → supervised (sop.output_supervisor may steer/replace)
- a non-built-in turn_stopping hook + ordinary conversation → supervised
- no registry / no snapshot → not supervised
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.turn_coordinator import TurnCoordinator
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.composition.staff import SessionPolicy, StaffComposition
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.contracts.manifest import HookContribution, ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules import registry as registry_mod
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, manifest


def _ref(t, i, **attrs):
    return ResourceRef(type=t, id=i, tenant_id="t1", attributes=attrs)


def _staff():
    return StaffComposition(
        tenant_id="t1", staff_id="a1", name="A", is_overall=False, status="active", persona="你是A",
        model_route={"default": "m1"}, session_policy=SessionPolicy(), capabilities=(), sops=(),
        channels=(), team=None, interactions=("sop_adapter",), ref=_ref("agent", "a1", owner_user_id="u1"),
    )


def _coordinator(reg):
    engine = TurnCoordinator(SimpleNamespace(db=SimpleNamespace(), events=SimpleNamespace(record=lambda *a, **k: None)))
    engine.registry = reg
    engine.snapshot = CompositionCompiler().compile(_staff()) if reg is not None else None
    return engine


def _default_registry():
    from tests_harness.modules.conftest import FakeSettings

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


def test_ordinary_conversation_with_default_hooks_streams_live(monkeypatch):
    reg = _default_registry()
    monkeypatch.setattr(registry_mod, "_active", reg)
    engine = _coordinator(reg)
    assert engine.supervision_required(None) is False, "built-in supervisors are SOP-only; nothing can refuse a chat reply"


def test_active_sop_is_supervised(monkeypatch):
    reg = _default_registry()
    monkeypatch.setattr(registry_mod, "_active", reg)
    engine = _coordinator(reg)
    assert engine.supervision_required(SimpleNamespace(skill_id="sop_leave")) is True


def test_custom_turn_stopping_hook_supervises_ordinary_conversation(monkeypatch):
    from tests_harness.modules.conftest import FakeSettings

    reg = discover_and_install(ModuleRegistry(), FakeSettings())
    reg.install(
        manifest("acme.review", "Review", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INTERACTION], hooks=[HookContribution("turn_stopping", "review")]),
        SimpleNamespace(handlers={"review": lambda ctx, state: HookDecision.deny("unsafe")}),
        slot=SlotName.STAFF_INTERACTION,
    )
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    monkeypatch.setattr(registry_mod, "_active", reg)
    engine = _coordinator(reg)
    assert engine.supervision_required(None) is True, "a third-party output reviewer must see the text before the client does"


def test_no_registry_or_snapshot_is_never_supervised(monkeypatch):
    monkeypatch.setattr(registry_mod, "_active", None)
    engine = _coordinator(None)
    assert engine.supervision_required(None) is False
    reg = _default_registry()
    engine = _coordinator(reg)
    engine.snapshot = None
    assert engine.supervision_required(SimpleNamespace(skill_id="x")) is False


# --------------------------------------------------------------------------- live engine deltas

def test_relay_turns_engine_text_deltas_into_stream_deltas_and_ignores_the_rest():
    from staffdeck_harness.events.relay import relay_event

    text = relay_event({"type": "assistant/chunk", "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "index": 0, "text": "你好"}}})
    assert text == [("model_text_delta", {"content": "你好", "execution_engine": "harness_v3"})]
    reasoning = relay_event({"type": "assistant/chunk", "data": {"chunk": {"type": "reasoning-delta", "index": 0, "text": "thinking"}}})
    tool = relay_event({"type": "assistant/chunk", "data": {"chunk": {"type": "tool-call-delta", "index": 0, "id": "c1", "argumentsDelta": "{"}}})
    assert reasoning == [] and tool == [], "reasoning and tool-call deltas never reach the client"
    flat = relay_event({"type": "assistant/chunk", "data": {"text": "legacy"}})
    assert flat == [("model_text_delta", {"content": "legacy", "execution_engine": "harness_v3"})]


def test_streamed_text_is_turn_local_not_persisted_with_the_task_result():
    from app.core.task_request_compiler import TaskExecutionResult

    r = TaskExecutionResult(task_frame_id="tf", status="completed", reply_fragment="你好，老王")
    r.streamed_reply = "你好，老王"
    assert r.model_dump().get("streamed_reply") is None, "never persisted with the result"


def test_task_agent_forwards_deltas_live_only_for_unsupervised_conversation():
    """Engine text deltas reach the client sink as they arrive (and are remembered for de-dup),
    never the audit trace; with live streaming off, or outside a conversation frame, they are
    dropped. A failing sink never fails the turn."""

    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent

    def agent_for(*, live, sink):
        a = HarnessV3TaskAgent.__new__(HarnessV3TaskAgent)
        a.turn = SimpleNamespace(live_stream=live, stream_sink=sink)
        return a

    audit, sunk = [], []
    a = agent_for(live=True, sink=SimpleNamespace(on_delta=sunk.append))
    trace = a._client_stream_trace(lambda e, p: audit.append(e), SimpleNamespace(kind="conversation"))
    trace("model_text_delta", {"content": "你好"})
    trace("model_text_delta", {"content": ""})
    trace("harness_v3_tool_call", {"name": "x"})
    trace("model_text_delta", {"content": "，老王"})
    assert sunk == ["你好", "，老王"] and a._streamed_parts == ["你好", "，老王"]
    assert audit == ["harness_v3_tool_call"], "deltas never become audit rows; other events pass through"

    for live, kind in ((False, "conversation"), (True, "sop_step"), (True, "conversation")):
        sunk.clear()
        sink = None if (live and kind == "conversation") else SimpleNamespace(on_delta=sunk.append)
        a = agent_for(live=live, sink=sink)
        a._client_stream_trace(lambda e, p: None, SimpleNamespace(kind=kind))("model_text_delta", {"content": "x"})
        assert sunk == [] and a._streamed_parts == [], (live, kind, sink)

    def boom(_):
        raise RuntimeError("client gone")

    a = agent_for(live=True, sink=SimpleNamespace(on_delta=boom))
    a._client_stream_trace(lambda e, p: None, SimpleNamespace(kind="conversation"))("model_text_delta", {"content": "x"})
    assert a._streamed_parts == [], "failed delivery must not suppress the final fallback"
