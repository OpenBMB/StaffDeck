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

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from staffdeck_harness.contracts.invocation import ModuleResult

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
        from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent, IdlePhaseHost

        host = PhaseHost(model_config=model_config, trace=self.trace, phase=phase)
        self.runtime.registry.rebind(self.pooled.token, host, None)
        started = time.monotonic()
        self.trace(f"harness_v3_{phase}_started", {"engine_session": engine_session})
        try:
            text = f"{system_text.rstrip()}\n\n{user_text}"
            runner = HarnessV3TaskAgent.__new__(HarnessV3TaskAgent)  # borrow the event loop without a turn context
            runner.turn = _TurnStub(self.tenant_id, self.session_id)
            events, final_text, finish_reason = runner._run_engine_turn(self.pooled.process, engine_session, [{"type": "text", "text": text}], self.cancelled, self.trace)
            self.trace(f"harness_v3_{phase}_finished", {"engine_session": engine_session, "finish_reason": finish_reason, "duration_ms": int((time.monotonic() - started) * 1000), "events": len(events)})
            return final_text
        finally:
            self.runtime.registry.rebind(self.pooled.token, IdlePhaseHost(), None)


@dataclass
class _TurnStub:
    tenant_id: str
    session_id: str


# --------------------------------------------------------------------------- planning

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """Tolerant JSON extraction: fenced block, or the outermost ``{...}`` in the text."""

    text = (text or "").strip()
    if not text:
        raise ValueError("empty model output")
    m = _JSON_FENCE.search(text)
    candidate = m.group(1) if m else text
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


class EngineTurnPlanner:
    """Drop-in for ``app.core.turn_planner.TurnPlanner`` whose model call runs on the engine.

    Same prompt file, same ``stage_payload`` framing, same ``TurnPlan`` validation and the same
    ``_normalize`` post-processing as v2; only the transport differs (engine prompt instead of
    ``LLMClient.generate_json``). One schema-repair retry, like v2.
    """

    def __init__(self, runner: EnginePhaseRunner, *, engine_session: str):
        from app.core.turn_planner import TurnPlanner

        self._v2 = TurnPlanner()
        self.runner = runner
        self.engine_session = engine_session

    def plan(self, message: str, session: Any, available_skills: list[Any], model_config: Any, conversation_context: dict[str, object] | None = None, memory_context: list[dict[str, object]] | None = None, task_frame_state: list[dict[str, Any]] | None = None, interaction_mode: str = "normal", team_context: Any = None) -> Any:
        from copy import deepcopy

        from pydantic import ValidationError

        from app.core.turn_planner import PROMPT_PATH, SCHEMA_REPAIR_ATTEMPTS, _compact_validation_errors, _session_payload, _sop_payload
        from app.llm import LLMError
        from app.llm.stage_protocol import TURN_PLANNER_OUTPUT_SCHEMA, stage_payload, unified_system_prompt
        from app.core.context_projection import compact_conversation_context
        from app.session.session_schema import TurnPlan

        payload = stage_payload(
            phase="TurnPlanner",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data={
                "current_session": _session_payload(session, task_frame_state=task_frame_state),
                "available_sops": [_sop_payload(skill) for skill in available_skills],
                "interaction_mode": interaction_mode,
                "team_context": (team_context.model_dump(mode="json") if team_context is not None else None),
            },
            output_contract=TURN_PLANNER_OUTPUT_SCHEMA,
        )
        base_payload = deepcopy(payload)
        next_payload = payload
        plan = None
        for attempt in range(SCHEMA_REPAIR_ATTEMPTS + 1):
            user_text = stage_prompt_text(next_payload)
            raw_text = self.runner.prompt(phase="plan", model_config=model_config, system_text=unified_system_prompt() + "\n\n只输出一个 JSON object，不要调用任何工具，不要输出解释。", user_text=user_text, engine_session=f"{self.engine_session}-plan{attempt}")
            try:
                raw = parse_json_object(raw_text)
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt >= SCHEMA_REPAIR_ATTEMPTS:
                    raise LLMError(f"Turn Planner returned invalid JSON: {exc}") from exc
                next_payload = deepcopy(base_payload)
                next_payload["_schema_repair"] = {"attempt": attempt + 1, "max_attempts": SCHEMA_REPAIR_ATTEMPTS, "previous_output": raw_text[:2000], "validation_errors": [str(exc)], "instruction": "上一轮输出不是合法 JSON object。请只输出一个符合 output_contract 的 JSON object。"}
                continue
            try:
                plan = TurnPlan.model_validate(raw)
                break
            except ValidationError as exc:
                if attempt >= SCHEMA_REPAIR_ATTEMPTS:
                    raise LLMError(f"Turn Planner returned invalid JSON schema: {exc}") from exc
                next_payload = deepcopy(base_payload)
                next_payload["_schema_repair"] = {"attempt": attempt + 1, "max_attempts": SCHEMA_REPAIR_ATTEMPTS, "previous_output": raw, "validation_errors": _compact_validation_errors(exc), "instruction": "上一轮输出是合法 JSON，但不符合输出字段类型。请保留原任务语义，修正列出的字段后重新输出完整 JSON object。空 object 使用 {}，空 array 使用 []，不要为容器字段输出 null。"}
        assert plan is not None
        return self._v2._normalize(plan, message, session, available_skills, task_frame_state, interaction_mode, team_context)


def _as_text(rendered: Any) -> str:
    if isinstance(rendered, str):
        return rendered
    if isinstance(rendered, list):
        return "\n".join(str(p.get("text") if isinstance(p, dict) else p) for p in rendered)
    return json.dumps(rendered, ensure_ascii=False)


def stage_prompt_text(payload: dict[str, Any]) -> str:
    """The v2 stage protocol as one engine prompt: prior conversation turns (text only, what
    ``LLMClient._prepare_stage_user_input`` would send as history) followed by the rendered stage
    input. Kept as text because the engine session is fresh per phase and carries no history."""

    from app.llm.stage_protocol import render_stage_user_message

    history: list[str] = []
    context = payload.get("conversation_context")
    try:
        from app.llm.client import _project_messages_from_context  # noqa: PLC0415 - legacy helper, read-only

        for m in _project_messages_from_context(context):
            role = str(m.get("role") or "")
            content = m.get("content")
            text = content if isinstance(content, str) else "".join(str(i.get("text") or "") for i in content if isinstance(i, dict) and i.get("type") == "text") if isinstance(content, list) else ""
            if role in {"user", "assistant"} and text.strip():
                history.append(f"[{'用户' if role == 'user' else '助手'}] {text.strip()}")
    except Exception:  # noqa: BLE001 - history is best-effort; the stage input still carries the message
        history = []
    body = _as_text(render_stage_user_message(payload))
    if history:
        return "# 对话历史\n" + "\n".join(history[-40:]) + "\n\n# 本轮阶段输入\n" + body
    return body


# --------------------------------------------------------------------------- reply

class EngineResponseGenerator:
    """Drop-in for the owner's ``ResponseGenerator`` whose synthesis call runs on the engine.

    Delegates every non-model decision (direct step reply, tool-failure reply, clarify
    short-cut, payload projection) to the v2 generator so the reply policy is shared; only the
    ``generate``/``generate_stream`` model call moves to the pooled process.
    """

    def __init__(self, v2: Any, runner: EnginePhaseRunner, *, engine_session: str):
        self._v2 = v2
        self.runner = runner
        self.engine_session = engine_session

    def __getattr__(self, name: str) -> Any:  # chunk_text and every helper stay v2's
        return getattr(self._v2, name)

    def _synthesize(self, message, session, skill, router_decision, step_result, tool_result, model_config, persona_prompt, memory_context, conversation_context, task_results) -> str | None:
        v2 = self._v2
        if v2._can_use_step_reply_directly(step_result, tool_result, task_results):
            return (step_result.reply or "").strip()
        from app.core.response_generator import tool_failure_reply
        from app.llm.stage_protocol import unified_system_prompt

        if tool_result and not tool_result.success and not task_results:
            return tool_failure_reply(tool_result)
        if router_decision.decision == "clarify" and step_result.reply:
            return step_result.reply
        raw_payload = v2._payload(message, session, skill, router_decision, step_result, tool_result, memory_context, conversation_context, task_results)
        payload = v2._stage_payload(raw_payload, persona_prompt)
        text = self.runner.prompt(phase="reply", model_config=model_config, system_text=unified_system_prompt() + "\n\n直接输出给用户的最终回复正文，不要调用任何工具。", user_text=stage_prompt_text(payload), engine_session=f"{self.engine_session}-reply")
        reply = text.strip() or (step_result.reply or "") or v2._minimal_fallback(router_decision)
        return v2._visible_reply_or_fallback(reply, session, router_decision, step_result, tool_result, skill)

    def generate(self, message, session, skill, router_decision, step_result, tool_result, model_config, persona_prompt=None, memory_context=None, conversation_context=None, task_results=None) -> str:
        text = self._synthesize(message, session, skill, router_decision, step_result, tool_result, model_config, persona_prompt, memory_context, conversation_context, task_results)
        return text if text is not None else ""

    def generate_stream(self, message, session, skill, router_decision, step_result, tool_result, model_config, persona_prompt=None, memory_context=None, conversation_context=None, task_results=None):
        text = self.generate(message, session, skill, router_decision, step_result, tool_result, model_config, persona_prompt, memory_context, conversation_context, task_results)
        yield from self._v2.chunk_text(text)
