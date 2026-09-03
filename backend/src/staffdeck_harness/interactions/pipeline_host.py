"""InteractionPipelineHost: compiles and runs the fixed hook plan around one DSH step.

The host is kernel (K). Modules contribute handlers by name; the host resolves
names to callables from its registry, runs them in the compiled order, and
merges decisions most-restrictive-first. Every point receives an immutable
``HookContext`` built from the same ``CompositionSnapshot``.

Default handlers (registered here, overridable per deployment):

    pre_step       persona · memory.recall · sop.execution_slice
    pre_tool       activation.allowlist · capability.pep
    post_tool      ledger.record · citations.collect
    turn_stopping  sop.output_supervisor · handoff.detect
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from staffdeck_harness.composition.compiler import CompositionSnapshot, HookPlan
from staffdeck_harness.contracts.hooks import HookContext, HookDecision, merge_decisions
from staffdeck_harness.contracts.manifest import HookPoint
from staffdeck_harness.interactions.sop_adapter import build_slice

Handler = Callable[[HookContext, "PipelineState"], HookDecision]


@dataclass
class PipelineState:
    """Mutable per-turn scratch shared by handlers (never persisted as-is)."""

    snapshot: CompositionSnapshot
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    session_slots: dict[str, Any] = field(default_factory=dict)
    active_sop_id: str | None = None
    active_node_id: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    steer_budget: int = 1
    handoff_requested: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- default handlers

def _persona(ctx: HookContext, st: PipelineState) -> HookDecision:
    if ctx.step != 1 or not st.snapshot.persona:
        return HookDecision.passthrough()
    return HookDecision(kind="modify", contexts=({"type": "text", "text": st.snapshot.persona, "source": "persona"},))


def _memory_recall(ctx: HookContext, st: PipelineState) -> HookDecision:
    if ctx.step != 1 or not st.memory_context:
        return HookDecision.passthrough()
    lines = ["# 关于该用户的已知记忆"]
    for m in st.memory_context[:20]:
        content = str(m.get("content") or "").strip()
        if content:
            lines.append(f"- [{m.get('kind', 'note')}] {content}")
    return HookDecision(kind="modify", contexts=({"type": "text", "text": "\n".join(lines), "source": "memory"},))


def _sop_slice(ctx: HookContext, st: PipelineState) -> HookDecision:
    if not st.active_sop_id:
        return HookDecision.passthrough()
    slice_ = build_slice(st.snapshot, st.active_sop_id, st.active_node_id, st.session_slots)
    if slice_ is None:
        return HookDecision.passthrough()
    st.extras["execution_slice"] = slice_
    return HookDecision(kind="modify", contexts=({"type": "text", "text": slice_.as_prompt(), "source": "sop"},), metadata={"skill_id": slice_.skill_id, "node_id": slice_.node_id})


def _activation_allowlist(ctx: HookContext, st: PipelineState) -> HookDecision:
    """Snapshot guard: only narrows. The CapabilityHost enforces the real PEP."""

    call = ctx.payload
    name = str(call.get("name") or "")
    allowed = st.snapshot.allowed_resource_ids(sop_id=st.active_sop_id, node_id=st.active_node_id)
    if name == "tool_invoke":
        tid = str((call.get("arguments") or {}).get("tool_id") or "")
        if tid and tid not in allowed.get("tool", set()):
            return HookDecision.deny(f"tool {tid} is not activated for this step")
    if name == "general_skill_read":
        sid = str((call.get("arguments") or {}).get("skill_id") or "")
        if sid and sid not in allowed.get("general_skill", set()):
            return HookDecision.deny(f"skill {sid} is not activated for this step")
    if name == "knowledge_search" and not allowed.get("knowledge_base"):
        return HookDecision.deny("no knowledge base is activated for this step")
    return HookDecision.passthrough()


def _capability_pep(ctx: HookContext, st: PipelineState) -> HookDecision:
    # The PEP itself runs inside CapabilityHost.invoke (it needs the live row).
    # This hook only records intent so a denied call is visible in the trace.
    st.tool_calls += 1
    return HookDecision.passthrough()


def _ledger_record(ctx: HookContext, st: PipelineState) -> HookDecision:
    receipt = ctx.payload.get("receipt")
    if isinstance(receipt, Mapping):
        st.receipts.append(dict(receipt))
    return HookDecision.passthrough()


def _citations_collect(ctx: HookContext, st: PipelineState) -> HookDecision:
    for c in ctx.payload.get("citations") or ():
        if isinstance(c, Mapping):
            st.citations.append(dict(c))
    return HookDecision.passthrough()


def _output_supervisor(ctx: HookContext, st: PipelineState) -> HookDecision:
    """SOP completeness check at the natural stop boundary."""

    slice_ = st.extras.get("execution_slice")
    if slice_ is None:
        return HookDecision.passthrough()
    final_text = str(ctx.payload.get("final_text") or "")
    missing = [f for f in slice_.expected_user_info if not st.session_slots.get(f)]
    # Allow the model to ask the user for missing slots — do not steer; asking IS the correct output.
    if missing and not final_text.strip():
        if st.steer_budget > 0:
            st.steer_budget -= 1
            return HookDecision(kind="steer", steer_message="当前步骤还缺少信息：" + "、".join(missing) + "。请向用户提问以收集这些信息。", metadata={"missing": missing})
    return HookDecision.passthrough()


def _handoff_detect(ctx: HookContext, st: PipelineState) -> HookDecision:
    final_text = str(ctx.payload.get("final_text") or "")
    slice_ = st.extras.get("execution_slice")
    declares = bool(slice_ and slice_.declares_handoff)
    markers = ("转人工", "转接人工", "人工处理", "请人工", "[HANDOFF]")
    if declares and any(m in final_text for m in markers):
        st.handoff_requested = {"reason": "sop_step_declares_handoff", "node_id": slice_.node_id if slice_ else None, "text": final_text[:500]}
        return HookDecision(kind="modify", handoff=st.handoff_requested)
    return HookDecision.passthrough()


DEFAULT_HANDLERS: dict[str, Handler] = {
    "persona": _persona,
    "memory.recall": _memory_recall,
    "sop.execution_slice": _sop_slice,
    "activation.allowlist": _activation_allowlist,
    "capability.pep": _capability_pep,
    "ledger.record": _ledger_record,
    "citations.collect": _citations_collect,
    "sop.output_supervisor": _output_supervisor,
    "handoff.detect": _handoff_detect,
}


class InteractionPipelineHost:
    def __init__(self, plan: HookPlan, handlers: Mapping[str, Handler] | None = None, *, trace: Callable[[str, dict[str, Any]], None] | None = None):
        self.plan = plan
        self.handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.trace = trace
        missing = [c.handler for hs in plan.order.values() for c in hs if c.handler not in self.handlers]
        if missing:
            raise LookupError(f"hook handlers not registered: {sorted(set(missing))}")

    def run(self, point: HookPoint, ctx: HookContext, state: PipelineState) -> HookDecision:
        decisions: list[HookDecision] = []
        for contribution in self.plan.order.get(point, ()):
            handler = self.handlers[contribution.handler]
            try:
                d = handler(ctx, state)
            except Exception as exc:  # a broken hook must not kill the turn; log and pass
                if self.trace:
                    self.trace("hook_failed", {"point": point, "handler": contribution.handler, "error": str(exc)})
                continue
            if d.kind != "pass" and self.trace:
                self.trace("hook_decision", {"point": point, "handler": contribution.handler, "kind": d.kind, "reason": d.reason})
            decisions.append(d)
            if d.kind == "deny":
                break  # nothing later can resurrect a denial
        return merge_decisions(decisions)
