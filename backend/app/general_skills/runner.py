from __future__ import annotations

import json
import os
import queue
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from app import paths
from app.db.models import GeneralSkill, ModelConfig
from app.general_skills.schema import (
    GeneralSkillExecutionPlan,
    GeneralSkillExecutionReview,
    GeneralSkillReply,
    GeneralSkillRunResponse,
    GeneralSkillSelection,
)
from app.general_skills.runtime_env import GeneralSkillRuntimeError, ensure_runtime_python, runtime_environment
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import snapshot_model_config
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation


PROMPT_DIR = paths.resource_dir() / "app" / "llm" / "prompts"
SELECTOR_PROMPT = PROMPT_DIR / "general_skill_selector_prompt.md"
RUNNER_PROMPT = PROMPT_DIR / "general_skill_runner_prompt.md"
REPAIR_PROMPT = PROMPT_DIR / "general_skill_repair_prompt.md"
REVIEW_PROMPT = PROMPT_DIR / "general_skill_review_prompt.md"
REPLY_PROMPT = PROMPT_DIR / "general_skill_reply_prompt.md"
RUN_TIMEOUT_SECONDS = 12
MAX_OUTPUT_CHARS = 20000
GENERAL_SKILL_MAX_TOKENS = 8192
GENERAL_SKILL_MAX_ATTEMPTS = 10
TraceSink = Callable[[dict[str, Any]], None]
GENERAL_SKILL_SELECTION_OUTPUT = {
    "use_general_skill": "boolean",
    "selected_slug": "string?",
    "use_knowledge": "boolean",
    "knowledge_query": "string?",
    "confidence": "number",
    "reason": "string?",
}
GENERAL_SKILL_PLAN_OUTPUT = {
    "code": "string",
    "runtime": "bash | python",
    "rationale": "string?",
    "expected_output": "string?",
}
GENERAL_SKILL_REVIEW_OUTPUT = {
    "result_sufficient": "boolean",
    "needs_retry": "boolean",
    "terminal": "boolean",
    "reason": "string",
    "repair_hint": "string?",
}
GENERAL_SKILL_REPLY_OUTPUT = {"reply": "string"}


class GeneralSkillSelector:
    def decide(
        self,
        query: str,
        general_skills: list[GeneralSkill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillSelection:
        payload = stage_payload(
            phase="Router / General Skill Selector",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=SELECTOR_PROMPT.read_text(encoding="utf-8"),
            stage_data={
                "general_skills": [
                    {
                        "slug": skill.slug,
                        "name": skill.name,
                        "description": skill.description,
                        "homepage": skill.homepage,
                        "status": skill.status,
                    }
                    for skill in general_skills
                    if skill.status == "published"
                ],
            },
            output_contract=GENERAL_SKILL_SELECTION_OUTPUT,
        )
        with llm_operation("general_skill.select"):
            raw = LLMClient(model_config).generate_json(
                unified_system_prompt(), payload
            )
        decision = GeneralSkillSelection.model_validate(raw)
        slugs = {skill.slug for skill in general_skills if skill.status == "published"}
        if decision.use_general_skill and decision.selected_slug in slugs:
            return decision
        return decision.model_copy(update={"use_general_skill": False, "selected_slug": None})


class GeneralSkillRunner:
    def run(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        user_id: str = "",
        max_attempts: int = GENERAL_SKILL_MAX_ATTEMPTS,
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillRunResponse:
        trace: list[dict[str, Any]] = []
        max_attempts = max(1, min(max_attempts, GENERAL_SKILL_MAX_ATTEMPTS))
        _emit(trace, {"phase": "skill_loaded", "message": f"已加载通用技能 {skill.name}", "slug": skill.slug}, event_sink)
        try:
            plan, planning_attempts = self._generate_plan_with_reflection(
                skill,
                query,
                model_config,
                trace,
                event_sink,
                max_attempts,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            _emit(trace, {"phase": "plan_failed", "message": "模型生成 runner 失败", "error": str(exc)}, event_sink)
            return GeneralSkillRunResponse(
                skill_slug=skill.slug,
                execution_trace=trace,
                generated_code="",
                stdout="",
                stderr=str(exc),
                structured_result={"success": False, "error": "runner_plan_failed", "message": str(exc)},
                reply="抱歉，当前通用技能执行代码生成失败，暂时无法完成这次运行。",
            )

        attempts: list[dict[str, Any]] = planning_attempts
        stdout = ""
        stderr = ""
        structured_result: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            _emit(
                trace,
                {"phase": "attempt_started", "message": f"开始第 {attempt} 次运行", "attempt": attempt},
                event_sink,
            )
            stdout, stderr, structured_result = self._execute_plan(
                skill,
                query,
                plan,
                user_id,
                trace,
                event_sink,
                attempt,
            )
            _normalize_failure_diagnostics(structured_result)
            review = self._review_execution_result(
                skill,
                query,
                model_config,
                plan,
                stdout,
                stderr,
                structured_result,
                trace,
                event_sink,
                attempt,
                conversation_context,
                memory_context,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "code": _truncate(plan.code),
                    "stdout": _truncate(stdout),
                    "stderr": _truncate(stderr),
                    "structured_result": structured_result,
                    "execution_review": review,
                }
            )
            needs_retry = bool(review.get("needs_retry"))
            if not needs_retry:
                if structured_result.get("success") is False or review.get("result_sufficient") is False:
                    _emit(
                        trace,
                        {
                            "phase": "reflection_stopped",
                            "message": f"第 {attempt} 次运行结果不足，但模型判断不可继续自动修复",
                            "attempt": attempt,
                            "structured_result": structured_result,
                            "review": review,
                        },
                        event_sink,
                    )
                else:
                    _emit(
                        trace,
                        {"phase": "reflection_passed", "message": f"第 {attempt} 次运行结果可用", "attempt": attempt},
                        event_sink,
                    )
                break
            if attempt >= max_attempts:
                _emit(
                    trace,
                    {
                        "phase": "reflection_stopped",
                        "message": f"已达到最多 {max_attempts} 次尝试，停止自动修复",
                        "attempt": attempt,
                    },
                    event_sink,
                )
                break
            _emit(
                trace,
                {
                    "phase": "reflection_retrying",
                    "message": f"第 {attempt} 次运行未达预期，模型正在根据结果反思修复",
                    "attempt": attempt,
                    "stdout_preview": stdout[:600],
                    "stderr_preview": stderr[:600],
                    "structured_result": structured_result,
                    "review": review,
                },
                event_sink,
            )
            try:
                plan = self._repair_plan(
                    skill,
                    query,
                    model_config,
                    trace,
                    attempts,
                    event_sink,
                    attempt + 1,
                    conversation_context,
                    memory_context,
                )
            except LLMError as exc:
                _emit(
                    trace,
                    {"phase": "repair_failed", "message": "模型反思修复代码失败", "attempt": attempt, "error": str(exc)},
                    event_sink,
                )
                break

        try:
            reply = self._generate_reply(
                skill,
                query,
                model_config,
                trace,
                stdout,
                stderr,
                structured_result,
                event_sink,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            _emit(trace, {"phase": "reply_failed", "message": "模型生成最终回复失败", "error": str(exc)}, event_sink)
            reply = _fallback_reply(structured_result)
        return GeneralSkillRunResponse(
            skill_slug=skill.slug,
            execution_trace=trace,
            generated_code=plan.code,
            stdout=stdout,
            stderr=stderr,
            structured_result=structured_result,
            reply=reply,
        )

    def _generate_plan(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillExecutionPlan:
        _emit(trace, {"phase": "planning", "message": "正在根据 SKILL.md 生成 runner"}, event_sink)
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": skill.skill_markdown,
                "package": _skill_package_payload(skill),
            },
            "runtime": {
                "languages": ["bash", "python"],
                "stdin_json": {
                    "query": query,
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "skill_workspace": "<runtime absolute path to the restored skill folder>",
                    "skill_files": [file["path"] for file in _skill_files(skill)],
                },
                "timeout_seconds": RUN_TIMEOUT_SECONDS,
            },
        }
        payload = stage_payload(
            phase="Step Agent / General Skill Plan",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=RUNNER_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_PLAN_OUTPUT,
        )
        with llm_operation("general_skill.plan"):
            raw = LLMClient(_with_min_tokens(model_config, GENERAL_SKILL_MAX_TOKENS)).generate_json(
                unified_system_prompt(),
                payload,
            )
        plan = GeneralSkillExecutionPlan.model_validate(raw)
        plan.runtime = _plan_runtime(plan)
        if not plan.code.strip():
            raise LLMError("General skill runner code is empty")
        runtime_label = _runtime_label(plan.runtime)
        _emit(
            trace,
            {
                "phase": "plan_created",
                "message": f"已生成 {runtime_label} runner",
                "runtime": plan.runtime,
                "rationale": plan.rationale,
                "code": plan.code,
                "expected_output": plan.expected_output,
            },
            event_sink,
        )
        return plan

    def _generate_plan_with_reflection(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None,
        max_attempts: int,
        conversation_context: dict[str, object] | None,
        memory_context: list[dict[str, object]] | None,
    ) -> tuple[GeneralSkillExecutionPlan, list[dict[str, Any]]]:
        planning_failures: list[dict[str, Any]] = []
        last_error: LLMError | None = None
        for plan_attempt in range(1, max_attempts + 1):
            try:
                if plan_attempt == 1:
                    return (
                        self._generate_plan(
                            skill,
                            query,
                            model_config,
                            trace,
                            event_sink,
                            conversation_context,
                            memory_context,
                        ),
                        planning_failures,
                    )
                return (
                    self._repair_plan(
                        skill,
                        query,
                        model_config,
                        trace,
                        planning_failures,
                        event_sink,
                        plan_attempt,
                        conversation_context,
                        memory_context,
                    ),
                    planning_failures,
                )
            except LLMError as exc:
                last_error = exc
                failure = {
                    "attempt": f"planning-{plan_attempt}",
                    "code": "",
                    "stdout": "",
                    "stderr": str(exc),
                    "structured_result": {
                        "success": False,
                        "error": "plan_generation_failed",
                        "message": str(exc),
                        "retryable": True,
                    },
                    "execution_review": {
                        "result_sufficient": False,
                        "needs_retry": plan_attempt < max_attempts,
                        "terminal": False,
                        "reason": "模型未能生成可执行 runner 计划，需要重新输出合法 JSON、runtime 和完整代码。",
                        "repair_hint": "保留原始 skill 与 query，重新输出包含 runtime、code、rationale、expected_output 的合法 JSON。",
                    },
                }
                planning_failures.append(failure)
                _emit(
                    trace,
                    {
                        "phase": "plan_failed",
                        "message": f"第 {plan_attempt} 次 runner 计划生成失败",
                        "attempt": plan_attempt,
                        "error": str(exc),
                    },
                    event_sink,
                )
                if plan_attempt >= max_attempts:
                    break
                _emit(
                    trace,
                    {
                        "phase": "reflection_retrying",
                        "message": f"第 {plan_attempt} 次计划生成失败，模型正在反思并重新输出代码",
                        "attempt": plan_attempt,
                        "structured_result": failure["structured_result"],
                        "review": failure["execution_review"],
                    },
                    event_sink,
                )
        raise LLMError(str(last_error) if last_error else "General skill runner plan generation failed")

    def _repair_plan(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        event_sink: TraceSink | None,
        next_attempt: int,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> GeneralSkillExecutionPlan:
        _emit(
            trace,
            {"phase": "repair_planning", "message": f"正在生成第 {next_attempt} 次运行代码", "attempt": next_attempt},
            event_sink,
        )
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": skill.skill_markdown,
                "package": _skill_package_payload(skill),
            },
            "runtime": {
                "languages": ["bash", "python"],
                "stdin_json": {
                    "query": query,
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "skill_workspace": "<runtime absolute path to the restored skill folder>",
                    "skill_files": [file["path"] for file in _skill_files(skill)],
                },
                "timeout_seconds": RUN_TIMEOUT_SECONDS,
            },
            "previous_attempts": attempts[-3:],
        }
        payload = stage_payload(
            phase="Step Agent / General Skill Repair",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REPAIR_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_PLAN_OUTPUT,
        )
        with llm_operation("general_skill.repair", attempt=next_attempt):
            raw = LLMClient(_with_min_tokens(model_config, GENERAL_SKILL_MAX_TOKENS)).generate_json(
                unified_system_prompt(),
                payload,
            )
        plan = GeneralSkillExecutionPlan.model_validate(raw)
        plan.runtime = _plan_runtime(plan)
        if not plan.code.strip():
            raise LLMError("General skill repaired runner code is empty")
        runtime_label = _runtime_label(plan.runtime)
        _emit(
            trace,
            {
                "phase": "plan_created",
                "message": f"已生成第 {next_attempt} 次 {runtime_label} runner",
                "attempt": next_attempt,
                "runtime": plan.runtime,
                "rationale": plan.rationale,
                "code": plan.code,
                "expected_output": plan.expected_output,
            },
            event_sink,
        )
        return plan

    def _execute_plan(
        self,
        skill: GeneralSkill,
        query: str,
        plan: GeneralSkillExecutionPlan,
        user_id: str,
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None = None,
        attempt: int = 1,
    ) -> tuple[str, str, dict[str, Any]]:
        run_dir = Path(mkdtemp(prefix="ultrarag_general_skill_"))
        skill_dir = run_dir / "skill"
        _materialize_skill_package(skill, skill_dir)
        runtime = _plan_runtime(plan)
        runner_path = run_dir / ("runner.sh" if runtime == "bash" else "runner.py")
        runner_path.write_text(plan.code, encoding="utf-8")
        stdin_payload = {
            "query": query,
            "skill_slug": skill.slug,
            "skill_name": skill.name,
            "user_id": user_id,
            "skill_workspace": str(skill_dir),
            "skill_files": [file["path"] for file in _skill_files(skill)],
        }
        _emit(
            trace,
            {
                "phase": "running_code",
                "message": f"正在运行第 {attempt} 次 {_runtime_label(runtime)} runner",
                "run_id": run_dir.name,
                "attempt": attempt,
                "runtime": runtime,
            },
            event_sink,
        )
        try:
            runtime_python = ensure_runtime_python()
            env = runtime_environment(os.environ.copy())
        except GeneralSkillRuntimeError as exc:
            structured = {
                "success": False,
                "error": "runtime_environment_error",
                "message": str(exc),
                "retryable": False,
            }
            _emit(
                trace,
                {
                    "phase": "runtime_environment_failed",
                    "message": "通用技能运行环境准备失败",
                    "attempt": attempt,
                    "runtime": runtime,
                    "structured_result": structured,
                },
                event_sink,
            )
            return "", str(exc), structured
        env.update(
            {
                "ARGUMENTS": query,
                "QUERY": query,
                "SKILL_WORKSPACE": str(skill_dir),
                "SKILL_SLUG": skill.slug,
                "SKILL_NAME": skill.name,
                "USER_ID": user_id,
                "SKILL_FILES_JSON": json.dumps([file["path"] for file in _skill_files(skill)], ensure_ascii=False),
            }
        )
        if runtime == "bash" and not _bash_supported():
            structured = {
                "success": False,
                "error": "bash_runtime_unsupported",
                "message": "当前运行环境不支持 bash 技能（Windows 或打包版），请改用 Python 技能。",
                "retryable": False,
            }
            _emit(trace, {"phase": "runtime_environment_failed",
                          "message": "bash runtime 不受支持", "attempt": attempt,
                          "runtime": runtime, "structured_result": structured}, event_sink)
            return "", structured["message"], structured
        command = ["/bin/bash", str(runner_path)] if runtime == "bash" else [str(runtime_python), str(runner_path)]
        cwd = str(skill_dir if runtime == "bash" else run_dir)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=False,
        )
        if process.stdin:
            process.stdin.write(json.dumps(stdin_payload, ensure_ascii=False).encode("utf-8"))
            process.stdin.close()

        try:
            stdout, stderr, timed_out = _stream_process_output(process, trace, event_sink, attempt)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        if timed_out:
            stdout = _truncate(stdout)
            stderr = _truncate(stderr)
            structured = {"success": False, "error": "runner_timeout", "message": "通用技能运行超时"}
            _emit(
                trace,
                {
                    "phase": "code_timeout",
                    "message": f"{_runtime_label(runtime)} runner 执行超时",
                    "attempt": attempt,
                    "runtime": runtime,
                    "stdout_preview": stdout[:600],
                    "stderr_preview": stderr[:600],
                    "structured_result": structured,
                },
                event_sink,
            )
            return stdout, stderr, structured

        return_code = process.wait()
        stdout = _truncate(stdout)
        stderr = _truncate(stderr)
        structured = _parse_stdout_json(stdout)
        if return_code != 0:
            structured.setdefault("success", False)
            structured.setdefault("error", f"runner exited with code {return_code}")
        _emit(
            trace,
            {
                "phase": "code_finished",
                "message": f"{_runtime_label(runtime)} runner 执行完成",
                "attempt": attempt,
                "runtime": runtime,
                "return_code": return_code,
                "stdout_preview": stdout[:600],
                "stderr_preview": stderr[:600],
                "structured_result": structured,
            },
            event_sink,
        )
        return stdout, stderr, structured

    def _generate_reply(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
        stdout: str,
        stderr: str,
        structured_result: dict[str, Any],
        event_sink: TraceSink | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> str:
        _emit(trace, {"phase": "replying", "message": "正在根据运行结果生成回复"}, event_sink)
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
            },
            "execution_trace": trace,
            "stdout": stdout,
            "stderr": stderr,
            "structured_result": structured_result,
        }
        payload = stage_payload(
            phase="Response Generator / General Skill Reply",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REPLY_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_REPLY_OUTPUT,
        )
        try:
            with llm_operation("general_skill.reply"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            reply = GeneralSkillReply.model_validate(raw).reply.strip()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"General skill reply returned invalid JSON schema: {exc}") from exc
        if not reply:
            raise LLMError("General skill reply is empty")
        _emit(trace, {"phase": "reply_created", "message": "已生成最终回复"}, event_sink)
        return reply

    def _review_execution_result(
        self,
        skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        plan: GeneralSkillExecutionPlan,
        stdout: str,
        stderr: str,
        structured_result: dict[str, Any],
        trace: list[dict[str, Any]],
        event_sink: TraceSink | None,
        attempt: int,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> dict[str, Any]:
        _emit(
            trace,
            {
                "phase": "reflection_reviewing",
                "message": f"正在校验第 {attempt} 次运行结果",
                "attempt": attempt,
            },
            event_sink,
        )
        stage_data = {
            "skill": {
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description,
                "homepage": skill.homepage,
                "markdown": _truncate(skill.skill_markdown, 6000),
                "package": _skill_package_payload(skill, preview_limit=6000),
            },
            "runner": {
                "rationale": plan.rationale,
                "expected_output": plan.expected_output,
                "code_preview": _truncate(plan.code, 6000),
            },
            "attempt": attempt,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "structured_result": structured_result,
        }
        payload = stage_payload(
            phase="Reflection / General Skill Review",
            user_message=query,
            conversation_context=conversation_context,
            memory_context=memory_context,
            instructions=REVIEW_PROMPT.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=GENERAL_SKILL_REVIEW_OUTPUT,
        )
        try:
            with llm_operation("general_skill.review", attempt=attempt):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            review = GeneralSkillExecutionReview.model_validate(raw).model_dump(mode="json")
        except Exception as exc:
            fallback_needs_retry = _execution_needs_retry(stdout, stderr, structured_result)
            review = {
                "result_sufficient": not fallback_needs_retry,
                "needs_retry": fallback_needs_retry,
                "terminal": False,
                "reason": f"模型校验失败，使用运行信号兜底判断：{exc}",
                "repair_hint": "补充运行诊断或调整 runner 输出结构",
            }
        if review.get("terminal") is True:
            review["needs_retry"] = False
        _emit(
            trace,
            {
                "phase": "reflection_reviewed",
                "message": "已完成运行结果校验",
                "attempt": attempt,
                "review": review,
            },
            event_sink,
        )
        return review


def _truncate(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...<truncated>"


def _plan_runtime(plan: GeneralSkillExecutionPlan) -> str:
    runtime = str(getattr(plan, "runtime", "") or "python").strip().lower()
    if runtime in {"bash", "shell", "sh"}:
        return "bash"
    return "python"


def _runtime_label(runtime: str) -> str:
    return "Bash" if runtime == "bash" else "Python"


def _skill_files(skill: GeneralSkill) -> list[dict[str, Any]]:
    raw_files = getattr(skill, "skill_files_json", None)
    files = raw_files if isinstance(raw_files, list) else []
    normalized: list[dict[str, Any]] = []
    for raw_file in files:
        if not isinstance(raw_file, dict):
            continue
        path = _safe_package_path(str(raw_file.get("path") or ""))
        content = str(raw_file.get("content") or "")
        if not path:
            continue
        normalized.append(
            {
                "path": path,
                "content": content,
                "size": int(raw_file.get("size") or len(content.encode("utf-8"))),
                "mime_type": raw_file.get("mime_type"),
            }
        )
    if normalized:
        return normalized
    markdown = str(getattr(skill, "skill_markdown", "") or "")
    return [{"path": "SKILL.md", "content": markdown, "size": len(markdown.encode("utf-8")), "mime_type": "text/markdown"}]


def _skill_package_payload(skill: GeneralSkill, preview_limit: int = 12000) -> dict[str, Any]:
    files = _skill_files(skill)
    previews: list[dict[str, Any]] = []
    remaining = preview_limit
    for file in files:
        content = str(file.get("content") or "")
        preview = content[: max(0, min(len(content), remaining))]
        remaining -= len(preview)
        previews.append(
            {
                "path": file["path"],
                "size": file.get("size"),
                "mime_type": file.get("mime_type"),
                "content_preview": preview,
                "truncated": len(preview) < len(content),
            }
        )
    return {
        "entrypoint": "SKILL.md",
        "file_count": len(files),
        "files": previews,
    }


def _materialize_skill_package(skill: GeneralSkill, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for file in _skill_files(skill):
        relative_path = _safe_package_path(str(file["path"]))
        output_path = target_dir / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(str(file.get("content") or ""), encoding="utf-8")


def _safe_package_path(path: str) -> str:
    cleaned = path.replace("\\", "/").strip().strip("/")
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _parse_stdout_json(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    if not stripped:
        return {"success": False, "message": "runner produced no stdout"}
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
        return {"success": True, "data": value}
    except json.JSONDecodeError:
        return {"success": True, "text": stripped}


def _stream_process_output_selectors(
    process: subprocess.Popen[bytes],
    trace: list[dict[str, Any]],
    event_sink: TraceSink | None,
    attempt: int,
) -> tuple[str, str, bool]:
    selector = selectors.DefaultSelector()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    streams: list[tuple[Any, str]] = []
    if process.stdout:
        streams.append((process.stdout, "stdout"))
    if process.stderr:
        streams.append((process.stderr, "stderr"))
    for stream, name in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, data=name)

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    timed_out = False
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                timed_out = True
                process.kill()
                break
            events = selector.select(timeout=0.1)
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in list(selector.get_map().values())]
            for key, _ in events:
                name = str(key.data)
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except KeyError:
                        pass
                    continue
                text = chunk.decode("utf-8", errors="replace")
                if name == "stdout":
                    stdout_parts.append(text)
                    phase = "stdout_chunk"
                    message = "收到运行输出"
                else:
                    stderr_parts.append(text)
                    phase = "stderr_chunk"
                    message = "收到错误输出"
                _emit(
                    trace,
                    {"phase": phase, "message": message, "attempt": attempt, "text": text},
                    event_sink,
                )
    finally:
        selector.close()
    return "".join(stdout_parts), "".join(stderr_parts), timed_out


def _use_thread_reader() -> bool:
    return sys.platform == "win32"


def _stream_process_output(process, trace, event_sink, attempt):
    if _use_thread_reader():
        return _stream_process_output_threaded(process, trace, event_sink, attempt)
    return _stream_process_output_selectors(process, trace, event_sink, attempt)


def _stream_process_output_threaded(process, trace, event_sink, attempt):
    q: "queue.Queue[tuple[str, bytes]]" = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def _reader(stream, name: str) -> None:
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                q.put((name, chunk))
        finally:
            q.put((name, b""))  # EOF 标记

    stream_map = [(process.stdout, "stdout"), (process.stderr, "stderr")]
    threads: list[threading.Thread] = []
    for stream, name in stream_map:
        if stream is None:
            continue
        t = threading.Thread(target=_reader, args=(stream, name), daemon=True)
        t.start()
        threads.append(t)

    open_streams = sum(1 for s, _ in stream_map if s is not None)
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    timed_out = False
    eof_count = 0
    while eof_count < open_streams:
        if time.monotonic() > deadline:
            timed_out = True
            process.kill()
            break
        try:
            name, chunk = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if chunk == b"":
            eof_count += 1
            continue
        text = chunk.decode("utf-8", errors="replace")
        if name == "stdout":
            stdout_parts.append(text)
            phase, message = "stdout_chunk", "收到运行输出"
        else:
            stderr_parts.append(text)
            phase, message = "stderr_chunk", "收到错误输出"
        _emit(trace, {"phase": phase, "message": message, "attempt": attempt, "text": text}, event_sink)

    for t in threads:
        t.join(timeout=1.0)
    return "".join(stdout_parts), "".join(stderr_parts), timed_out


def _bash_supported() -> bool:
    if sys.platform == "win32":
        return False
    if paths.is_frozen():
        return False
    return Path("/bin/bash").exists()


def _emit(trace: list[dict[str, Any]], item: dict[str, Any], event_sink: TraceSink | None = None) -> None:
    trace.append(item)
    if event_sink:
        event_sink(item)


def _execution_needs_retry(stdout: str, stderr: str, structured_result: dict[str, Any]) -> bool:
    if structured_result.get("success") is False:
        if structured_result.get("retryable") is False or structured_result.get("terminal") is True:
            return False
        return True
    if structured_result.get("error") or structured_result.get("error_code"):
        return True
    if stderr.strip():
        return True
    if not stdout.strip():
        return True
    return False


def _normalize_failure_diagnostics(structured_result: dict[str, Any]) -> None:
    if structured_result.get("success") is not False:
        return
    diagnostic_keys = {
        "diagnostics",
        "attempted_urls",
        "status_code",
        "exception",
        "exception_type",
        "response_preview",
        "parse_strategy",
    }
    if any(key in structured_result for key in diagnostic_keys):
        return
    structured_result.setdefault("diagnostics_missing", True)
    structured_result.setdefault(
        "diagnostics_required",
        [
            "attempted_urls",
            "status_code",
            "exception_type",
            "exception_message",
            "response_preview",
            "parse_strategy",
            "retryable",
        ],
    )


def _with_min_tokens(model_config: ModelConfig, max_output_tokens: int) -> ModelConfig:
    return snapshot_model_config(model_config, min_output_tokens=max_output_tokens)


def _fallback_reply(structured_result: dict[str, Any]) -> str:
    if structured_result.get("success") is False:
        message = str(structured_result.get("message") or structured_result.get("error") or "").strip()
        return f"抱歉，通用技能运行失败。{message}" if message else "抱歉，通用技能运行失败。"
    return "通用技能已运行完成，结果已展示在运行输出中。"
