from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.capability_manifest import (
    CapabilityAuthorizationError,
    CapabilityManifestBuilder,
    general_skill_snapshot_digest,
    tool_snapshot_digest,
)
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.core.harness_agent import HarnessExecutionCancelled
from app.core.task_request_compiler import CapabilityDescriptor, CapabilityManifest
from app.core.tool_replay_policy import ToolReplayPolicy
from app.db.models import (
    ChatSession,
    GeneralSkill,
    HarnessInvocationRecord,
    ModelConfig,
    Skill,
    Tool,
    new_id,
    utc_now,
)
from app.capabilities.local_general_skill import (
    package_from_row,
    runtime_snapshot_from_package,
)
from app.general_skills.runner import (
    GeneralSkillExecutionCancelled,
    GeneralSkillRunner,
)
from app.harness import (
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
)
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


class HarnessCapabilityInvoker:
    """Executes only capabilities frozen into one TaskFrame manifest."""

    def __init__(
        self,
        db: Any,
        *,
        tenant_id: str,
        session: ChatSession,
        task_frame_id: str,
        model_config: ModelConfig,
        manifest: CapabilityManifest,
        active_skill: Skill | None,
        active_step_id: str | None,
        agent_id: str | None,
        run_id: str | None = None,
        is_cancelled: Any | None = None,
        ensure_execution_lease: Any | None = None,
        trace_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.session = session
        self.task_frame_id = task_frame_id
        self.model_config = model_config
        self.manifest = manifest
        self.active_skill = active_skill
        self.active_skill_id = (
            active_skill.skill_id if active_skill is not None else None
        )
        self.active_step_id = active_step_id
        self.agent_id = agent_id
        self.is_cancelled = is_cancelled
        self.ensure_execution_lease = ensure_execution_lease
        self.trace_sink = trace_sink
        self.run_id = str(run_id or new_id("hrun"))
        self.workspace_root = _workspace_root(
            tenant_id, session.id, task_frame_id
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._file_registry = build_file_tool_registry()
        self._file_executor = HarnessExecutor(self._file_registry)
        self._file_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=tenant_id,
            workspace_root=self.workspace_root,
        )
        self._descriptors = {
            item.name: item
            for item in manifest.available
            if item.available
        }

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._raise_if_cancelled()
        if callable(self.ensure_execution_lease):
            self.ensure_execution_lease()
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            return _failure(
                "TOOL_NOT_AVAILABLE",
                "该能力不在当前 TaskFrame 的冻结清单中。",
            )
        current_descriptor = self._currently_authorized_descriptor(descriptor)
        if current_descriptor is None:
            return _failure(
                "CAPABILITY_AUTHORIZATION_REVOKED",
                "该能力在当前 HarnessRun 执行前已被撤权、归档或改为不可用。",
            )
        self._raise_if_cancelled()
        logical_action_key = self._logical_action_key(
            descriptor,
            arguments,
        )
        if logical_action_key:
            replayed = self._replay_or_block(logical_action_key)
            if replayed is not None:
                return replayed
        call_id = new_id("hcall")
        invocation = HarnessInvocationRecord(
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            task_id=self.task_frame_id,
            run_id=self.run_id,
            call_id=call_id,
            tool_name=name,
            request_digest=_request_digest(name, arguments),
            logical_action_key=logical_action_key,
            status="started",
            arguments_json=_audit_arguments(arguments),
        )
        self.db.add(invocation)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if logical_action_key:
                replayed = self._replay_or_block(logical_action_key)
                if replayed is not None:
                    return replayed
            raise
        try:
            self._raise_if_cancelled()
            if descriptor.kind == "file":
                result = self._invoke_file(name, arguments, call_id=call_id)
            elif descriptor.kind == "general_skill":
                result = self._invoke_general_skill(
                    descriptor.capability_id,
                    descriptor.metadata,
                    arguments,
                )
            elif descriptor.kind == "knowledge":
                result = self._search_knowledge(
                    _intersect_knowledge_metadata(
                        descriptor.metadata,
                        current_descriptor.metadata,
                    ),
                    arguments,
                )
            elif descriptor.kind == "tool":
                result = self._invoke_external_tool(
                    descriptor.capability_id,
                    descriptor.metadata,
                    name,
                    arguments,
                )
            else:
                result = _failure(
                    "UNSUPPORTED_CAPABILITY", "不支持的 Harness 能力类型。"
                )
        except HarnessExecutionCancelled:
            invocation.status = "cancelled"
            invocation.logical_action_key = None
            invocation.finished_at = utc_now()
            invocation.updated_at = utc_now()
            self.db.add(invocation)
            self.db.commit()
            raise
        except Exception as exc:
            result = _failure("HARNESS_TOOL_ERROR", str(exc))
        if result.get("success") is True:
            invocation.status = "completed"
        elif _failure_was_not_sent(result):
            # Configuration/authorization failures are known to occur before
            # the external side effect. Release the stable claim so a later
            # turn can retry after the configuration is repaired.
            invocation.status = "failed"
            invocation.logical_action_key = None
        else:
            # A timeout, HTTP error, connection reset, or MCP error can happen
            # after the provider accepted a write. Keep the claim and require
            # reconciliation instead of replaying the side effect.
            invocation.status = "outcome_unknown"
        invocation.result_json = _audit_result(result)
        invocation.response_cache_json = dict(result)
        invocation.finished_at = utc_now()
        invocation.updated_at = utc_now()
        self.db.add(invocation)
        self.db.commit()
        return result

    def _logical_action_key(
        self,
        descriptor: CapabilityDescriptor,
        arguments: dict[str, Any],
    ) -> str | None:
        if descriptor.kind != "tool":
            return None
        tool = self.db.get(Tool, descriptor.capability_id)
        if tool is None or tool.tenant_id != self.tenant_id:
            return None
        configured, key_fields = ToolReplayPolicy.configuration(
            tool.config_json if isinstance(tool.config_json, dict) else {},
            tool.input_schema if isinstance(tool.input_schema, dict) else {},
        )
        if configured is False:
            return None
        if configured is not True and not ToolReplayPolicy.default_replay_enabled(
            str(tool.method or "")
        ):
            return None
        key_arguments = ToolReplayPolicy.arguments(arguments, key_fields)
        signature = ToolReplayPolicy.signature(tool.name, key_arguments)
        canonical = json.dumps(
            {
                "tenant_id": self.tenant_id,
                "task_frame_id": self.task_frame_id,
                "step_id": self.active_step_id,
                "tool_id": tool.id,
                "signature": signature,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _replay_or_block(
        self,
        logical_action_key: str,
    ) -> dict[str, Any] | None:
        prior = self.db.exec(
            select(HarnessInvocationRecord).where(
                HarnessInvocationRecord.logical_action_key
                == logical_action_key
            )
        ).first()
        if prior is None:
            return None
        if (
            prior.status == "completed"
            and prior.response_cache_json.get("success") is True
        ):
            return _replayed_result(prior)
        return _failure(
            "TOOL_CALL_OUTCOME_UNKNOWN",
            (
                "相同副作用调用已有未完成的持久化记录；为避免重复提交，"
                "Harness 不会自动重试，请先核对外部系统状态。"
            ),
        )

    def _raise_if_cancelled(self) -> None:
        if callable(self.is_cancelled) and self.is_cancelled():
            raise HarnessExecutionCancelled(
                "Harness execution was cancelled before a capability call."
            )

    def _currently_authorized_descriptor(
        self,
        frozen: CapabilityDescriptor,
    ) -> CapabilityDescriptor | None:
        try:
            current = CapabilityManifestBuilder(self.db).build(
                self.tenant_id,
                self.agent_id,
                self.active_skill,
                self.active_step_id,
            )
        except CapabilityAuthorizationError:
            return None
        return next(
            (
                item
                for item in current.available
                if item.available
                and item.capability_id == frozen.capability_id
                and item.name == frozen.name
                and item.kind == frozen.kind
            ),
            None,
        )

    def _invoke_file(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> dict[str, Any]:
        result = self._file_executor.execute(
            self._file_context,
            HarnessToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
            ),
        )
        if result.success:
            data = dict(result.data or {})
            artifacts: list[dict[str, Any]] = []
            if name in {"write_file", "edit_file", "copy_file", "move_file"}:
                artifact_path = str(
                    data.get("path")
                    or data.get("destination_path")
                    or ""
                ).strip()
                if artifact_path:
                    artifacts.append(
                        {
                            "type": "workspace_file",
                            "task_frame_id": self.task_frame_id,
                            "path": artifact_path,
                            "sha256": data.get("sha256"),
                            "size": data.get("size"),
                            "operation": name,
                        }
                    )
            return {
                "success": True,
                "data": data,
                "artifacts": artifacts,
                "duration_ms": result.duration_ms,
            }
        return {
            "success": False,
            "error": {
                "code": result.error.code if result.error else "FILE_TOOL_ERROR",
                "message": (
                    result.error.message
                    if result.error
                    else "文件工具执行失败。"
                ),
                "retryable": bool(result.error.retryable) if result.error else False,
                "details": dict(result.error.details) if result.error else {},
            },
            "duration_ms": result.duration_ms,
        }

    def _invoke_general_skill(
        self,
        capability_id: str,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        skill = self.db.get(GeneralSkill, capability_id)
        if (
            skill is None
            or skill.tenant_id != self.tenant_id
            or skill.status != "published"
        ):
            return _failure(
                "SKILL_NOT_AVAILABLE",
                "通用技能在当前 HarnessRun 中已不可用。",
            )
        digest = general_skill_snapshot_digest(skill)
        if digest != str(metadata.get("content_digest") or ""):
            return _failure(
                "CAPABILITY_SNAPSHOT_CHANGED",
                "通用技能内容在当前 HarnessRun 启动后发生变化，请重新规划。",
            )
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "通用技能 query 不能为空。")
        operation = str(arguments.get("operation") or "execute").strip().lower()
        if operation not in {"read", "execute"}:
            return _failure(
                "INVALID_ARGUMENTS",
                "通用技能 operation 只能是 read 或 execute。",
            )
        if operation == "read":
            return self._read_general_skill_package(skill, metadata, query)

        package = package_from_row(skill)
        snapshot = runtime_snapshot_from_package(skill, package)
        try:
            response = GeneralSkillRunner().run(
                snapshot,
                query,
                self.model_config,
                self.session.user_id,
                max_attempts=_general_skill_max_attempts(skill),
                event_sink=lambda item: self._emit_trace(
                    "general_skill_trace",
                    {
                        "skill_slug": skill.slug,
                        "skill_name": skill.name,
                        "operation": "execute",
                        **item,
                    },
                ),
                workspace_root=self.workspace_root,
                is_cancelled=self.is_cancelled,
            )
        except GeneralSkillExecutionCancelled as exc:
            self._emit_trace(
                "general_skill_run_finished",
                {
                    "skill_slug": skill.slug,
                    "operation": "execute",
                    "success": False,
                    "status": "cancelled",
                },
            )
            raise HarnessExecutionCancelled(str(exc)) from exc

        structured = (
            dict(response.structured_result)
            if isinstance(response.structured_result, dict)
            else {}
        )
        declared_success = structured.get("success")
        succeeded = True if declared_success is None else bool(declared_success)
        data = {
            "kind": "general_skill",
            "slug": response.skill_slug,
            "operation": response.operation,
            "query": query,
            "reply": response.reply,
            "structured_result": structured,
            "stdout": response.stdout,
            "stderr": response.stderr,
            "generated_code": response.generated_code,
            "execution_trace": response.execution_trace,
        }
        self._emit_trace(
            "general_skill_run_finished",
            {
                "skill_slug": response.skill_slug,
                "operation": response.operation,
                "success": succeeded,
                "structured_result": structured,
                "stdout_preview": response.stdout[:600],
                "stderr_preview": response.stderr[:600],
            },
        )
        if succeeded:
            return {"success": True, "data": data}
        return {
            "success": False,
            "data": data,
            "error": {
                "code": str(
                    structured.get("error") or "GENERAL_SKILL_EXECUTION_FAILED"
                ),
                "message": str(
                    structured.get("message")
                    or response.reply
                    or "通用技能执行失败。"
                ),
                "retryable": bool(structured.get("retryable")),
            },
        }

    def _read_general_skill_package(
        self,
        skill: GeneralSkill,
        metadata: dict[str, Any],
        query: str,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "data": {
                "kind": "general_skill",
                "slug": metadata.get("slug"),
                "operation": "read",
                "query": query,
                "package": _skill_package_preview(skill),
                "notice": (
                    "技能包说明已加载到当前隔离 Harness transcript；"
                    "如需运行技能，请使用 operation=execute。"
                ),
            },
        }

    def _emit_trace(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if callable(self.trace_sink):
            self.trace_sink(event_type, payload)

    def _search_knowledge(
        self, metadata: dict[str, Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "知识检索 query 不能为空。")
        allowed = {
            str(item)
            for item in metadata.get("allowed_knowledge_base_ids") or []
            if str(item).strip()
        }
        requested = {
            str(item)
            for item in arguments.get("knowledge_base_ids") or []
            if str(item).strip()
        }
        selected = sorted(requested & allowed) if requested else sorted(allowed)
        if requested and not selected:
            return _failure(
                "KNOWLEDGE_NOT_AVAILABLE",
                "请求的知识库不在当前 TaskFrame 授权范围内。",
            )
        version_by_base = (
            metadata.get("knowledge_version_by_base_id")
            if isinstance(metadata.get("knowledge_version_by_base_id"), dict)
            else {}
        )
        selected_version_ids = [
            str(version_by_base[kb_id])
            for kb_id in selected
            if str(version_by_base.get(kb_id) or "").strip()
        ]
        response = KnowledgeService(self.db).search(
            KnowledgeSearchRequest(
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                query=query,
                mode="chat",
                knowledge_base_ids=selected,
                knowledge_base_version_ids=selected_version_ids,
                max_chunks=max(
                    1, min(int(arguments.get("max_chunks") or 8), 12)
                ),
            ),
            self.model_config,
        )
        payload = response.model_dump(mode="json")
        return {
            "success": True,
            "data": payload,
            "citations": knowledge_citations_from_results([payload]),
        }

    def _invoke_external_tool(
        self,
        capability_id: str,
        metadata: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        source_tool_name = str(
            metadata.get("source_tool_name") or name
        ).strip()
        tool = self.db.get(Tool, capability_id)
        if (
            tool is None
            or tool.tenant_id != self.tenant_id
            or not tool.enabled
            or tool.name != source_tool_name
        ):
            return _failure(
                "TOOL_NOT_AVAILABLE",
                "工具在当前 HarnessRun 中已不可用。",
            )
        if tool_snapshot_digest(self.db, tool) != str(
            metadata.get("content_digest") or ""
        ):
            return _failure(
                "CAPABILITY_SNAPSHOT_CHANGED",
                "工具配置在当前 HarnessRun 启动后发生变化，请重新规划。",
            )
        result = ToolExecutor(self.db).execute(
            self.tenant_id,
            ToolCall(name=source_tool_name, arguments=arguments),
            active_skill_id=self.active_skill_id,
            agent_id=self.agent_id,
        )
        return result.model_dump(mode="json")


def _workspace_root(
    tenant_id: str, session_id: str, task_frame_id: str
) -> Path:
    return harness_task_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
    )


def _general_skill_max_attempts(skill: GeneralSkill) -> int:
    runtime_config = (
        skill.runtime_config_json
        if isinstance(skill.runtime_config_json, dict)
        else {}
    )
    try:
        configured = int(runtime_config.get("max_attempts") or 3)
    except (TypeError, ValueError):
        configured = 3
    return max(1, min(configured, 10))


def _intersect_knowledge_metadata(
    frozen: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    current_ids = {
        str(item)
        for item in current.get("allowed_knowledge_base_ids") or []
        if str(item).strip()
    }
    frozen_ids = [
        str(item)
        for item in frozen.get("allowed_knowledge_base_ids") or []
        if str(item).strip() and str(item) in current_ids
    ]
    version_by_base = (
        frozen.get("knowledge_version_by_base_id")
        if isinstance(frozen.get("knowledge_version_by_base_id"), dict)
        else {}
    )
    filtered_versions = {
        kb_id: str(version_by_base[kb_id])
        for kb_id in frozen_ids
        if str(version_by_base.get(kb_id) or "").strip()
    }
    return {
        **frozen,
        "allowed_knowledge_base_ids": frozen_ids,
        "allowed_knowledge_base_version_ids": list(filtered_versions.values()),
        "knowledge_version_by_base_id": filtered_versions,
    }


def _failure(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
        },
    }


def _skill_package_preview(
    skill: GeneralSkill,
    *,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    package = package_from_row(skill)
    remaining = max_chars
    files: list[dict[str, Any]] = []
    for item in package.files:
        content = str(item.content or "")
        preview = content[:remaining]
        remaining -= len(preview)
        files.append(
            {
                "path": item.path,
                "size": item.size,
                "mime_type": item.mime_type,
                "content_preview": preview,
                "truncated": len(preview) < len(content),
            }
        )
        if remaining <= 0:
            break
    return {
        "package_id": package.package_id,
        "version": package.version,
        "digest": package.digest,
        "entrypoint": package.entrypoint,
        "file_count": len(package.files),
        "files": files,
        "truncated": len(files) < len(package.files)
        or any(bool(item.get("truncated")) for item in files),
    }


def _failure_was_not_sent(result: dict[str, Any]) -> bool:
    error = result.get("error")
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    return code in {
        "NOT_FOUND",
        "DISABLED",
        "NOT_ALLOWED",
        "UNSUPPORTED_TOOL_TYPE",
        "TOOL_NOT_AVAILABLE",
        "CAPABILITY_AUTHORIZATION_REVOKED",
        "CAPABILITY_SNAPSHOT_CHANGED",
        "INVALID_ARGUMENTS",
    }


def _replayed_result(invocation: HarnessInvocationRecord) -> dict[str, Any]:
    result = dict(invocation.response_cache_json or {})
    data = result.get("data")
    replay_metadata = {
        "idempotent_replay": True,
        "replayed_from_invocation_id": invocation.id,
    }
    if isinstance(data, dict):
        result["data"] = {**data, **replay_metadata}
    else:
        result["data"] = {
            "result": data,
            **replay_metadata,
        }
    result["idempotent_replay"] = True
    return result


def _request_digest(name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    audited: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = str(key).lower()
        if any(
            token in lowered
            for token in ("content", "secret", "token", "password", "api_key")
        ):
            audited[str(key)] = "<redacted>"
        else:
            audited[str(key)] = value
    return audited


def _audit_result(result: dict[str, Any]) -> dict[str, Any]:
    audited = dict(result)
    data = audited.get("data")
    if isinstance(data, dict):
        audited["data"] = {
            key: (
                "<redacted>"
                if str(key).lower() in {"content", "instructions", "stdout", "stderr"}
                else value
            )
            for key, value in data.items()
        }
    citations = audited.get("citations")
    if isinstance(citations, list):
        audited["citations"] = [
            {
                key: value
                for key, value in item.items()
                if key not in {"content", "excerpt"}
            }
            for item in citations
            if isinstance(item, dict)
        ]
    return audited
