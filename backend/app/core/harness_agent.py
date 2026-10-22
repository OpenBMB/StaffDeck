from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError

from app import paths
from app.core.harness_attachments import (
    ValidatedTaskImagePayload,
    isolated_attachment_context,
)
from app.core.base.request_compiler import (
    CapabilityDescriptor,
    TaskExecutionResult,
    TaskRequirement,
)
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.llm.json_field_stream import JsonStringFieldStream
from app.observability.spans import llm_operation
from app.core.knowledge_prefetch import plan_knowledge_prefill_actions
from app.core.sop_step_executor import plan_sop_prefill_actions
from app.session.slot_policy import strip_router_generated_message_slots

PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "harness_agent_prompt.md"
MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK = 2
# 模型可见 tool result 的字符预算。知识检索走内联（≤24KB 的精简载荷），
# 必须给它留出对应额度，否则会被截成半截 JSON 的 preview。
_CAPABILITY_RESULT_TRANSCRIPT_MAX_CHARS = 12_000
_KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS = 25_000

# 动作生成（harness.task_action）单次模型调用最多尝试几次。此前上游超时/限流会
# 直接终止整个 TaskFrame，把「瞬时不可用」当成「模型没按协议返回动作」，用户等了
# 数分钟后只拿到一句「当前任务的执行模型没有返回有效动作。」（2026-09-28 现场：
# 41K 字符 payload + 思考模式下第 14 轮撞 90s 上限）。一次有界重试即可覆盖网关抖动，
# 又不会让最坏延迟失控（每次尝试仍受 output_policy 的单次上限约束）。
_ACTION_MODEL_ATTEMPTS = 2
_MODEL_TRANSIENT_ERROR_CODES = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_RATE_LIMITED",
        "MODEL_UPSTREAM_UNAVAILABLE",
        "MODEL_UPSTREAM_ERROR",
    }
)
# 供 harness_v2_engine 判定 SOP 步骤是否可续跑：模型侧瞬时失败与协议失败一样
# 不应该吃掉已完成节点的结果。
MODEL_UNAVAILABLE_FAILURE_CODE = "HARNESS_MODEL_UNAVAILABLE"
# 面向用户的失败归因（截图/回复里只看到「没有返回有效动作」时无从判断该怎么办）。
_MODEL_FAILURE_REASONS = {
    "MODEL_TIMEOUT": "模型调用超时",
    "MODEL_RATE_LIMITED": "模型被限流",
    "MODEL_UPSTREAM_UNAVAILABLE": "模型上游服务不可用",
    "MODEL_UPSTREAM_ERROR": "模型上游返回错误",
    "MODEL_AUTHENTICATION_FAILED": "模型凭证无效",
    "MODEL_PERMISSION_DENIED": "模型权限不足",
    "MODEL_ENDPOINT_NOT_FOUND": "模型或接入地址不存在",
    "MODEL_INVALID_REQUEST": "模型拒绝了本次请求",
    "MODEL_REQUEST_TOO_LARGE": "请求体超过模型上限",
    "MODEL_UPSTREAM_CONFLICT": "模型上游冲突",
}
# 这些失败重发无用，必须改配置。
_MODEL_CONFIG_ERROR_CODES = frozenset(
    {
        "MODEL_AUTHENTICATION_FAILED",
        "MODEL_PERMISSION_DENIED",
        "MODEL_ENDPOINT_NOT_FOUND",
    }
)
ToolInvoker = Callable[[str, dict[str, Any]], dict[str, Any]]
TraceSink = Callable[[str, dict[str, Any]], None]
CancellationCheck = Callable[[], bool]

# 轻量节点直通（P1-1）：结构简单的 SOP 节点（唯一必执行能力 + 唯一无条件转移 +
# 无缺槽）的模型决策是低风险分类/编排任务，优先用意图识别轻量模型 + 一次性
# 编排提示，减少主模型串行调用；模型输出仍走同一校验协议，失败自动回退主模型。
LIGHTWEIGHT_DIRECTIVE = (
    "当前节点结构简单：只需调用一个必执行能力，成功后用 finish(completed) 收尾，"
    "并沿用唯一可用转移的 next_node_id。请直接输出 tool -> finish 动作序列，"
    "不要展开多余检查或重复确认。"
)


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
    # A2UI：缺槽询问的表单描述（确定性执行器产出），原样带到消息 metadata。
    ui_form: dict[str, Any] | None = None


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
        checkpoint: dict[str, Any] | None = None,
        lightweight_model_config: ModelConfig | None = None,
        edge_condition_specs: dict[str, Any] | None = None,
        slot_submission: Mapping[str, Any] | None = None,
        laya_router: Any = None,
        reply_preview_sink: Callable[[str], None] | None = None,
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
        # 产物只挂「本轮真正新产生」的（2026-09-16 修复）：不再从 checkpoint
        # 恢复历史产物——同一 task frame 续轮时，上一轮的产物会被原样复挂到
        # 每条新助手消息的「生成文件」区（典型现场：扫描合同问答里那条空的
        # contract_text.txt 跟着后续每条消息出现）。历史产物的下载不受影响：
        # 下载端点按会话全部消息的 harness_artifacts 检索。
        artifacts: list[dict[str, Any]] = []
        loaded_general_skill_names = (
            _string_list(checkpoint.get("loaded_general_skill_names"))
            if same_frame
            else []
        )
        recent_task_summaries = _string_list(
            checkpoint.get("recent_task_summaries")
        )[-8:]
        # A non-retryable failure only blocks an identical call inside this
        # invocation of the AgentLoop.  Persisting the signature in the
        # checkpoint made a later user turn inherit an obsolete failure even
        # after its inputs or external state had changed.
        non_retryable_action_signatures: set[str] = set()
        allowed_names = requirement.capability_manifest.allowed_names()
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        pending_actions: list[HarnessAction] = []
        # SOP 确定性执行器（P1）：预检索/纯流转/缺槽询问三类场景由代码直接判定，
        # 预填动作序列跳过首轮 task_action LLM 决策；不可判定返回空，走现有链路。
        # 恢复到同一步骤且上次终点是 awaiting_user（等槽位）时，放行场景 A
        # 槽位抽取——用户带齐信息回来正是该场景的主路径。
        resumed_awaiting_user = (
            same_step
            and str(checkpoint.get("last_status") or "") == "awaiting_user"
        )
        if same_step and not resumed_awaiting_user and callable(trace_sink):
            # 观测点：同步骤恢复但 last_status 不是 awaiting_user → 执行器会被
            # same_step 守卫静默放行给 LLM（2026-09-15 复盘发现补槽 turn 整帧
            # 落回 task_action 的头号嫌疑）。带出 checkpoint 现场，跑一次即定位。
            trace_sink(
                "sop_resume_skipped",
                {
                    "reason": "checkpoint_last_status_not_awaiting_user",
                    "checkpoint_last_status": str(checkpoint.get("last_status") or ""),
                    "checkpoint_step_id": str(checkpoint.get("step_id") or ""),
                    "current_step_id": current_step_id,
                    "checkpoint_keys": sorted(str(k) for k in checkpoint.keys())[:12],
                },
            )
        prefill_actions = plan_sop_prefill_actions(
            requirement,
            same_step=same_step,
            resumed_awaiting_user=resumed_awaiting_user,
            satisfied_required_knowledge_ids=set(satisfied_required_knowledge_ids),
            trace_sink=trace_sink,
            slot_extraction_model=lightweight_model_config or model_config,
            edge_condition_specs=edge_condition_specs,
            slot_submission=slot_submission,
            laya_router=laya_router,
        )
        if prefill_actions:
            pending_actions.extend(
                HarnessAction.model_validate(item) for item in prefill_actions
            )
        # 仅知识库数字员工的确定性直查：新帧首轮直接预填 knowledge_search，
        # 模型第一次被调用时检索结果已在 transcript 里，省掉「决策调检索」
        # 那一整轮 LLM（仅 conversation kind 且无技能/工具能力时介入）。
        # 判定为纯确定性：仅民间词典拦寒暄，其余一律预填（不再调 LLM 判「要不要查」）。
        knowledge_prefill = plan_knowledge_prefill_actions(
            requirement,
            first_turn=not transcript and not evidence_results,
            trace_sink=trace_sink,
        )
        if knowledge_prefill:
            pending_actions.extend(
                HarnessAction.model_validate(item) for item in knowledge_prefill
            )

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
                "last_status": result.status,
                "transcript": _transcript_for_model(transcript),
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
                        # 轻量节点直通：首轮决策尝试用轻量模型 + 一次性编排提示；
                        # 轻量输出协议校验失败时第二轮回退主模型重跑（不再带提示）。
                        first_iteration = iteration == 1
                        using_lightweight = (
                            lightweight_model_config is not None
                            and first_iteration
                            and protocol_attempt == 0
                        )
                        decision_model = (
                            lightweight_model_config if using_lightweight else model_config
                        )
                        directive_payload = (
                            {**payload, "execution_directive": LIGHTWEIGHT_DIRECTIVE}
                            if using_lightweight
                            else payload
                        )
                        with llm_operation(
                            "harness.task_action",
                            task_frame_id=requirement.task_frame_id,
                            iteration=iteration,
                            protocol_attempt=protocol_attempt + 1,
                            lightweight_decision=using_lightweight,
                            # 响应生成链观测：单次 task_action 实测 91-187s，
                            # 需 payload 体量佐证是否上下文过大
                            payload_chars=(
                                len(json.dumps(directive_payload, ensure_ascii=False, default=str))
                                + len(system_prompt or "")
                            ),
                        ):
                            # 上游瞬时失败（超时/限流/5xx）重试同一请求：这类错误与
                            # 协议无关，重试成本远低于让整轮对话以「模型没有返回有效
                            # 动作」收尾。协议/鉴权类错误不在此处重试，直接抛出交给
                            # 外层分类收尾。
                            for model_attempt in range(_ACTION_MODEL_ATTEMPTS):
                                try:
                                    client = _deadline_llm_client(
                                        decision_model,
                                        step_deadline_monotonic,
                                    )
                                    raw = _generate_harness_action_json(
                                        client,
                                        system_prompt,
                                        directive_payload,
                                        preview_sink=_iteration_reply_preview(
                                            requirement,
                                            capability_results,
                                            satisfied_required_knowledge_ids,
                                            reply_preview_sink,
                                        ),
                                    )
                                    break
                                except LLMError as exc:
                                    exhausted = model_attempt + 1 >= _ACTION_MODEL_ATTEMPTS
                                    if (
                                        exhausted
                                        or not _is_transient_model_error(exc)
                                        or _deadline_expired(step_deadline_monotonic)
                                    ):
                                        raise
                                    if trace_sink:
                                        trace_sink(
                                            "harness_action_model_retry",
                                            {
                                                "iteration": iteration,
                                                "attempt": model_attempt + 2,
                                                "max_attempts": _ACTION_MODEL_ATTEMPTS,
                                                "error": str(exc),
                                                "code": str(getattr(exc, "code", "") or ""),
                                            },
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
                                        "lightweight_decision": using_lightweight,
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
                return finish(
                    _action_generation_failure(
                        requirement,
                        exc,
                        iteration=iteration,
                        capability_results=capability_results,
                    )
                )
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
                trace_payload = {
                    "iteration": iteration,
                    "tool_name": tool_name,
                    "success": bool(result.get("success")),
                    "error": result.get("error"),
                    "result": _trace_capability_result(
                        tool_name,
                        result,
                    ),
                }
                if tool_name == "knowledge_search":
                    # 检索词透传：ks 的 query 是模型改写后的检索词（非用户原话），
                    # 路由缓存按它建 key——观测 hit 具体是哪个问法命中必须带上它
                    trace_payload["query"] = str(
                        (action.arguments or {}).get("query") or ""
                    )[:120]
                trace_sink(
                    "harness_tool_completed",
                    trace_payload,
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
    preview_sink: Callable[[str], None] | None = None,
) -> Any:
    """Use sequence-aware parsing when the concrete client supports it."""

    kwargs: dict[str, Any] = {}
    if preview_sink is not None:
        kwargs["preview_sink"] = preview_sink
    sequence_generator = getattr(client, "generate_json_sequence", None)
    if callable(sequence_generator):
        return sequence_generator(system_prompt, payload, **kwargs)
    # Test doubles and older compatible clients still expose the original method.
    return client.generate_json(system_prompt, payload, **kwargs)


def _reply_preview_sink(emit: Callable[[str], None]) -> Callable[[str], None]:
    """把决策 JSON 的原始文本增量翻译成 ``finish.reply_fragment`` 预览增量。

    预览通道只负责「正文早出现」：结构不符、字段顺序意外都静默放弃，最终正文
    一律以完整 JSON 的解析结果为准（由 ``TurnStreamSink.reconcile`` 收尾对账）。
    """

    extractor = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    def feed(delta: str) -> None:
        text = extractor.feed(delta)
        if text:
            emit(text)

    return feed


def _iteration_reply_preview(
    requirement: TaskRequirement,
    capability_results: list[dict[str, Any]],
    satisfied_required_knowledge_ids: set[str],
    emit: Callable[[str], None] | None,
) -> Callable[[str], None] | None:
    """本次决策调用是否启用正文预览。

    只在「强制能力都已满足」时启用：此时模型要么收尾（``finish``，正文就是最终
    答案），要么再多调一个非强制能力（没有 ``reply_fragment``，预览不会误吐）。
    仍缺强制能力时模型不可能被接受 ``finish``，提前预览只会把一段注定作废的文本
    推给用户，反而要等收尾对账再抹掉。
    """

    if emit is None:
        return None
    if _missing_required_capabilities(
        requirement,
        capability_results,
        satisfied_required_knowledge_ids,
    ):
        return None
    return _reply_preview_sink(emit)


def _harness_actions_from_raw(raw: object) -> list[HarnessAction]:
    """Normalize one action or an ordered action sequence from a provider response."""

    items: object = raw
    if isinstance(raw, dict) and set(raw) == {"actions"}:
        items = raw.get("actions")
    if not isinstance(items, list):
        return [HarnessAction.model_validate(items)]
    if not items:
        raise ValueError("Harness action sequence must not be empty.")
    actions = [HarnessAction.model_validate(item) for item in items]
    if any(action.action == "finish" for action in actions[:-1]):
        raise ValueError("A finish action must be the final item in an action sequence.")
    return actions


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
    # successful SOP step into a handoff. Even a dedicated handoff node may have a
    # valid non-handoff transition chosen by the model; only a terminal handoff node
    # with no selected successor is coerced to the handoff status.
    if step_type == "handoff" and status == "completed" and next_step_id is None:
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
        ui_form=action.ui_form,
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


def _is_transient_model_error(exc: Exception) -> bool:
    """判断模型/上游侧错误是否值得原样重试（协议与鉴权类错误不在此列）。"""

    if not isinstance(exc, LLMError):
        return False
    code = str(getattr(exc, "code", "") or "")
    if code in _MODEL_TRANSIENT_ERROR_CODES:
        return True
    return bool(getattr(exc, "retryable", False)) and code.startswith("MODEL_")


def _action_generation_failure(
    requirement: TaskRequirement,
    exc: Exception,
    *,
    iteration: int,
    capability_results: list[dict[str, Any]],
) -> TaskExecutionResult:
    """把动作生成失败收尾成用户能看懂的结果。

    模型侧失败（超时/限流/5xx/凭证）与协议失败必须分开：前者此前统一以「当前任务的
    执行模型没有返回有效动作。」收尾，用户等了整轮却看不到任何可操作的提示。
    """

    detail = str(exc)
    if isinstance(exc, LLMError):
        code = str(getattr(exc, "code", "") or "")
        reason = _MODEL_FAILURE_REASONS.get(code, "模型调用失败")
        retried = (
            f"（已重试 {_ACTION_MODEL_ATTEMPTS} 次）" if _is_transient_model_error(exc) else ""
        )
        advice = (
            "请到「模型配置」检查该模型的凭证、接入地址与权限。"
            if code in _MODEL_CONFIG_ERROR_CODES
            else "可以：① 直接重新发送这条消息；② 换用响应更快的模型后重试；"
            "③ 把任务拆成更小的步骤。"
        )
        return TaskExecutionResult(
            task_frame_id=requirement.task_frame_id,
            status="failed",
            reply_fragment=f"本步{reason}{retried}，任务没有完成。{advice}",
            task_summary=f"{reason}，任务未完成。",
            capability_results=capability_results,
            action_count=iteration,
            error={"code": MODEL_UNAVAILABLE_FAILURE_CODE, "message": detail},
        )
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status="failed",
        reply_fragment=(
            "模型没有返回可执行的动作（协议校验失败），请重新发送这条消息，"
            "或更换模型后再试。"
        ),
        task_summary="Harness 动作解析失败。",
        capability_results=capability_results,
        action_count=iteration,
        error={"code": "HARNESS_ACTION_INVALID", "message": detail},
    )


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


def _capability_result_transcript_budget(tool_name: str) -> int:
    """Model-visible tool result budget per capability.

    ``knowledge_search`` 的结果已由 app/knowledge/search_payload 压到 24KB 以内并内联
    （见 KNOWLEDGE_PAYLOAD_MAX_CHARS）；这里必须留出同样量级的预算，否则会被截成一个
    半截 JSON 的 ``preview``——比原来落沙箱文件更难用。其他能力沿用 12K 口径不变。
    """

    if tool_name == "knowledge_search":
        return _KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS
    return _CAPABILITY_RESULT_TRANSCRIPT_MAX_CHARS


def _bounded_capability_result(
    tool_name: str,
    result: dict[str, Any],
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    if max_chars is None:
        max_chars = _capability_result_transcript_budget(tool_name)
    payload = {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "data": result.get("data"),
        "error": result.get("error"),
    }
    chunk_count = result.get("chunk_count")
    if isinstance(chunk_count, int) and not isinstance(chunk_count, bool):
        # 小标量随行（场景 C2 判「检索到/没检索到」）；data 超限换文件引用
        # 或整体截断后，data.chunks 都会丢，顶层标量是唯一可靠载体
        payload["chunk_count"] = chunk_count
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
    if isinstance(chunk_count, int) and not isinstance(chunk_count, bool):
        truncated["chunk_count"] = chunk_count
    if isinstance(result.get("mcp_app"), dict):
        truncated["mcp_app"] = result["mcp_app"]
    return truncated


def _transcript_for_model(
    transcript: list[dict[str, Any]],
    *,
    keep_recent_entries: int = 6,
) -> list[dict[str, Any]]:
    """Project persistent tool history into a bounded execution context.

    Tool results stay in the durable invocation/trace records.  The model only
    needs the newest interactions plus stable receipts for older operations.
    GeneralSkill package instructions are retained because they define the
    workflow the current AgentLoop is following.
    """

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
    trace = _bounded_capability_result(
        tool_name,
        trace_result,
        max_chars=4_000,
    )
    # 知识检索的 route 观测 phase（route_cache_hit / fast_path 等）是
    # 轻量字符串列表，原样保留到事件 payload 顶层，供诊断脚本扫描
    route_phases = result.get("route_phases")
    if isinstance(route_phases, list) and route_phases:
        trace["route_phases"] = route_phases
    return trace
