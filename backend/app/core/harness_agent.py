from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app import paths
from app.core.acp import AcpConfig, AcpEngine, AcpError
from app.core.acp.pricing import RealUsageMeter, TokenMeter
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
from app.llm.client import latest_llm_usage_observation
from app.observability.spans import llm_operation
from app.session.slot_policy import strip_router_generated_message_slots

logger = logging.getLogger(__name__)

PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "harness_agent_prompt.md"
MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK = 2
ToolInvoker = Callable[[str, dict[str, Any]], dict[str, Any]]
TraceSink = Callable[[str, dict[str, Any]], None]
CancellationCheck = Callable[[], bool]

ACP_CAPABILITY_NAMES = frozenset(
    {"acp_compress", "acp_decompress", "acp_search_context", "acp_status"}
)
# Mirror the session-layer originals cap so the task-level checkpoint state
# cannot grow unboundedly with compression count either.
ACP_CHECKPOINT_ORIGINALS_CAP = 5
# compress/decompress mutate the transcript in place (summary entry replaces
# the compressed range / restored entries replace the summary entry), so the
# regular assistant+tool append is skipped for them.
_TRANSCRIPT_SYNCED_ACP_CAPABILITIES = frozenset({"acp_compress", "acp_decompress"})


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
    # Optional ACP (Active Context Pruning) operations attached by the model
    # alongside a normal action. Backward-compatible extension: absent for
    # every existing protocol consumer, so no protocol break.
    acp_ops: dict[str, Any] | None = None


class HarnessTaskAgent:
    """Runs one isolated TaskRequirement without outer conversation messages."""

    def __init__(self, context_compression_mode: str = "legacy") -> None:
        self._context_compression_mode = context_compression_mode

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
        checkpoint: dict[str, Any] | None = None,
        context_compression_mode: str | None = None,
        acp_config: AcpConfig | None = None,
        session_id: str | None = None,
        frame_kind: str | None = None,
        session_acp_nudge: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        max_actions = max(1, min(int(max_actions), 100))
        checkpoint = dict(checkpoint or {})
        current_step_id = _requirement_step_id(requirement)
        same_frame = (
            str(checkpoint.get("task_frame_id") or "") == requirement.task_frame_id
        )
        same_step = (
            same_frame
            and str(checkpoint.get("step_id") or "") == current_step_id
        )
        transcript = _dict_items(checkpoint.get("transcript")) if same_frame else []
        # 从暂停恢复时，把用户的最新回复显式写进内层对话记录：仅靠
        # requirement.source_user_message 字段太不显眼，实测模型会忽略它
        # 而重复上一轮的提问（真实案例：确认暂停点反复索要确认）。
        latest_user_message = str(requirement.source_user_message or "").strip()
        if same_frame and transcript and latest_user_message:
            previous_user_entry = next(
                (
                    str(item.get("content") or "")
                    for item in reversed(transcript)
                    if item.get("role") == "user"
                ),
                None,
            )
            if previous_user_entry != latest_user_message:
                transcript.append({"role": "user", "content": latest_user_message})
        citations = _dict_items(checkpoint.get("citations")) if same_frame else []
        evidence_results = (
            _dict_items(checkpoint.get("evidence_results")) if same_frame else []
        )
        capability_results = (
            _dict_items(checkpoint.get("capability_results")) if same_step else []
        )
        satisfied_required_knowledge_ids = set(
            _string_list(checkpoint.get("satisfied_required_knowledge_ids"))
            if same_step
            else []
        )
        successful_knowledge_searches = (
            int(checkpoint.get("successful_knowledge_searches") or 0)
            if same_step
            else 0
        )
        artifacts = _dict_items(checkpoint.get("artifacts")) if same_frame else []
        loaded_general_skill_names = (
            _string_list(checkpoint.get("loaded_general_skill_names"))
            if same_frame
            else []
        )
        recent_task_summaries = _string_list(
            checkpoint.get("recent_task_summaries")
        )[-8:]
        acp_mode = (context_compression_mode or self._context_compression_mode) == "acp"
        acp_meter = (
            RealUsageMeter(
                usage_source=lambda: latest_llm_usage_observation(session_id)
            )
            if acp_mode
            else None
        )
        acp_engine = (
            _acp_engine_for_transcript(
                transcript,
                checkpoint.get("acp_state"),
                meter=acp_meter,
                config=acp_config,
            )
            if acp_mode
            else None
        )
        # A non-retryable failure only blocks an identical call inside this
        # invocation of the AgentLoop.  Persisting the signature in the
        # checkpoint made a later user turn inherit an obsolete failure even
        # after its inputs or external state had changed.
        non_retryable_action_signatures: set[str] = set()
        # 强制能力从未尝试就 finish(failed) 只拦一次，避免与固执模型互相死锁。
        failed_finish_without_attempt_blocked = False
        allowed_names = requirement.capability_manifest.allowed_names()
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        pending_actions: list[HarnessAction] = []
        collected_acp_ops: list[dict[str, Any]] = []

        def finish(result: TaskExecutionResult) -> TaskExecutionResult:
            summary = " ".join(
                str(result.task_summary or result.reply_fragment or "").split()
            ).strip()
            if (
                result.status != "action_budget"
                and summary
                and (
                    not recent_task_summaries
                    or recent_task_summaries[-1] != summary
                )
            ):
                recent_task_summaries.append(summary[:1_000])
            result.loop_checkpoint = {
                "version": 1,
                "task_frame_id": requirement.task_frame_id,
                "step_id": current_step_id,
                "transcript": _transcript_for_model(transcript, acp_mode=acp_mode),
                "citations": citations[-20:],
                "evidence_results": evidence_results[-10:],
                "capability_results": capability_results[-20:],
                "satisfied_required_knowledge_ids": sorted(
                    satisfied_required_knowledge_ids
                ),
                "successful_knowledge_searches": successful_knowledge_searches,
                "artifacts": artifacts[-20:],
                "loaded_general_skill_names": loaded_general_skill_names[-20:],
                "recent_task_summaries": recent_task_summaries[-8:],
            }
            if acp_mode and acp_engine is not None:
                result.loop_checkpoint["acp_state"] = _serialize_acp_engine_state(
                    acp_engine
                )
            # Task-layer acp_ops target the task transcript's sequence layout;
            # the session layer reads the same general:{session_id} frame for
            # conversation frames, so exporting them there would mis-index the
            # session engine. Only non-conversation (task) frames export ops.
            if collected_acp_ops and frame_kind != "conversation":
                result.loop_checkpoint["acp_ops"] = list(collected_acp_ops)
            return result

        for iteration in range(1, max_actions + 1):
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration - 1,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))
            requirement_payload = requirement.model_dump(mode="json")
            attachment_descriptors, attachment_context = isolated_attachment_context(
                requirement.attachments,
                image_payloads,
            )
            requirement_payload["attachments"] = attachment_descriptors
            payload = {
                "task_requirement": requirement_payload,
                "harness_transcript": _transcript_for_model(transcript),
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
            if recent_task_summaries:
                payload["agent_loop_memory"] = {
                    "recent_task_summaries": list(recent_task_summaries),
                }
            if acp_mode and acp_engine is not None and acp_meter is not None:
                nudge = _acp_task_nudge(acp_engine, acp_meter, transcript)
                if nudge is not None:
                    payload["acp_nudge"] = nudge
            if session_acp_nudge:
                payload["session_acp_nudge"] = dict(session_acp_nudge)
            if attachment_context is not None:
                payload["conversation_context"] = attachment_context
            try:
                action: HarnessAction | None = None
                validation_error: Exception | None = None
                if pending_actions:
                    action = pending_actions.pop(0)
                else:
                    for protocol_attempt in range(2):
                        # Persist a stable link between this LLM span and the Harness
                        # iteration that consumes it.  Timing projections must not
                        # infer this relationship from overlapping wall-clock windows.
                        with llm_operation(
                            "harness.task_action",
                            task_frame_id=requirement.task_frame_id,
                            iteration=iteration,
                            protocol_attempt=protocol_attempt + 1,
                        ):
                            client = _deadline_llm_client(
                                model_config,
                                step_deadline_monotonic,
                                session_id=session_id,
                            )
                            raw = _generate_harness_action_json(
                                client,
                                system_prompt,
                                payload,
                            )
                        try:
                            actions = _harness_actions_from_raw(raw)
                            action = actions[0]
                            pending_actions.extend(actions[1:])
                            if len(actions) > 1 and trace_sink:
                                trace_sink(
                                    "harness_action_sequence_accepted",
                                    {
                                        "iteration": iteration,
                                        "action_count": len(actions),
                                        "actions": [item.action for item in actions],
                                    },
                                )
                        except (ValidationError, ValueError) as exc:
                            action = _adapt_general_skill_structured_result(
                                raw,
                                loaded_general_skill_names=loaded_general_skill_names,
                            )
                            if action is not None:
                                if trace_sink:
                                    trace_sink(
                                        "harness_structured_result_adapted",
                                        {
                                            "iteration": iteration,
                                            "source": loaded_general_skill_names[-1],
                                            "result_type": type(raw).__name__,
                                        },
                                    )
                                break
                            validation_error = exc
                            if protocol_attempt == 0:
                                payload = {
                                    **payload,
                                    "protocol_repair": {
                                        "message": (
                                            "上一次输出不符合 HarnessAction Schema。请只修正动作"
                                            "协议，不要改变任务意图。"
                                        ),
                                        "invalid_output": raw,
                                        "validation_error": str(exc),
                                        "required_tool_envelope": {
                                            "action": "tool",
                                            "tool_name": (
                                                "capability_manifest 中的已授权能力名称"
                                            ),
                                            "arguments": {},
                                        },
                                        "required_finish_envelope": {
                                            "action": "finish",
                                            "status": "completed | awaiting_user | handoff | failed",
                                        },
                                    },
                                }
                                if trace_sink:
                                    trace_sink(
                                        "harness_action_repair_requested",
                                        {
                                            "iteration": iteration,
                                            "error": str(exc),
                                        },
                                    )
                                continue
                            raise
                        break
                if action is None:
                    if validation_error is not None:
                        raise validation_error
                    raise RuntimeError("Harness action generation returned no action.")
            except (ValidationError, ValueError, LLMError) as exc:
                if _deadline_expired(step_deadline_monotonic):
                    return finish(_step_timeout_result(
                        requirement,
                        action_count=iteration - 1,
                        timeout_seconds=step_timeout_seconds,
                        capability_results=capability_results,
                        citations=citations,
                        evidence_results=evidence_results,
                        artifacts=artifacts,
                        trace_sink=trace_sink,
                    ))
                if trace_sink:
                    trace_sink(
                        "harness_action_failed",
                        {
                            "iteration": iteration,
                            "error": str(exc),
                        },
                    )
                return finish(TaskExecutionResult(
                    task_frame_id=requirement.task_frame_id,
                    status="failed",
                    reply_fragment="当前任务的执行模型没有返回有效动作。",
                    task_summary="Harness 动作解析失败。",
                    capability_results=capability_results,
                    action_count=iteration,
                    error={"code": "HARNESS_ACTION_INVALID", "message": str(exc)},
                ))
            if action is not None and isinstance(action.acp_ops, dict) and action.acp_ops:
                collected_acp_ops.append(action.acp_ops)
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))

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
                # 真实案例：提交节点的模型带着上一步骤的工具报错直接
                # finish(failed)，全程没在本节点尝试过强制能力。失败结论
                # 必须建立在真实尝试之上，先打回一次要求实际调用。
                if (
                    action.status == "failed"
                    and missing_capabilities
                    and not failed_finish_without_attempt_blocked
                ):
                    attempted = {
                        str(item.get("tool_name") or "")
                        for item in capability_results
                        if isinstance(item, dict)
                    }
                    unattempted = [
                        name
                        for name in missing_capabilities
                        if not name.startswith("knowledge_search:")
                        and name not in attempted
                    ]
                    if unattempted:
                        failed_finish_without_attempt_blocked = True
                        transcript.extend(
                            [
                                {
                                    "role": "assistant",
                                    "action": "finish",
                                    "status": "failed",
                                },
                                {
                                    "role": "tool",
                                    "tool_name": "harness_requirement_check",
                                    "result": {
                                        "success": False,
                                        "error": {
                                            "code": (
                                                "REQUIRED_CAPABILITY_NOT_ATTEMPTED"
                                            ),
                                            "message": (
                                                "当前 SOP 节点的强制能力在本节点内"
                                                "尚未尝试调用，不能直接宣告失败："
                                                + "、".join(unattempted)
                                                + "。请先实际调用，再依据其真实"
                                                "结果决定完成或失败。"
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
                                    "reason": "required_capability_not_attempted",
                                    "missing_capabilities": unattempted,
                                },
                            )
                        continue
                return finish(_finish_result(
                    requirement,
                    action,
                    citations,
                    evidence_results,
                    capability_results,
                    artifacts,
                    action_count=iteration,
                ))

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
                action_signature = _action_signature(
                    tool_name,
                    dict(action.arguments or {}),
                )
                if action_signature in non_retryable_action_signatures:
                    result = {
                        "success": False,
                        "error": {
                            "code": "NON_RETRYABLE_ACTION_REPEATED",
                            "message": (
                                "相同工具与参数此前已失败且不可重试，本次未再次执行。"
                                "请根据前一次错误更换工具或参数，或结束当前任务。"
                            ),
                            "retryable": False,
                        },
                    }
                    if trace_sink:
                        trace_sink(
                            "harness_action_failed",
                            {
                                "iteration": iteration,
                                "tool_name": tool_name,
                                "error": {
                                    "code": "NON_RETRYABLE_ACTION_REPEATED",
                                    "message": (
                                        "模型重复提交了已标记为不可重试的相同工具调用；"
                                        "调用未执行，AgentLoop 将继续重新规划。"
                                    ),
                                    "retryable": False,
                                },
                            },
                        )
                else:
                    try:
                        _raise_if_cancelled(is_cancelled)
                        if acp_mode and tool_name in ACP_CAPABILITY_NAMES:
                            assert acp_engine is not None
                            result = _invoke_acp_capability(
                                acp_engine,
                                tool_name,
                                dict(action.arguments or {}),
                                transcript,
                            )
                        else:
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
                    if _is_non_retryable_failure(result):
                        non_retryable_action_signatures.add(action_signature)
            bounded_result = _bounded_capability_result(tool_name, result)
            result_data = result.get("data")
            if (
                result.get("success") is True
                and isinstance(result_data, dict)
                and result_data.get("detached") is True
            ):
                capability_results.append(bounded_result)
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
                reply = str(result_data.get("user_reply") or "").strip()
                return finish(TaskExecutionResult(
                    task_frame_id=requirement.task_frame_id,
                    status="waiting_external_task",
                    reply_fragment=reply,
                    capability_results=capability_results,
                    action_count=iteration,
                    task_summary=(
                        "异步业务任务已受理，等待完成后恢复 SOP。"
                        if requirement.kind == "sop"
                        else "异步业务任务已受理，可通过任务号查询进度。"
                    ),
                    structured_result={
                        "task_id": result_data.get("task_id"),
                        "status": result_data.get("status"),
                    },
                ))
            if _is_loaded_general_skill_result(tool_name, result):
                loaded_general_skill_names.append(tool_name)
            if (
                acp_mode
                and tool_name in _TRANSCRIPT_SYNCED_ACP_CAPABILITIES
                and result.get("success") is True
            ):
                # compress/decompress already replaced the compressed range
                # with the summary entry (or restored the original entries)
                # inside the kernel dispatch, keeping transcript and block
                # store in sync; nothing is appended here.
                pass
            else:
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
                if acp_mode and acp_engine is not None:
                    _append_transcript_blocks(acp_engine, transcript[-2:])
            if tool_name not in {"capability_search", "capability_describe"}:
                capability_results.append(bounded_result)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))
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
        return finish(TaskExecutionResult(
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
        ))


def _requirement_step_id(requirement: TaskRequirement) -> str:
    step = requirement.sop_context.get("step")
    if not isinstance(step, dict):
        return ""
    for key in ("step_id", "node_id", "id"):
        value = str(step.get(key) or "").strip()
        if value:
            return value
    return ""


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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


def _action_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _is_non_retryable_failure(result: object) -> bool:
    if not isinstance(result, dict) or result.get("success") is not False:
        return False
    error = result.get("error")
    return isinstance(error, dict) and error.get("retryable") is False


def _generate_harness_action_json(
    client: LLMClient,
    system_prompt: str,
    payload: dict[str, Any],
) -> Any:
    """Use sequence-aware parsing when the concrete client supports it."""

    sequence_generator = getattr(client, "generate_json_sequence", None)
    if callable(sequence_generator):
        return sequence_generator(system_prompt, payload)
    # Test doubles and older compatible clients still expose the original method.
    return client.generate_json(system_prompt, payload)


def _harness_actions_from_raw(raw: object) -> list[HarnessAction]:
    """Normalize one action or an ordered action sequence from a provider response."""

    items: object = raw
    if isinstance(raw, dict) and set(raw) == {"actions"}:
        items = raw.get("actions")
    if not isinstance(items, list):
        return [HarnessAction.model_validate(_sanitize_acp_ops(items))]
    if not items:
        raise ValueError("Harness action sequence must not be empty.")
    actions = [HarnessAction.model_validate(_sanitize_acp_ops(item)) for item in items]
    if any(action.action == "finish" for action in actions[:-1]):
        raise ValueError("A finish action must be the final item in an action sequence.")
    return actions


def _sanitize_acp_ops(item: object) -> object:
    """Drop malformed acp_ops so a bad op never fails the turn."""
    if not isinstance(item, dict) or "acp_ops" not in item:
        return item
    if isinstance(item["acp_ops"], dict):
        return item
    logger.warning(
        "ignoring malformed acp_ops (expected dict, got %s)",
        type(item["acp_ops"]).__name__,
    )
    sanitized = dict(item)
    sanitized.pop("acp_ops", None)
    return sanitized


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
    if isinstance(raw, dict) and ({"action", "actions"} & set(raw)):
        return None
    if isinstance(raw, list) and any(
        isinstance(item, dict) and "action" in item for item in raw
    ):
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
    step = requirement.sop_context.get("step") if requirement.sop_context else None
    step_type = str(step.get("type") or "").strip() if isinstance(step, dict) else ""
    allowed_next_steps = {
        str(item.get("next_node_id") or "").strip()
        for item in requirement.allowed_transitions
        if isinstance(item, dict) and item.get("next_node_id")
    }
    next_step_id = str(action.next_step_id or "").strip() or None
    if next_step_id and next_step_id not in allowed_next_steps:
        next_step_id = None
    # Merely allowing the optional handoff_human action must not turn an otherwise
    # successful SOP step into a handoff. A dedicated terminal handoff node owns
    # the routing decision, so normalize model failure/completion wording to the
    # durable handoff state instead of telling the user that routing is unavailable.
    if step_type == "handoff" and next_step_id is None:
        status = "handoff"
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
    session_id: str | None = None,
) -> LLMClient:
    if deadline_monotonic is None:
        return LLMClient(model_config, session_id=session_id)
    remaining = max(deadline_monotonic - time.monotonic(), 0.1)
    configured = getattr(model_config, "timeout_seconds", None)
    timeout_seconds = min(float(configured), remaining) if configured else remaining
    if is_dataclass(model_config):
        limited_config = replace(model_config, timeout_seconds=timeout_seconds)
    else:
        limited_config = model_config.model_copy(
            update={"timeout_seconds": timeout_seconds}
        )
    return LLMClient(limited_config, session_id=session_id)


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


def _transcript_for_model(
    transcript: list[dict[str, Any]],
    *,
    keep_recent_entries: int = 6,
    acp_mode: bool = False,
) -> list[dict[str, Any]]:
    """Project persistent tool history into a bounded execution context.

    Tool results stay in the durable invocation/trace records.  The model only
    needs the newest interactions plus stable receipts for older operations.
    GeneralSkill package instructions are retained because they define the
    workflow the current AgentLoop is following.

    Under ACP the model drives compression itself via acp_compress, so the
    full transcript is projected and the legacy receipt/cap path is bypassed.
    """

    if acp_mode:
        return [dict(entry) for entry in transcript]

    cutoff = max(0, len(transcript) - keep_recent_entries)
    latest_skill_instruction_index: dict[str, int] = {}
    for index, entry in enumerate(transcript):
        if _is_general_skill_instruction_entry(entry):
            latest_skill_instruction_index[str(entry.get("tool_name") or "")] = index

    projected: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(transcript):
        copied = dict(entry)
        skill_name = str(copied.get("tool_name") or "")
        is_latest_skill_instruction = (
            _is_general_skill_instruction_entry(copied)
            and latest_skill_instruction_index.get(skill_name) == index
        )
        if _is_general_skill_instruction_entry(copied) and not is_latest_skill_instruction:
            continue
        if (
            copied.get("role") == "assistant"
            and skill_name.startswith("general_skill.")
            and latest_skill_instruction_index.get(skill_name) != index + 1
        ):
            continue
        if index >= cutoff or is_latest_skill_instruction:
            projected.append((index, copied))
            continue
        if copied.get("role") == "tool" and isinstance(copied.get("result"), dict):
            result = dict(copied["result"])
            receipt: dict[str, Any] = {
                key: result.get(key)
                for key in ("tool_name", "success", "error", "truncated")
                if key in result
            }
            data = result.get("data")
            if isinstance(data, dict):
                receipt["data"] = {
                    key: data.get(key)
                    for key in (
                        "path",
                        "source_path",
                        "destination_path",
                        "sha256",
                        "size",
                        "offset",
                        "next_offset",
                        "continuation_token",
                        "eof",
                        "status",
                        "exit_code",
                    )
                    if data.get(key) is not None
                }
            receipt["history_receipt"] = _history_receipt(result)
            copied["result"] = receipt
        elif copied.get("role") == "assistant":
            arguments = copied.get("arguments")
            if isinstance(arguments, dict):
                copied["arguments"] = _compact_old_arguments(arguments)
        projected.append((index, copied))

    if len(projected) <= 40:
        return [entry for _index, entry in projected]

    # A long-lived non-SOP AgentLoop may exceed the transcript entry cap. Keep
    # the latest loaded instruction for every Skill, then spend the remaining
    # budget on the newest ordinary interactions.
    essential_indexes = {
        index
        for index, entry in projected
        if _is_general_skill_instruction_entry(entry)
    }
    remaining = max(0, 40 - len(essential_indexes))
    ordinary_indexes = [
        index for index, _entry in projected if index not in essential_indexes
    ][-remaining:]
    selected_indexes = essential_indexes | set(ordinary_indexes)
    return [
        entry for index, entry in projected if index in selected_indexes
    ]


def _is_general_skill_instruction_entry(entry: dict[str, Any]) -> bool:
    if entry.get("role") != "tool" or not str(entry.get("tool_name") or "").startswith(
        "general_skill."
    ):
        return False
    result = entry.get("result")
    return isinstance(result, dict) and bool(result.get("success"))


def _acp_engine_for_transcript(
    transcript: list[dict[str, Any]],
    acp_state: object,
    meter: TokenMeter | None = None,
    config: AcpConfig | None = None,
) -> AcpEngine:
    """Build the task-level kernel, restoring persisted state when possible.

    The kernel is framework-agnostic and has no persistence API, so the task
    wrapper restores its internal stores directly (same format as the session
    layer). Block ids are preserved so model-issued decompress references stay
    valid across turns. A malformed or missing state falls back to seeding the
    engine from the transcript; compressed content is then unrecoverable but
    the task never crashes.
    """
    engine = AcpEngine(config=config, meter=meter)
    if isinstance(acp_state, dict):
        _restore_acp_engine_state(engine, acp_state)
    if len(engine.blocks()) != len(transcript):
        engine = AcpEngine(config=config, meter=meter)
        _append_transcript_blocks(engine, transcript)
    return engine


def _acp_task_nudge(
    engine: AcpEngine,
    meter: RealUsageMeter,
    transcript: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Evaluate task-context pressure and build the advisory nudge payload.

    Real usage (the meter source) wins when available; otherwise the
    pre-check estimate of the serialized transcript is used and flagged.
    The nudge is advisory only — the model decides whether to compress.
    """
    pressure_tokens = meter.latest_usage_tokens
    estimated = pressure_tokens is None
    if pressure_tokens is None:
        pressure_tokens = meter.estimate_tokens(
            json.dumps(transcript, ensure_ascii=False, default=str)
        )
    recommendation = engine.nudge(pressure_tokens)
    if recommendation is None:
        return None
    hint = (
        "如之前已压缩过任务记录，可先查看 acp_status 并复用仍有效的压缩块"
        "（acp_decompress / acp_search_context 可找回细节）。"
    )
    return {
        "level": recommendation.level,
        "message": f"{recommendation.message}\n{hint}",
        "usage_pct": recommendation.usage_pct,
        "current_tokens": recommendation.current_tokens,
        "limit_tokens": recommendation.limit_tokens,
        "estimated": estimated,
    }


def _restore_acp_engine_state(engine: AcpEngine, acp_state: dict[str, Any]) -> None:
    """Rebuild the task-level kernel from the persisted acp sub-state."""
    engine.from_state(acp_state)


def _serialize_acp_engine_state(engine: AcpEngine) -> dict[str, Any]:
    """Persist the task-level kernel into the loop checkpoint.

    Every block content is the serialized transcript entry itself, so no
    separate roles/ingested-id bookkeeping is needed (unlike the session
    layer). Checkpoint originals are capped like the session layer so the
    checkpoint state cannot grow unboundedly with compression count.
    """
    return engine.to_state(max_originals=ACP_CHECKPOINT_ORIGINALS_CAP)


def _append_transcript_blocks(
    engine: AcpEngine,
    entries: list[dict[str, Any]],
) -> None:
    for entry in entries:
        engine.add_message(
            f"t{len(engine.blocks())}",
            json.dumps(entry, ensure_ascii=False, default=str),
        )


def _invoke_acp_capability(
    engine: AcpEngine,
    tool_name: str,
    arguments: dict[str, Any],
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch one ACP tool call to the task-level kernel.

    compress/decompress keep the transcript in sync with the kernel block
    store: the compressed range is replaced by a summary entry (or the
    summary entry is replaced by the restored original entries). Kernel
    errors surface as structured capability errors the model can see.
    """
    if tool_name == "acp_compress":
        seq_start = _acp_int_argument(arguments, "seq_start")
        seq_end = _acp_int_argument(arguments, "seq_end")
        summary = str(arguments.get("summary") or "").strip()
        if seq_start is None or seq_end is None or not summary:
            return _acp_failure(
                "INVALID_ARGUMENTS",
                "acp_compress 需要整数 seq_start/seq_end 与非空 summary。",
            )
        result = engine.compress(seq_start, seq_end, summary)
        if isinstance(result, AcpError):
            return _acp_failure(result.code, result.message, result.detail)
        transcript[seq_start : seq_end + 1] = [
            {
                "role": "tool",
                "tool_name": "acp_compress",
                "result": {
                    "success": True,
                    "data": {
                        "summary": summary,
                        "summary_block_id": result.summary_block_id,
                        "checkpoint_id": result.checkpoint_id,
                        "tier": result.tier,
                        "removed_block_ids": list(result.removed_block_ids),
                        "token_delta": result.token_delta,
                        "ledger_balance": result.ledger_balance,
                    },
                },
            }
        ]
        return {"success": True, "data": asdict(result)}
    if tool_name == "acp_decompress":
        block_id = _acp_int_argument(arguments, "block_id")
        if block_id is None:
            return _acp_failure("INVALID_ARGUMENTS", "acp_decompress 需要整数 block_id。")
        position = _block_position(engine, block_id)
        if position is None:
            return _acp_failure(
                "BLOCK_NOT_FOUND",
                f"block {block_id} 不在当前压缩账本中。",
                {"block_id": block_id},
            )
        result = engine.decompress(block_id)
        if isinstance(result, AcpError):
            return _acp_failure(result.code, result.message, result.detail)
        restored = engine.blocks()[position : position + len(result.restored_block_ids)]
        transcript[position : position + 1] = [
            _deserialize_transcript_entry(block.content) for block in restored
        ]
        return {"success": True, "data": asdict(result)}
    if tool_name == "acp_search_context":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _acp_failure("INVALID_ARGUMENTS", "acp_search_context 需要非空 query。")
        top_k = arguments.get("top_k")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)):
            return _acp_failure("INVALID_ARGUMENTS", "acp_search_context top_k 必须是整数。")
        return {"success": True, "data": asdict(engine.search_context(query, top_k=top_k))}
    if tool_name == "acp_status":
        return {"success": True, "data": asdict(engine.status())}
    return _acp_failure(
        "UNSUPPORTED_INTERNAL_CAPABILITY",
        f"不支持的 ACP 能力：{tool_name}",
    )


def _acp_failure(
    code: str,
    message: str,
    detail: dict[str, object] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    return {"success": False, "error": error}


def _acp_int_argument(arguments: dict[str, Any], key: str) -> int | None:
    value = arguments.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _block_position(engine: AcpEngine, block_id: int) -> int | None:
    for index, block in enumerate(engine.blocks()):
        if block.block_id == block_id:
            return index
    return None


def _deserialize_transcript_entry(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        return value
    return {
        "role": "tool",
        "tool_name": "acp_decompress",
        "result": {
            "success": False,
            "error": {
                "code": "ACP_RESTORE_INVALID",
                "message": "压缩块内容无法还原为 transcript 条目。",
            },
        },
    }


def _history_receipt(value: Any) -> dict[str, Any]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "omitted_chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _compact_old_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    if len(serialized) <= 1_000:
        return dict(arguments)
    return {"history_receipt": _history_receipt(arguments)}


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
