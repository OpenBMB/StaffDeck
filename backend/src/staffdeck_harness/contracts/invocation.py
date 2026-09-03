"""Invocation, result, receipt and cancellation contracts.

A ``ModuleInvocation`` is the only way anything (DSH via the Bridge, the legacy
engine, a scheduler) asks a capability module to do work. The
``CapabilityHost`` executes it, the Invocation Ledger records it, and a
``Receipt`` comes back alongside the ``ModuleResult``.

Idempotency is expressed once here: ``side_effect_key`` is the logical action
identity (tenant + frame + step + module + canonical arguments). The ledger
enforces exactly-once semantics on it and surfaces ``outcome_unknown`` when a
write may have happened but no terminal outcome was recorded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

JsonObject = Mapping[str, Any]

InvocationStatus = Literal["started", "completed", "failed", "outcome_unknown", "cancelled", "denied"]


@dataclass(frozen=True)
class InvocationContext:
    """Trusted context stamped by the host. Modules never construct this."""

    tenant_id: str
    agent_id: str
    user_id: str
    session_id: str
    turn_id: str
    channel: str
    task_frame_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None
    snapshot_id: str | None = None
    trace_id: str | None = None
    deadline_at: datetime | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        for name in ("tenant_id", "agent_id", "user_id", "session_id", "turn_id", "channel"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"InvocationContext requires non-empty {name}")
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")


@dataclass(frozen=True)
class ModuleInvocation:
    invocation_id: str
    module_id: str
    operation: str                # e.g. ``knowledge.search/v1``
    arguments: JsonObject
    context: InvocationContext
    binding_id: str | None = None
    side_effecting: bool = False
    idempotency_key_fields: tuple[str, ...] = ()
    metadata: JsonObject = field(default_factory=dict)
    # A side-effecting call may still be non-replayable (the tool disables idempotency). The
    # distinction matters: ``side_effecting`` controls outcome tracking (an ambiguous failure is
    # ``outcome_unknown``), while ``replayable`` controls replay/dedupe against a prior key.
    replayable: bool = True

    def canonical_arguments(self) -> str:
        args = self.arguments
        if self.idempotency_key_fields:
            args = {k: self.arguments.get(k) for k in sorted(self.idempotency_key_fields)}
        return json.dumps(args, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)

    def request_digest(self) -> str:
        payload = f"{self.module_id}|{self.operation}|{self.canonical_arguments()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def side_effect_key(self) -> str | None:
        if not self.side_effecting or not self.replayable:
            return None
        ctx = self.context
        payload = "|".join(
            [
                ctx.tenant_id,
                ctx.task_frame_id or ctx.session_id,
                ctx.step_id or "",
                self.module_id,
                self.operation,
                self.canonical_arguments(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Receipt:
    invocation_id: str
    status: InvocationStatus
    request_digest: str
    side_effect_key: str | None = None
    replayed_from: str | None = None
    ledger_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: JsonObject | None = None


@dataclass(frozen=True)
class ModuleResult:
    success: bool
    data: Any = None
    error: JsonObject | None = None
    citations: tuple[JsonObject, ...] = ()
    artifacts: tuple[JsonObject, ...] = ()
    extensions: JsonObject = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any = None, **kw: Any) -> "ModuleResult":
        return cls(success=True, data=data, **kw)

    @classmethod
    def fail(cls, code: str, message: str, **kw: Any) -> "ModuleResult":
        return cls(success=False, error={"code": code, "message": message}, **kw)


@dataclass(frozen=True)
class CancelCommand:
    session_id: str
    turn_id: str | None = None
    reason: str = "user_cancelled"
    requested_by: str | None = None
