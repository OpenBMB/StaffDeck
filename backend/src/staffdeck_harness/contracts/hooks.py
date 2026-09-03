"""Hook envelope contracts for the InteractionPipelineHost.

Hooks run at four fixed points around one Harness v3 step. Modules *contribute*
handlers; they never own the plan. Every handler receives a ``HookContext``
snapshot and returns a ``HookDecision``. Decisions are merged
most-restrictive-first (deny > steer > modify > pass) exactly like the engine's own
interception surface so a later handler cannot resurrect a denial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from staffdeck_harness.contracts.manifest import HookPoint

JsonObject = Mapping[str, Any]

DecisionKind = Literal["pass", "modify", "steer", "deny"]


@dataclass(frozen=True)
class HookContext:
    point: HookPoint
    tenant_id: str
    agent_id: str
    session_id: str
    turn_id: str
    step: int
    snapshot_id: str
    payload: JsonObject                  # point-specific: messages, tool call, tool result, stop
    frame: JsonObject = field(default_factory=dict)   # active TaskFrame projection
    sop: JsonObject = field(default_factory=dict)     # active SOP ExecutionSlice
    generation: int = 0


@dataclass(frozen=True)
class HookDecision:
    kind: DecisionKind = "pass"
    reason: str | None = None
    # pre_step: extra context blocks to prepend; post_tool: replacement content
    contexts: tuple[JsonObject, ...] = ()
    replacement: JsonObject | None = None
    # turn_stopping: steer content for one more step
    steer_message: str | None = None
    handoff: JsonObject | None = None    # request a human handoff after the loop
    metadata: JsonObject = field(default_factory=dict)

    @classmethod
    def passthrough(cls) -> "HookDecision":
        return cls()

    @classmethod
    def deny(cls, reason: str) -> "HookDecision":
        return cls(kind="deny", reason=reason)


_RANK = {"pass": 0, "modify": 1, "steer": 2, "deny": 3}


def merge_decisions(decisions: list[HookDecision]) -> HookDecision:
    """Most restrictive wins; contexts accumulate in handler order."""

    if not decisions:
        return HookDecision.passthrough()
    contexts: list[JsonObject] = []
    replacement: JsonObject | None = None
    steer: str | None = None
    handoff: JsonObject | None = None
    winner = decisions[0]
    for d in decisions:
        contexts.extend(d.contexts)
        if d.replacement is not None:
            replacement = d.replacement
        if d.steer_message and steer is None:
            steer = d.steer_message
        if d.handoff is not None and handoff is None:
            handoff = d.handoff
        if _RANK[d.kind] > _RANK[winner.kind]:
            winner = d
    return HookDecision(
        kind=winner.kind,
        reason=winner.reason,
        contexts=tuple(contexts),
        replacement=replacement,
        steer_message=steer,
        handoff=handoff,
        metadata=dict(winner.metadata),
    )
