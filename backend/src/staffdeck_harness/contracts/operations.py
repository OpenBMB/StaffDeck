"""One contract catalog shared by composition, registry, PEP and generic proxy dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .manifest import SlotName


@dataclass(frozen=True)
class OperationContract:
    operation: str
    resource_type: str
    action: str
    slot: SlotName = SlotName.SOP_SLOT_ACTION
    parameters: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    description: str = ""
    side_effecting: bool = False


SUPPORTED_CONTRACTS = {
    name: {"v1"}
    for name in (
        "knowledge.search",
        "general_skill.consume",
        "tool.invoke",
        "mcp.invoke",
        "a2a.invoke",
        "sandbox.execute",
        "artifact.publish",
        "handoff.request",
        "handoff.assign",
        "handoff.reply",
        "sop.execute",
        "channel.receive",
        "channel.send",
        "memory.read",
        "memory.write",
        "team.delegate",
        "model.use",
        "runtime.turn",
        "capability.describe",
        "task.finish",
        "event.observe",
        "hook.contribute",
        "knowledge.import",
        "notification.send",
    )
}
