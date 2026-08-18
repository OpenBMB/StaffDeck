from __future__ import annotations

from collections.abc import Callable
from dataclasses import is_dataclass, replace
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app import paths
from app.core.harness_attachments import (
    ValidatedTaskImagePayload,
    isolated_attachment_context,
)
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    TaskExecutionResult,
    TaskRequirement,
)
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation
from app.session.slot_policy import strip_router_generated_message_slots


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "harness_agent_prompt.md"
MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK = 2
ToolInvoker = Callable[[str, dict[str, Any]], dict[str, Any]]
TraceSink = Callable[[str, dict[str, Any]], None]
CancellationCheck = Callable[[], bool]


class HarnessExecutionCancelled(RuntimeError):
    pass


class HarnessExecutionFenced(RuntimeError):
    pass


class HarnessAction(BaseModel):
    action: Literal["tool", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["completed", "awaiting_user", "handoff", "failed"] | None = None
    reply_fragment: str = ""
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    next_step_id: str | None = None
    task_summary: str = ""
    structured_result: Any | None = None


class HarnessTaskAgent:
    """Runs one isolated TaskRequirement without outer conversation messages."""

    def run(
        self,
        requirement: TaskRequirement,
        model_config: ModelConfig,
        invoke_tool: ToolInvoker,
        *,
        max_actions: int = 6,
        trace_sink: TraceSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        image_payloads: list[ValidatedTaskImagePayload] | None = None,
        step_deadline_monotonic: float | None = None,
        step_timeout_seconds: int | None = None,
    ) -> TaskExecutionResult:
        max_actions = max(1, min(int(max_actions), 100))
        transcript: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        evidence_results: list[dict[str, Any]] = []
        capability_results: list[dict[str, Any]] = []
        satisfied_required_knowledge_ids: set[str] = set()
        successful_knowledge_searches = 0
        artifacts: list[dict[str, Any]] = []
        loaded_general_skill_names: list[str] = []
        allowed_names = requirement.capability_manifest.allowed_names()
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()

        for iteration in range(1, max_actions + 1):
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return _step_timeout_result(
                    requirement,
                    action_count=iteration - 1,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                )
            requirement_payload = requirement.model_dump(mode="json")
            attachment_descriptors, attachment_context = isolated_attachment_context(
                requirement.attachments,
                image_payloads,
            )
            requirement_payload["attachments"] = attachment_descriptors
            payload = {
                "task_requirement": requirement_payload,
                "harness_transcript": transcript,
                "iteration": iteration,
                "remaining_actions": max_actions - iteration + 1,
                "knowledge_search_budget": {
                    "maximum_successful_calls": MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK,
                    "successful_calls": successful_knowledge_searches,
                    "remaining_successful_calls": max(
                        0,
                        MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK
                        - successful_knowledge_searches,
                    ),
                },
            }
            if attachment_context is not None:
                payload["conversation_context"] = attachment_context
            try:
                with llm_operation("harness.task_action"):
                    raw = _deadline_llm_client(
                        model_config,
                        step_deadline_monotonic,
                    ).generate_json(
                        system_prompt,
                        payload,
                    )
                try:
                    action = HarnessAction.model_validate(raw)
                except ValidationError:
                    action = _adapt_general_skill_structured_result(
                        raw,
                        loaded_general_skill_names=loaded_general_skill_names,
                    )
                    if action is None:
                        raise
                    if trace_sink:
                        trace_sink(
                            "harness_structured_result_adapted",
                            {
                                "iteration": iteration,
                                "source": loaded_general_skill_names[-1],
                                "result_type": type(raw).__name__,
                            },
                        )
            except (ValidationError, LLMError) as exc:
                if _deadline_expired(step_deadline_monotonic):
                    return _step_timeout_result(
                        requirement,
                        action_count=iteration - 1,
                        timeout_seconds=step_timeout_seconds,
                        capability_results=capability_results,
                        citations=citations,
                        evidence_results=evidence_results,
                        artifacts=artifacts,
                        trace_sink=trace_sink,
                    )
                if trace_sink:
                    trace_sink(
                        "harness_action_failed",
                        {
                            "iteration": iteration,
                            "error": str(exc),
                        },
                    )
                return TaskExecutionResult(
                    task_frame_id=requirement.task_frame_id,
                    status="failed",
                    reply_fragment="当前任务的执行模型没有返回有效动作。",
                    task_summary="Harness 动作解析失败。",
                    capability_results=capability_results,
                    action_count=iteration,
                    error={"code": "HARNESS_ACTION_INVALID", "message": str(exc)},
                )
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return _step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                )

            if trace_sink:
                trace_sink(
                    "harness_action_created",
                    {
                        "iteration": iteration,
                        "action": action.action,
                        "tool_name": action.tool_name,
                    },
                )
            if action.action == "finish":
                missing_capabilities = _missing_required_capabilities(
                    requirement,
                    capability_results,
                    satisfied_required_knowledge_ids,
                )
                if action.status in {None, "completed"} and missing_capabilities:
                    transcript.extend(
                        [
                            {
                                "role": "assistant",
                                "action": "finish",
                                "status": action.status or "completed",
                            },
                            {
                                "role": "tool",
                                "tool_name": "harness_requirement_check",
                                "result": {
                                    "success": False,
                                    "error": {
                                        "code": "REQUIRED_CAPABILITY_NOT_INVOKED",
                                        "message": (
                                            "当前 SOP 节点尚未成功执行强制能力："
                                            + "、".join(missing_capabilities)
                                        ),
                                    },
                                },
                            },
                        ]
                    )
                    if trace_sink:
                        trace_sink(
                            "harness_completion_blocked",
                            {
                                "iteration": iteration,
                                "reason": "required_capability_not_invoked",
                                "missing_capabilities": missing_capabilities,
                            },
                        )
                    continue
                return _finish_result(
                    requirement,
                    action,
                    citations,
                    evidence_results,
                    capability_results,
                    artifacts,
                    action_count=iteration,
                )

            tool_name = str(action.tool_name or "").strip()
            if not tool_name or tool_name not in allowed_names:
                transcript.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "result": {
                            "success": False,
                            "error": {
                                "code": "TOOL_NOT_AVAILABLE",
                                "message": "该能力不在当前 TaskFrame 的冻结清单中。",
                            },
                        },
                    }
                )
                continue

            if (
                tool_name == "knowledge_search"
                and successful_knowledge_searches
                >= MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK
            ):
                result = {
                    "success": False,
                    "error": {
                        "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
                        "message": (
                            "当前 TaskFrame 已完成两次有效知识检索。请使用已有证据完成"
                            "原始需求；不要扩展相邻主题或继续改写同义查询。"
                        ),
                    },
                }
            else:
                try:
                    _raise_if_cancelled(is_cancelled)
                    result = invoke_tool(tool_name, dict(action.arguments or {}))
                    _raise_if_cancelled(is_cancelled)
                except (HarnessExecutionCancelled, HarnessExecutionFenced):
                    raise
                except Exception as exc:
                    result = {
                        "success": False,
                        "error": {
                            "code": "HARNESS_TOOL_ERROR",
                            "message": str(exc),
                        },
                    }
            bounded_result = _bounded_capability_result(tool_name, result)
            if _is_loaded_general_skill_result(tool_name, result):
                loaded_general_skill_names.append(tool_name)
            transcript.extend(
                [
                    {
                        "role": "assistant",
                        "action": "tool",
                        "tool_name": tool_name,
                        "arguments": action.arguments,
                    },
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "result": bounded_result,
                    },
                ]
            )
            if tool_name not in {"capability_search", "capability_describe"}:
                capability_results.append(bounded_result)
            if _deadline_expired(step_deadline_monotonic):
                return _step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                )
            activated_names = _activate_described_capabilities(
                requirement,
                tool_name,
                result,
            )
            allowed_names.update(activated_names)
            _extend_dict_list(artifacts, result.get("artifacts"))
            if tool_name == "knowledge_search" and bool(result.get("success")):
                if _has_usable_knowledge_evidence(result):
                    successful_knowledge_searches += 1
                requested_knowledge_ids = _string_list(
                    (action.arguments or {}).get("knowledge_base_ids")
                )
                satisfied_required_knowledge_ids.update(
                    requested_knowledge_ids or requirement.required_knowledge_base_ids
                )
            if (
                tool_name == "knowledge_search"
                and bool(result.get("success"))
                and isinstance(result.get("data"), dict)
            ):
                _extend_dict_list(citations, result.get("citations"))
                evidence_results.append(dict(result["data"]))
            else:
                _extend_dict_list(citations, result.get("citations"))
            if trace_sink:
                trace_sink(
                    "harness_tool_completed",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "success": bool(result.get("success")),
                        "error": result.get("error"),
                        "result": _trace_capability_result(
                            tool_name,
                            result,
                        ),
                    },
                )
        return TaskExecutionResult(
            task_frame_id=requirement.task_frame_id,
            status="action_budget",
            reply_fragment="当前任务已达到本轮自动执行上限，需要下一轮继续。",
            citations=citations,
            evidence_results=evidence_results,
            capability_results=capability_results,
            artifacts=artifacts,
            task_summary="Harness 达到 action budget。",
            action_count=max_actions,
            error={"code": "ACTION_BUDGET_EXHAUSTED"},
        )


def _activate_described_capabilities(
    requirement: TaskRequirement,
    tool_name: str,
    result: dict[str, Any],
) -> set[str]:
    if tool_name != "capability_describe" or result.get("success") is not True:
        return set()
    data = result.get("data")
    if (
        not isinstance(data, dict)
        or str(data.get("snapshot_revision") or "")
        != requirement.capability_manifest.snapshot_revision
    ):
        return set()
    raw_descriptors = data.get("activated_capabilities")
    if not isinstance(raw_descriptors, list):
        return set()
    existing = {item.name: item for item in requirement.capability_manifest.available}
    activated: set[str] = set()
    for raw in raw_descriptors:
        if not isinstance(raw, dict):
            continue
        try:
            descriptor = CapabilityDescriptor.model_validate(raw)
        except ValidationError:
            continue
        if not descriptor.available or descriptor.kind == "internal":
            continue
        existing[descriptor.name] = descriptor
        activated.add(descriptor.name)
    requirement.capability_manifest.available = list(existing.values())
    return activated


def _is_loaded_general_skill_result(
    tool_name: str,
    result: dict[str, Any],
) -> bool:
    data = result.get("data")
    return (
        tool_name.startswith("general_skill.")
        and result.get("success") is True
        and isinstance(data, dict)
        and data.get("kind") == "general_skill"
        and data.get("operation") == "read"
    )


def _adapt_general_skill_structured_result(
    raw: object,
    *,
    loaded_general_skill_names: list[str],
) -> HarnessAction | None:
    """Turn an instruction-only Skill's bare business JSON into a safe finish action.

    Skill authors describe the business output contract, not the Harness control
    protocol.  The adapter is deliberately gated on a successfully loaded
    GeneralSkill and never accepts an object that attempted to emit an invalid
    Harness action.  Consequently an RFC/MCP-shaped object is returned as data;
    it is not interpreted or executed as a tool call.
    """

    if not loaded_general_skill_names or not isinstance(raw, (dict, list)):
        return None
    if isinstance(raw, dict) and "action" in raw:
        return None
    reply_fragment = json.dumps(
        raw,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return HarnessAction(
        action="finish",
        status="completed",
        reply_fragment=reply_fragment,
        task_summary=(
            f"{loaded_general_skill_names[-1]} 已生成结构化业务结果。"
        ),
        structured_result=raw,
    )


def _missing_required_capabilities(
    requirement: TaskRequirement,
    capability_results: list[dict[str, Any]],
    satisfied_required_knowledge_ids: set[str],
) -> list[str]:
    succeeded = {
        str(item.get("tool_name") or "")
        for item in capability_results
        if isinstance(item, dict) and item.get("success") is True
    }
    missing = [name for name in requirement.required_capability_names if name not in succeeded]
    for knowledge_base_id in requirement.required_knowledge_base_ids:
        if knowledge_base_id not in satisfied_required_knowledge_ids:
            missing.append(f"knowledge_search:{knowledge_base_id}")
    return missing


def _has_usable_knowledge_evidence(result: dict[str, Any]) -> bool:
    citations = result.get("citations")
    if isinstance(citations, list) and any(isinstance(item, dict) for item in citations):
        return True
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    evidence = data.get("evidence_pack")
    return isinstance(evidence, list) and any(isinstance(item, dict) for item in evidence)


def _finish_result(
    requirement: TaskRequirement,
    action: HarnessAction,
    citations: list[dict[str, Any]],
    evidence_results: list[dict[str, Any]],
    capability_results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    action_count: int,
) -> TaskExecutionResult:
    status = action.status or "completed"
    allowed_next_steps = {
        str(item.get("next_node_id") or "").strip()
        for item in requirement.allowed_transitions
        if isinstance(item, dict) and item.get("next_node_id")
    }
    next_step_id = str(action.next_step_id or "").strip() or None
    if next_step_id and next_step_id not in allowed_next_steps:
        next_step_id = None
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status=status,
        reply_fragment=action.reply_fragment.strip(),
        slot_updates=strip_router_generated_message_slots(action.slot_updates),
        next_step_id=next_step_id,
        citations=citations,
        evidence_results=evidence_results,
        capability_results=capability_results,
        artifacts=artifacts,
        task_summary=action.task_summary.strip(),
        action_count=action_count,
        structured_result=action.structured_result,
    )


def _extend_dict_list(target: list[dict[str, Any]], value: object) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            target.append(item)


def _raise_if_cancelled(check: CancellationCheck | None) -> None:
    if check is not None and check():
        raise HarnessExecutionCancelled("Harness execution was cancelled.")


def _deadline_expired(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


def _deadline_llm_client(
    model_config: ModelConfig,
    deadline_monotonic: float | None,
) -> LLMClient:
    if deadline_monotonic is None:
        return LLMClient(model_config)
    remaining = max(deadline_monotonic - time.monotonic(), 0.1)
    configured = getattr(model_config, "timeout_seconds", None)
    timeout_seconds = min(float(configured), remaining) if configured else remaining
    if is_dataclass(model_config):
        limited_config = replace(model_config, timeout_seconds=timeout_seconds)
    else:
        limited_config = model_config.model_copy(
            update={"timeout_seconds": timeout_seconds}
        )
    return LLMClient(limited_config)


def _step_timeout_result(
    requirement: TaskRequirement,
    *,
    action_count: int,
    timeout_seconds: int | None,
    capability_results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    evidence_results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    trace_sink: TraceSink | None,
) -> TaskExecutionResult:
    limit_text = f"{timeout_seconds} 秒" if timeout_seconds else "配置的时间"
    error = {
        "code": "SOP_STEP_TIMEOUT",
        "message": f"当前 SOP 单步运行超过 {limit_text}，已停止继续执行。",
        "timeout_seconds": timeout_seconds,
    }
    if trace_sink:
        trace_sink(
            "harness_step_timeout",
            {
                "timeout_seconds": timeout_seconds,
                "action_count": max(0, action_count),
                "error": error,
            },
        )
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status="failed",
        reply_fragment=error["message"],
        citations=citations,
        evidence_results=evidence_results,
        capability_results=capability_results,
        artifacts=artifacts,
        task_summary="SOP 单步运行超时。",
        action_count=max(0, action_count),
        error=error,
    )


def _bounded_capability_result(
    tool_name: str,
    result: dict[str, Any],
    *,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    payload = {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "data": result.get("data"),
        "error": result.get("error"),
    }
    if isinstance(result.get("mcp_app"), dict):
        payload["mcp_app"] = result["mcp_app"]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= max_chars:
        return payload
    truncated = {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "truncated": True,
        "preview": serialized[:max_chars],
        "error": result.get("error"),
    }
    if isinstance(result.get("mcp_app"), dict):
        truncated["mcp_app"] = result["mcp_app"]
    return truncated


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _trace_capability_result(
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    trace_result = dict(result)
    data = trace_result.get("data")
    if tool_name.startswith("general_skill.") and isinstance(data, dict):
        trace_result["data"] = {
            key: data.get(key)
            for key in (
                "kind",
                "slug",
                "operation",
                "reply",
                "structured_result",
            )
            if data.get(key) not in (None, "", [], {})
        }
    return _bounded_capability_result(
        tool_name,
        trace_result,
        max_chars=4_000,
    )
