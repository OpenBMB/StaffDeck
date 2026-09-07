"""Engine-independent StaffDeck planning/reply policies; transport is an injected port."""

from __future__ import annotations
import json
import re
from typing import Any
from staffdeck_harness.contracts.engine_phase import (
    EnginePhaseError,
    PhaseRunner as EnginePhaseRunner,
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


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

    def plan(
        self,
        message: str,
        session: Any,
        available_skills: list[Any],
        model_config: Any,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        task_frame_state: list[dict[str, Any]] | None = None,
        interaction_mode: str = "normal",
        team_context: Any = None,
    ) -> Any:
        from copy import deepcopy

        from pydantic import ValidationError

        from app.core.turn_planner import SCHEMA_REPAIR_ATTEMPTS, compact_validation_errors
        from app.llm import LLMError
        from app.llm.stage_protocol import unified_system_prompt
        from app.session.session_schema import TurnPlan

        payload = self._v2.prepare_payload(
            message,
            session,
            available_skills,
            conversation_context,
            memory_context,
            task_frame_state,
            interaction_mode,
            team_context,
        )
        base_payload = deepcopy(payload)
        next_payload = payload
        plan = None
        for attempt in range(SCHEMA_REPAIR_ATTEMPTS + 1):
            user_text = stage_prompt_text(next_payload)
            try:
                raw_text = self.runner.prompt(
                    phase="plan",
                    model_config=model_config,
                    system_text=unified_system_prompt()
                    + "\n\n只输出一个 JSON object，不要调用任何工具，不要输出解释。",
                    user_text=user_text,
                    engine_session=f"{self.engine_session}-plan{attempt}",
                )
            except EnginePhaseError as exc:
                raise LLMError(
                    f"LLM provider request failed (MODEL_UPSTREAM_UNAVAILABLE); message={exc.detail}"
                ) from exc
            try:
                raw = parse_json_object(raw_text)
            except (ValueError, json.JSONDecodeError) as exc:
                if attempt >= SCHEMA_REPAIR_ATTEMPTS:
                    raise LLMError(f"Turn Planner returned invalid JSON: {exc}") from exc
                next_payload = deepcopy(base_payload)
                next_payload["_schema_repair"] = {
                    "attempt": attempt + 1,
                    "max_attempts": SCHEMA_REPAIR_ATTEMPTS,
                    "previous_output": raw_text[:2000],
                    "validation_errors": [str(exc)],
                    "instruction": "上一轮输出不是合法 JSON object。请只输出一个符合 output_contract 的 JSON object。",
                }
                continue
            try:
                plan = TurnPlan.model_validate(raw)
                break
            except ValidationError as exc:
                if attempt >= SCHEMA_REPAIR_ATTEMPTS:
                    raise LLMError(f"Turn Planner returned invalid JSON schema: {exc}") from exc
                next_payload = deepcopy(base_payload)
                next_payload["_schema_repair"] = {
                    "attempt": attempt + 1,
                    "max_attempts": SCHEMA_REPAIR_ATTEMPTS,
                    "previous_output": raw,
                    "validation_errors": compact_validation_errors(exc),
                    "instruction": "上一轮输出是合法 JSON，但不符合输出字段类型。请保留原任务语义，修正列出的字段后重新输出完整 JSON object。空 object 使用 {}，空 array 使用 []，不要为容器字段输出 null。",
                }
        assert plan is not None
        return self._v2.normalize_plan(
            plan,
            message,
            session,
            available_skills,
            task_frame_state,
            interaction_mode,
            team_context,
        )


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
            text = (
                content
                if isinstance(content, str)
                else "".join(
                    str(i.get("text") or "")
                    for i in content
                    if isinstance(i, dict) and i.get("type") == "text"
                )
                if isinstance(content, list)
                else ""
            )
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

    def _synthesize(
        self,
        message,
        session,
        skill,
        router_decision,
        step_result,
        tool_result,
        model_config,
        persona_prompt,
        memory_context,
        conversation_context,
        task_results,
        *,
        on_text=None,
    ) -> str | None:
        v2 = self._v2
        direct = v2.direct_reply(step_result, tool_result, task_results, router_decision)
        if direct is not None:
            return direct
        from app.llm.stage_protocol import unified_system_prompt

        payload = v2.prepare_payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
            persona_prompt,
        )
        text = self.runner.prompt(
            phase="reply",
            model_config=model_config,
            system_text=unified_system_prompt()
            + "\n\n直接输出给用户的最终回复正文，不要调用任何工具。",
            user_text=stage_prompt_text(payload),
            engine_session=f"{self.engine_session}-reply",
            **({"on_text": on_text} if on_text is not None else {}),
        )
        return v2.normalize_reply(text, session, router_decision, step_result, tool_result, skill)

    def generate(
        self,
        message,
        session,
        skill,
        router_decision,
        step_result,
        tool_result,
        model_config,
        persona_prompt=None,
        memory_context=None,
        conversation_context=None,
        task_results=None,
    ) -> str:
        text = self._synthesize(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )
        return text if text is not None else ""

    def generate_with_stream(self, *args, on_delta=None) -> str:
        """Execute on the owning thread while projecting authorized reply deltas live."""
        from app.core.reply_stream import PublicTextProjection

        projection = PublicTextProjection(on_delta) if on_delta is not None else None
        text = self._synthesize(*args, on_text=projection.feed if projection else None)
        return text if text is not None else ""

    def generate_stream(
        self,
        message,
        session,
        skill,
        router_decision,
        step_result,
        tool_result,
        model_config,
        persona_prompt=None,
        memory_context=None,
        conversation_context=None,
        task_results=None,
    ):
        text = self.generate(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )
        yield from self._v2.chunk_text(text)
