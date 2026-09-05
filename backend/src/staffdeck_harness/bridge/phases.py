"""Harness v3 owns the whole turn: planning, every step, and the reply on one engine process.

Before this module, ``HarnessV3Engine`` inherited the v2 turn and swapped only the step
executor: the turn plan and the final reply were still produced by the v2 model stages
(``TurnPlanner`` / ``ResponseGenerator``), so one user message paid two model round-trips on
two different code paths, and the review called Harness v3 "a step executor, not the agent
loop". These phase runners move both onto the engine:

- ``PlanPhase``  — the v2 planner prompt/contract, sent as one engine prompt with **no tools
  bound**; the engine's assistant text is parsed and validated by the very same
  ``TurnPlan`` model and ``TurnPlanner._normalize`` the v2 path uses, so downstream
  (TaskFrame store, SOP CAS, router decision) sees an identical plan.
- ``ReplyPhase`` — the v2 response-synthesis payload sent the same way; only used when the
  turn produced more than one TaskFrame (a lone frame's ``finish_task`` reply is final, exactly
  like v2's ``_single_task_reply`` short-cut).

Both phases share the turn's pooled process (see ``process_pool``): the activation token is
rebound to a *phase host* whose ``model_config`` answers the model gateway but whose
``tool_schemas()`` is empty and whose ``invoke_proxy`` refuses, so a planning prompt can never
reach a capability. Everything else about the v2 turn (claim, leases, TaskFrame scheduling,
SOP CAS, handoff, memory capture) is untouched.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.engine_phase import EnginePhaseError
from staffdeck_harness.runtime.model_phases import (  # noqa: F401
    EngineTurnPlanner, EngineResponseGenerator, parse_json_object, stage_prompt_text,
)

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, dict[str, Any]], None]


@dataclass
class PhaseHost:
    """A phase binding: the model gateway works, capability calls are refused (tools stay listed)."""

    model_config: Any
    trace: TraceSink | None = None
    phase: str = "plan"
    idle: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def tool_schemas(self) -> list[dict[str, Any]]:
        from staffdeck_harness.capabilities.host import proxy_tool_schemas

        return proxy_tool_schemas()

    def invoke_proxy(self, proxy_name: str, arguments: Any, ctx: Any) -> tuple[ModuleResult, None]:
        hint = "请直接输出符合 output_contract 的 JSON object，不要调用工具。" if self.phase == "plan" else "请直接输出回复正文，不要调用工具。"
        return ModuleResult.fail("ACTIVATION_FENCED", f"{self.phase} 阶段不能调用能力。{hint}"), None


class EnginePhaseRunner:
    """Run one tool-less prompt on the turn's engine process and return the assistant text."""

    def __init__(self, runtime: Any, pooled: Any, *, tenant_id: str, session_id: str, trace: TraceSink, cancelled: Callable[[], bool]):
        self.runtime = runtime
        self.pooled = pooled
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.trace = trace
        self.cancelled = cancelled

    def prompt(self, *, phase: str, model_config: Any, system_text: str, user_text: str, engine_session: str) -> str:
        from staffdeck_harness.bridge.task_agent import IdlePhaseHost

        host = PhaseHost(model_config=model_config, trace=self.trace, phase=phase)
        self.runtime.registry.rebind(self.pooled.token, host, None)
        started = time.monotonic()
        self.trace(f"harness_v3_{phase}_started", {"engine_session": engine_session})
        try:
            text = f"{system_text.rstrip()}\n\n{user_text}"
            from staffdeck_harness.bridge.session_runner import run_session

            events, final_text, finish_reason = run_session(self.pooled.process, engine_session, [{"type": "text", "text": text}], self.cancelled, self.trace, tenant_id=self.tenant_id, host_session_id=self.session_id, timeout_seconds=getattr(getattr(self.runtime, "worker_config", None), "request_timeout_seconds", 600) or 600)
            self.trace(f"harness_v3_{phase}_finished", {"engine_session": engine_session, "finish_reason": finish_reason, "duration_ms": int((time.monotonic() - started) * 1000), "events": len(events)})
            if finish_reason == "error" or (not final_text.strip() and finish_reason not in (None, "completed", "end_turn", "stop")):
                # The engine ended the turn on an error (typically the model provider); say so
                # instead of reporting "empty output" and letting a schema-repair retry mask it.
                raise EnginePhaseError(phase, _turn_error(events))
            return final_text
        finally:
            try:
                self.runtime.registry.rebind(self.pooled.token, IdlePhaseHost(), None)
            except KeyError:
                pass  # forced process shutdown already released this activation



def _turn_error(events: list[dict[str, Any]]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "turn/end":
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            err = reason.get("error") if isinstance(reason.get("error"), dict) else {}
            msg = str(err.get("message") or reason.get("kind") or "").strip()
            code = str(err.get("code") or "").strip()
            return f"{msg} ({code})" if code else (msg or "unknown")
    return "unknown"


@dataclass
class _TurnStub:
    tenant_id: str
    session_id: str


# --------------------------------------------------------------------------- planning

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
