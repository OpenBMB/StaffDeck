from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app import paths
from app.core.harness_attachments import (
    ValidatedTaskImagePayload,
    isolated_attachment_context,
)
from app.core.task_request_compiler import TaskExecutionResult, TaskRequirement
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation
from app.session.slot_policy import strip_router_generated_message_slots


PROMPT_PATH = (
    paths.resource_dir() / "app" / "llm" / "prompts" / "harness_agent_prompt.md"
)
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
    ) -> TaskExecutionResult:
        max_actions = max(1, min(int(max_actions), 20))
        transcript: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        evidence_results: list[dict[str, Any]] = []
        capability_results: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        allowed_names = requirement.capability_manifest.allowed_names()
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()

        for iteration in range(1, max_actions + 1):
            _raise_if_cancelled(is_cancelled)
            requirement_payload = requirement.model_dump(mode="json")
            attachment_descriptors, attachment_context = (
                isolated_attachment_context(
                    requirement.attachments,
                    image_payloads,
                )
            )
            requirement_payload["attachments"] = attachment_descriptors
            payload = {
                "task_requirement": requirement_payload,
                "harness_transcript": transcript,
                "iteration": iteration,
                "remaining_actions": max_actions - iteration + 1,
            }
            if attachment_context is not None:
                payload["conversation_context"] = attachment_context
            try:
                with llm_operation("harness.task_action"):
                    raw = LLMClient(model_config).generate_json(
                        system_prompt,
                        payload,
                    )
                action = HarnessAction.model_validate(raw)
            except (ValidationError, LLMError) as exc:
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
                        "result": _bounded_capability_result(
                            tool_name,
                            result,
                        ),
                    },
                ]
            )
            capability_results.append(
                _bounded_capability_result(tool_name, result)
            )
            _extend_dict_list(citations, result.get("citations"))
            _extend_dict_list(artifacts, result.get("artifacts"))
            if tool_name == "knowledge_search" and isinstance(result.get("data"), dict):
                evidence_results.append(dict(result["data"]))
            if trace_sink:
                trace_sink(
                    "harness_tool_completed",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "success": bool(result.get("success")),
                        "error": result.get("error"),
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
    )


def _extend_dict_list(
    target: list[dict[str, Any]], value: object
) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            target.append(item)


def _raise_if_cancelled(check: CancellationCheck | None) -> None:
    if check is not None and check():
        raise HarnessExecutionCancelled("Harness execution was cancelled.")


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
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= max_chars:
        return payload
    return {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "truncated": True,
        "preview": serialized[:max_chars],
        "error": result.get("error"),
    }
