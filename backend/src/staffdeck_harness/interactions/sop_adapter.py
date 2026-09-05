"""SOP Interaction Adapter: turns a SOP execution position into an ExecutionSlice.

The slice is what the model sees about the SOP for one step: the current node,
its instruction, expected slots, the allowed transitions, and the logical
capability slots resolved for this Staff. It is derived from the immutable
``CompositionSnapshot`` (never from the live Skill row), so pre-loop, in-loop
and post-loop all observe the same SOP content.

Post-loop, the registered ``SopRuntimePort.after_execution`` applies the result.
This input adapter does not advance state. Both execution engines consume the same
SOP module; existing transaction/CAS storage remains owned by the scheduling kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from staffdeck_harness.composition.compiler import CompositionSnapshot, SopExecutionPlan


@dataclass(frozen=True)
class ExecutionSlice:
    skill_id: str
    skill_name: str
    version: str
    node_id: str | None
    node: Mapping[str, Any]
    transitions: tuple[Mapping[str, Any], ...]
    slots: Mapping[str, Any]                  # current session slots
    expected_user_info: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    resolved_slots: tuple[Mapping[str, Any], ...]  # logical slot -> resource for this staff
    is_terminal: bool
    declares_handoff: bool
    sub_sop_id: str | None
    goal: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_prompt(self) -> str:
        lines = [f"# 当前 SOP：{self.skill_name}（v{self.version}）"]
        if self.node_id:
            lines.append(f"## 当前步骤：{self.node.get('name') or self.node_id}")
        instruction = str(self.node.get("instruction") or "").strip()
        if instruction:
            lines.append(instruction)
        if self.expected_user_info:
            lines.append("需要收集的信息：" + "、".join(self.expected_user_info))
        if self.slots:
            filled = [f"{k}={v}" for k, v in self.slots.items() if v not in (None, "", [], {})]
            if filled:
                lines.append("已知信息：" + "；".join(filled))
        if self.resolved_slots:
            lines.append("本步骤可用能力：")
            for s in self.resolved_slots:
                lines.append(f"- {s['slot']} → {s['resource_type']}:{s['resource_id']}（{s['operation']}）")
        if self.transitions:
            lines.append("可选的下一步：")
            for t in self.transitions:
                cond = f"，条件：{t['condition']}" if t.get("condition") else ""
                lines.append(f"- → {t.get('next_node_id')}{cond}")
        if self.declares_handoff:
            lines.append("本步骤允许转交人工处理；当需要人工介入时，明确说明并停止。")
        if self.is_terminal:
            lines.append("这是终止步骤；完成后给出最终答复。")
        return "\n".join(lines)


def _node(plan: SopExecutionPlan, node_id: str | None) -> tuple[str | None, Mapping[str, Any]]:
    content = plan.content
    resolved = str(node_id or content.get("start_node_id") or "").strip()
    for node in content.get("nodes") or content.get("steps") or []:
        if isinstance(node, Mapping):
            nid = str(node.get("node_id") or node.get("step_id") or "").strip()
            if nid and nid == resolved:
                return nid, node
    return (resolved or None), {}


def _transitions(plan: SopExecutionPlan, node_id: str | None) -> tuple[Mapping[str, Any], ...]:
    if not node_id:
        return ()
    out = []
    for edge in plan.content.get("edges") or []:
        if isinstance(edge, Mapping) and str(edge.get("source_node_id") or "") == node_id:
            out.append({k: edge.get(k) for k in ("next_node_id", "condition", "label", "priority") if edge.get(k) not in (None, "")})
    return tuple(sorted(out, key=lambda e: -int(e.get("priority") or 0)))


def build_slice(snapshot: CompositionSnapshot, skill_id: str, node_id: str | None, session_slots: Mapping[str, Any]) -> ExecutionSlice | None:
    plan = snapshot.sop(skill_id)
    if plan is None:
        return None
    nid, node = _node(plan, node_id)
    terminal_ids = {str(t) for t in (plan.content.get("terminal_node_ids") or [])}
    resolved = tuple(
        {"slot": s.declaration.name, "operation": s.declaration.operation, "resource_type": s.resource_type, "resource_id": s.resource_id, "required": s.declaration.required}
        for s in plan.resolved_slots
        if s.declaration.node_id in (None, nid)
    )
    declares_handoff = bool(node.get("type") in {"human_handoff", "handoff"} or node.get("assignee_user_id") or "handoff" in (node.get("allowed_actions") or []))
    return ExecutionSlice(
        skill_id=plan.skill_id,
        skill_name=plan.name,
        version=plan.version,
        node_id=nid,
        node=dict(node),
        transitions=_transitions(plan, nid),
        slots=dict(session_slots or {}),
        expected_user_info=tuple(str(x) for x in (node.get("expected_user_info") or [])),
        allowed_actions=tuple(str(x) for x in (node.get("allowed_actions") or [])),
        resolved_slots=resolved,
        is_terminal=bool(nid and nid in terminal_ids),
        declares_handoff=declares_handoff,
        sub_sop_id=str(node.get("sub_sop_id") or "") or None,
        goal=f"完成 {plan.name} 的{node.get('name') or '当前步骤'}。",
    )
