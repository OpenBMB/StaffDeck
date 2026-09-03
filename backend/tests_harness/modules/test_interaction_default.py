"""Module tests for ``interaction.default`` (对话介入规则（默认）, kind A, slot staff.interaction).

Provider: ``staffdeck_harness.modules.builtin.DefaultInteractions`` — contributes
the nine default hook handlers the InteractionPipelineHost runs around a step.
"""

from __future__ import annotations

import re

import pytest

from staffdeck_harness.composition import (
    DEFAULT_HOOKS,
    CapabilityBindingView,
    CompositionCompiler,
    SessionPolicy,
    SopView,
    StaffComposition,
    compile_hooks,
    sop_slots,
)
from staffdeck_harness.contracts.hooks import HookContext, HookDecision
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.interactions.pipeline_host import DEFAULT_HANDLERS, InteractionPipelineHost, PipelineState
from staffdeck_harness.modules.builtin import DefaultInteractions
from staffdeck_harness.modules.registry import ModuleRegistry, UnsatisfiedRequirement, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "interaction.default"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")
EXPECTED_HOOKS = [
    "pre_step:persona",
    "pre_step:memory.recall",
    "pre_step:sop.execution_slice",
    "pre_tool:activation.allowlist",
    "pre_tool:capability.pep",
    "post_tool:ledger.record",
    "post_tool:citations.collect",
    "turn_stopping:sop.output_supervisor",
    "turn_stopping:handoff.detect",
]


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return m
    raise AssertionError(f"{module_id} not found in taxonomy tree")


def _ref(t, i, **attrs):
    return ResourceRef(type=t, id=i, tenant_id="t1", attributes=attrs)


SOP_CONTENT = {
    "start_node_id": "n1",
    "terminal_node_ids": ["n2"],
    "nodes": [
        {
            "node_id": "n1", "name": "收集信息", "instruction": "先问清楚订单号。",
            "expected_user_info": ["order_no"], "allowed_actions": ["handoff"],
            "metadata": {"slots": [{"name": "policy_docs", "operation": "knowledge.search/v1", "required": True}]},
        },
        {"node_id": "n2", "name": "结束"},
    ],
    "edges": [{"source_node_id": "n1", "next_node_id": "n2", "condition": "order_no 已知"}],
}


@pytest.fixture
def snapshot():
    caps = (
        CapabilityBindingView(resource_type="tool", resource_id="tool_1", binding_id="b_tool", ref=_ref("tool", "tool_1"), name="订单查询"),
        CapabilityBindingView(resource_type="knowledge_base", resource_id="kb_1", binding_id="b_kb", ref=_ref("knowledge_base", "kb_1"), name="政策库", capability_scope="sop_specific"),
    )
    sop = SopView(skill_id="sop_1", row_id="row_1", version="1.0.0", name="退款流程", content=SOP_CONTENT, binding_id="sb_1", slot_bindings={"policy_docs": "kb_1"}, declared_slots=tuple(sop_slots(SOP_CONTENT)), ref=_ref("sop", "sop_1"))
    staff = StaffComposition(
        tenant_id="t1", staff_id="a1", name="A", is_overall=False, status="active", persona="你是退款专员。",
        model_route={"default": "m1"}, session_policy=SessionPolicy(), capabilities=caps, sops=(sop,),
        channels=(), team=None, interactions=(MODULE_ID,), ref=_ref("agent", "a1", owner_user_id="u1"),
    )
    return CompositionCompiler(hooks=DEFAULT_HOOKS).compile(staff)


def _ctx(point: str, snapshot, payload=None, step: int = 1) -> HookContext:
    return HookContext(point=point, tenant_id="t1", agent_id="a1", session_id="s1", turn_id="turn1", step=step, snapshot_id=snapshot.snapshot_id, payload=dict(payload or {}))


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_interaction_default(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "A"
    assert item.slot is SlotName.STAFF_INTERACTION
    assert m.attaches_to == (SlotName.STAFF_INTERACTION,)
    assert m.provides_operations == ("hook.contribute/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == DEFAULT_HOOKS
    assert isinstance(item.provider, DefaultInteractions)
    assert item.provider.module_id == MODULE_ID

    d = _described(registry)
    assert d["name"] == "对话介入规则（默认）"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["contract_version"] == "v1"
    assert d["enabled"] is True
    assert d["source"] == "builtin"
    assert d["hooks"] == EXPECTED_HOOKS
    assert d["policy_actions"] == []


def test_manifest_not_guarded_without_policy_actions(settings):
    """Builtin registration leaves staff.interaction unguarded: hooks carry no policy actions of their own."""

    reg = discover_and_install(ModuleRegistry(), settings)
    d = _described(reg)
    assert d["policy_actions"] == [] and d["guarded"] is False


# --------------------------------------------------------------------------- 2. placement

def test_placement_interaction_default(registry):
    d = _described(registry)
    assert d["switchable"] is True
    placed = _placed(registry)
    assert placed["placement"] == {"big_id": "sop", "sub_id": "sop.supervision", "source": "taxonomy"}
    assert placed["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_interaction_default(settings):
    class Disabled(type(settings)):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert reg.providers(SlotName.STAFF_INTERACTION) == []
    assert reg.hooks() == () and reg.hook_handlers() == {}
    for slot in SlotName:
        reg.mark_guarded(slot)
    # The only hook contributor is off, so the compiler's ``hook.contribute/v1`` requirement is
    # unmet and the assembly refuses to seal (preflight surfaces this before a restart).
    with pytest.raises(UnsatisfiedRequirement) as exc:
        reg.seal()
    assert exc.value.details == {"missing": ["hook.contribute/v1"]}
    assert reg.sealed is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_interaction_default_handlers_mapping(registry, module):
    handlers = module(MODULE_ID).provider.handlers
    assert set(handlers) == set(DEFAULT_HANDLERS) == {h.split(":", 1)[1] for h in EXPECTED_HOOKS}
    assert len(handlers) == 9
    assert all(callable(fn) for fn in handlers.values())
    # the registry surfaces the same mapping and the same hook plan to the host
    assert registry.hook_handlers() == dict(DEFAULT_HANDLERS)
    assert registry.hooks() == DEFAULT_HOOKS
    plan = compile_hooks(registry.hooks())
    assert {p: [c.handler for c in cs] for p, cs in plan.order.items()} == {
        "pre_step": ["persona", "memory.recall", "sop.execution_slice"],
        "pre_tool": ["activation.allowlist", "capability.pep"],
        "post_tool": ["ledger.record", "citations.collect"],
        "turn_stopping": ["sop.output_supervisor", "handoff.detect"],
    }
    InteractionPipelineHost(plan, handlers)  # every planned handler resolves


def test_provider_interaction_default_pre_step_handlers(module, snapshot):
    h = module(MODULE_ID).provider.handlers
    st = PipelineState(snapshot=snapshot, memory_context=[{"kind": "fact", "content": "喜欢简洁回复"}, {"content": ""}])

    d = h["persona"](_ctx("pre_step", snapshot), st)
    assert d.kind == "modify" and d.contexts == ({"type": "text", "text": "你是退款专员。", "source": "persona"},)
    assert h["persona"](_ctx("pre_step", snapshot, step=2), st).kind == "pass"

    d = h["memory.recall"](_ctx("pre_step", snapshot), st)
    assert d.kind == "modify" and d.contexts[0]["source"] == "memory"
    assert "- [fact] 喜欢简洁回复" in d.contexts[0]["text"]
    assert h["memory.recall"](_ctx("pre_step", snapshot), PipelineState(snapshot=snapshot)).kind == "pass"

    assert h["sop.execution_slice"](_ctx("pre_step", snapshot), st).kind == "pass"  # no active SOP
    st.active_sop_id = "missing"
    assert h["sop.execution_slice"](_ctx("pre_step", snapshot), st).kind == "pass"
    st.active_sop_id, st.active_node_id = "sop_1", "n1"
    d = h["sop.execution_slice"](_ctx("pre_step", snapshot), st)
    assert d.kind == "modify" and d.metadata == {"skill_id": "sop_1", "node_id": "n1"}
    text = d.contexts[0]["text"]
    assert d.contexts[0]["source"] == "sop"
    assert "退款流程" in text and "先问清楚订单号。" in text and "policy_docs → knowledge_base:kb_1" in text
    assert st.extras["execution_slice"].expected_user_info == ("order_no",)


def test_provider_interaction_default_pre_tool_handlers(module, snapshot):
    h = module(MODULE_ID).provider.handlers
    st = PipelineState(snapshot=snapshot)
    allow = h["activation.allowlist"]
    assert allow(_ctx("pre_tool", snapshot, {"name": "tool_invoke", "arguments": {"tool_id": "tool_1"}}), st).kind == "pass"
    d = allow(_ctx("pre_tool", snapshot, {"name": "tool_invoke", "arguments": {"tool_id": "tool_9"}}), st)
    assert d.kind == "deny" and "tool_9" in (d.reason or "")
    # kb_1 is sop_specific: not reachable in general conversation, reachable inside the SOP node
    assert allow(_ctx("pre_tool", snapshot, {"name": "knowledge_search", "arguments": {}}), st).kind == "deny"
    st.active_sop_id, st.active_node_id = "sop_1", "n1"
    assert allow(_ctx("pre_tool", snapshot, {"name": "knowledge_search", "arguments": {}}), st).kind == "pass"
    assert allow(_ctx("pre_tool", snapshot, {"name": "general_skill_read", "arguments": {"skill_id": "gs_x"}}), st).kind == "deny"

    assert st.tool_calls == 0
    assert h["capability.pep"](_ctx("pre_tool", snapshot, {"name": "tool_invoke"}), st).kind == "pass"
    assert st.tool_calls == 1


def test_provider_interaction_default_post_tool_handlers(module, snapshot):
    h = module(MODULE_ID).provider.handlers
    st = PipelineState(snapshot=snapshot)
    assert h["ledger.record"](_ctx("post_tool", snapshot, {"receipt": {"invocation_id": "inv1", "status": "ok"}}), st).kind == "pass"
    assert h["ledger.record"](_ctx("post_tool", snapshot, {"receipt": "not-a-mapping"}), st).kind == "pass"
    assert st.receipts == [{"invocation_id": "inv1", "status": "ok"}]
    assert h["citations.collect"](_ctx("post_tool", snapshot, {"citations": [{"doc": "d1"}, "junk", {"doc": "d2"}]}), st).kind == "pass"
    assert st.citations == [{"doc": "d1"}, {"doc": "d2"}]


def test_provider_interaction_default_turn_stopping_handlers(module, snapshot):
    h = module(MODULE_ID).provider.handlers
    st = PipelineState(snapshot=snapshot, active_sop_id="sop_1", active_node_id="n1")
    # without a slice both handlers pass through
    assert h["sop.output_supervisor"](_ctx("turn_stopping", snapshot, {"final_text": ""}), st).kind == "pass"
    assert h["handoff.detect"](_ctx("turn_stopping", snapshot, {"final_text": "请转人工"}), st).kind == "pass"

    h["sop.execution_slice"](_ctx("pre_step", snapshot), st)
    # empty output while a required slot is missing -> steer once, then the budget is spent
    d = h["sop.output_supervisor"](_ctx("turn_stopping", snapshot, {"final_text": ""}), st)
    assert d.kind == "steer" and "order_no" in (d.steer_message or "") and d.metadata == {"missing": ["order_no"]}
    assert st.steer_budget == 0
    assert h["sop.output_supervisor"](_ctx("turn_stopping", snapshot, {"final_text": ""}), st).kind == "pass"
    # asking the user IS the right output: never steer when text is present
    st.steer_budget = 1
    assert h["sop.output_supervisor"](_ctx("turn_stopping", snapshot, {"final_text": "请提供订单号"}), st).kind == "pass"
    st.session_slots["order_no"] = "A1"
    assert h["sop.output_supervisor"](_ctx("turn_stopping", snapshot, {"final_text": ""}), st).kind == "pass"

    d = h["handoff.detect"](_ctx("turn_stopping", snapshot, {"final_text": "这个需要转人工处理。"}), st)
    assert d.kind == "modify" and d.handoff == st.handoff_requested
    assert st.handoff_requested["reason"] == "sop_step_declares_handoff" and st.handoff_requested["node_id"] == "n1"
    st.handoff_requested = None
    assert h["handoff.detect"](_ctx("turn_stopping", snapshot, {"final_text": "已完成"}), st).kind == "pass"
    assert st.handoff_requested is None


def test_provider_interaction_default_runs_through_pipeline_host(module, snapshot):
    handlers = module(MODULE_ID).provider.handlers
    events: list[tuple[str, dict]] = []
    host = InteractionPipelineHost(snapshot.hooks, handlers, trace=lambda kind, payload: events.append((kind, payload)))
    st = PipelineState(snapshot=snapshot, memory_context=[{"content": "VIP"}], active_sop_id="sop_1", active_node_id="n1")
    d = host.run("pre_step", _ctx("pre_step", snapshot), st)
    assert isinstance(d, HookDecision) and d.kind == "modify"
    assert [c["source"] for c in d.contexts] == ["persona", "memory", "sop"]
    d = host.run("pre_tool", _ctx("pre_tool", snapshot, {"name": "tool_invoke", "arguments": {"tool_id": "tool_9"}}), st)
    assert d.kind == "deny"
    assert st.tool_calls == 0  # deny short-circuits: capability.pep never ran
    assert [e[0] for e in events].count("hook_decision") >= 2 and not [e for e in events if e[0] == "hook_failed"]


# --------------------------------------------------------------------------- 5. events / PEP

def test_events_interaction_default_hooks_need_no_policy_mapping(guard, security_ctx, module):
    """Unguarded by design: hooks only narrow; the real PEP runs in CapabilityHost with mapped actions."""

    m = module(MODULE_ID).manifest
    assert m.policy_actions == ()
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    with pytest.raises(KeyError):
        mapper.map("hook.contribute/v1")  # not a PEP action, so no host may route it to the PEP
    # every hook the module declares resolves to a registered handler name
    assert {h.handler for h in m.hooks} <= set(DEFAULT_HANDLERS)
    # the guard the enclosing hosts use still fences tenants for the SOP actions the hooks prepare
    from staffdeck_harness.contracts.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        guard(MODULE_ID).require(security_ctx(), "sop.execute/v1", ResourceRef(type="sop", id="sop_1", tenant_id="t2"))
