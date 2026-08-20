from __future__ import annotations

import base64
import hashlib
import inspect
import json
import mimetypes
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.capabilities.local_general_skill import (
    package_from_row,
    runtime_snapshot_from_package,
)
from app.core.capability_discovery import (
    CAPABILITY_SEARCH_MAX_RESULTS,
    catalog_entry,
    model_descriptor,
    search_capability_descriptors,
)
from app.core.capability_manifest import (
    CapabilityAuthorizationError,
    CapabilityManifestBuilder,
    general_skill_snapshot_digest,
    tool_snapshot_digest,
)
from app.core.harness_agent import HarnessExecutionCancelled
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.core.task_request_compiler import CapabilityDescriptor, CapabilityManifest
from app.core.tool_replay_policy import ToolReplayPolicy
from app.db.models import (
    ChatSession,
    GeneralSkill,
    HarnessInvocationRecord,
    ModelConfig,
    Skill,
    Tool,
    UIConfig,
    new_id,
    utc_now,
)
from app.general_skills.runner import (
    GeneralSkillExecutionCancelled,
    GeneralSkillRunner,
)
from app.harness import (
    HarnessArtifactAccessError,
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
    open_harness_artifact,
    publish_changed_harness_artifacts,
    register_command_tools,
    snapshot_harness_workspace,
)
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.harness.errors import HarnessExecutionError
from app.harness.sandbox import parse_network_policy
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


_INLINE_JSON_TOOL_RESULT_MAX_CHARS = 50_000
_RPS_EVIDENCE_MAX_CHARS = 46_000
_RPS_EVIDENCE_TEXT_MAX_CHARS = 8_000
_INTERNAL_TOOL_RESULT_DIRECTORY = ".harness/tool-results"
_INTERNAL_RPS_DRAFT_DIRECTORY = ".harness/rps-drafts"
_SANDBOX_JSON_FILE_KIND = "sandbox_json_file"
_TOOL_SIDE_EFFECT_SCHEMA_KEY = "x-staffdeck-side-effect"
_MCP_WORKSPACE_FILE_TRANSFER_SCHEMA_KEY = "x-staffdeck-workspace-file-transfer"
_XIAOMING_AGENT_ID = "agent_4a018d61af2f4589"
_RPS_SKILL_ID = "rps_registration"
_RPS_EVIDENCE_STEP_ID = "retrieve_evidence"
_RPS_EVIDENCE_TOOL_NAMES = {
    "rps_mcp.remote_rag_search",
    "rps_mcp.rps_rag_retrieval",
}
_RPS_BUILD_TOOL_ID = "tool_83337016feb94138"
_RPS_BUILD_TOOL_NAME = "rps_mcp.registration_folder_build"


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
        initially_activated_names: set[str] | None = None,
        is_cancelled: Any | None = None,
        ensure_execution_lease: Any | None = None,
        trace_sink: Callable[[str, dict[str, Any]], None] | None = None,
        step_deadline_monotonic: float | None = None,
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
        self.step_deadline_monotonic = step_deadline_monotonic
        self.run_id = str(run_id or new_id("hrun"))
        self.workspace_root = _workspace_root(
            tenant_id, session.id, task_frame_id
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._workspace_snapshot = snapshot_harness_workspace(self.workspace_root)
        ui_config = self.db.get(UIConfig, tenant_id)
        sandbox_mode = parse_network_policy(
            getattr(ui_config, "sandbox_network_mode", None) if ui_config else None
        )
        sandbox_domains = tuple(
            str(item).strip()
            for item in (getattr(ui_config, "sandbox_allowed_domains", []) if ui_config else [])
            if str(item).strip()
        )
        self._file_registry = build_file_tool_registry()
        register_command_tools(self._file_registry)
        self._file_executor = HarnessExecutor(self._file_registry)
        self._file_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=tenant_id,
            workspace_root=self.workspace_root,
            sandbox_network_mode=sandbox_mode,
            sandbox_allowed_domains=sandbox_domains,
        )
        self._sandbox_network_mode = sandbox_mode
        self._sandbox_allowed_domains = sandbox_domains
        self._descriptors = {
            item.name: item
            for item in manifest.available
            if item.available
        }
        # ``None`` preserves compatibility for trusted direct callers.  The
        # Harness v2 engine always supplies the projected model allowlist so a
        # guessed hidden name cannot bypass progressive disclosure.
        self._activated_names = (
            set(self._descriptors)
            if initially_activated_names is None
            else {
                name
                for name in initially_activated_names
                if name in self._descriptors
            }
        )
        # GeneralSkill is a two-stage capability in Harness v2.  The task agent
        # must inspect the frozen package before it can decide whether the
        # instructions are sufficient or executable code is actually needed.
        self._loaded_general_skill_ids: set[str] = set()

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
        if name not in self._activated_names:
            return _failure(
                "CAPABILITY_NOT_ACTIVATED",
                "该能力尚未在当前 AgentLoop 中展开；请先调用 capability_describe。",
            )
        current_descriptor = self._currently_authorized_descriptor(descriptor)
        if current_descriptor is None:
            return _failure(
                "CAPABILITY_AUTHORIZATION_REVOKED",
                "该能力在当前 HarnessRun 执行前已被撤权、归档或改为不可用。",
            )
        side_effect = self._external_tool_side_effect(descriptor)
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
            if descriptor.kind == "internal":
                result = self._invoke_internal(name, arguments)
            elif descriptor.kind == "file":
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
                    call_id=call_id,
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
        elif side_effect == "read" or _failure_was_not_sent(result):
            # Read-only failures and pre-send validation failures have a known
            # outcome. Release any stable claim so corrected arguments can retry.
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

    def discover_artifacts(self) -> list[dict[str, Any]]:
        """Publish every user-facing file changed during this AgentLoop run."""

        try:
            discovered = publish_changed_harness_artifacts(
                self.workspace_root,
                self.task_frame_id,
                self._workspace_snapshot,
                operation="workspace_discovery",
                path_filter=_is_user_facing_workspace_file,
            )
        except (HarnessArtifactAccessError, OSError):
            return []
        artifacts: list[dict[str, Any]] = []
        for raw in discovered:
            item = dict(raw)
            relative_path = str(item.get("path") or "")
            display_name = Path(relative_path).name
            item.update(
                {
                    "sandbox_path": _sandbox_path(relative_path),
                    "display_name": display_name,
                    "content_type": (
                        mimetypes.guess_type(display_name)[0]
                        or "application/octet-stream"
                    ),
                    "source": "harness.workspace_discovery",
                }
            )
            artifacts.append(item)
        return artifacts

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

    def _external_tool_side_effect(
        self,
        descriptor: CapabilityDescriptor,
    ) -> str | None:
        if descriptor.kind != "tool":
            return None
        tool = self.db.get(Tool, descriptor.capability_id)
        if tool is None or tool.tenant_id != self.tenant_id:
            return None
        return _tool_side_effect(tool)

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
            if name == "publish_artifact":
                artifact_path = str(data.get("path") or "").strip()
                if artifact_path:
                    artifacts.append(
                        {
                            "type": "workspace_file",
                            "task_frame_id": self.task_frame_id,
                            "path": artifact_path,
                            "sandbox_path": _sandbox_path(artifact_path),
                            "sha256": data.get("sha256"),
                            "size": data.get("size"),
                            "display_name": data.get("display_name"),
                            "description": data.get("description"),
                            "content_type": data.get("content_type"),
                            "operation": "publish_artifact",
                            "source": "harness",
                        }
                    )
            elif name in {"write_file", "edit_file", "copy_file", "move_file"}:
                data["published"] = False
                data["publication_hint"] = (
                    "文件已写入隔离工作区；如需提供给用户下载，请在校验完成后"
                    "显式调用 publish_artifact。"
                )
            return {
                "success": True,
                "data": _model_visible_file_result(data),
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

    def _invoke_internal(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "capability_search":
            return self._search_capabilities(arguments)
        if name == "capability_describe":
            return self._describe_capabilities(arguments)
        return _failure(
            "UNSUPPORTED_INTERNAL_CAPABILITY",
            "不支持的 Harness 内部能力。",
        )

    def _search_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "capability_search query 不能为空。")
        raw_kinds = arguments.get("kinds")
        allowed_kinds = {"general_skill", "tool", "knowledge", "file"}
        kinds: set[str] | None = None
        if raw_kinds is not None:
            if not isinstance(raw_kinds, list) or any(
                not isinstance(item, str) or item not in allowed_kinds
                for item in raw_kinds
            ):
                return _failure(
                    "INVALID_ARGUMENTS",
                    "capability_search kinds 包含不支持的能力类型。",
                )
            kinds = set(raw_kinds)
        raw_limit = arguments.get("limit", 8)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_search limit 必须是整数。",
            )
        limit = max(1, min(raw_limit, CAPABILITY_SEARCH_MAX_RESULTS))
        matches = search_capability_descriptors(
            self._descriptors.values(),
            query,
            kinds=kinds,
            limit=limit,
        )
        payload = {
            "snapshot_revision": self.manifest.snapshot_revision,
            "query": query,
            "matches": [
                catalog_entry(item).model_dump(mode="json") for item in matches
            ],
            "match_count": len(matches),
            "notice": (
                "搜索结果仍未激活；选择后调用 capability_describe 加载完整 schema。"
            ),
        }
        self._emit_trace(
            "capability_search_completed",
            {
                "query": query,
                "kinds": sorted(kinds) if kinds else [],
                "match_count": len(matches),
                "matches": [item.name for item in matches],
            },
        )
        return {"success": True, "data": payload}

    def _describe_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_refs = arguments.get("capabilities")
        if not isinstance(raw_refs, list) or not raw_refs or len(raw_refs) > 8:
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_describe capabilities 必须包含 1 到 8 个能力名称或 ID。",
            )
        refs = [str(item or "").strip() for item in raw_refs]
        if any(not item for item in refs) or len(set(refs)) != len(refs):
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_describe capabilities 不能包含空值或重复项。",
            )
        by_ref = {
            ref: descriptor
            for descriptor in self._descriptors.values()
            for ref in (descriptor.capability_id, descriptor.name)
        }
        activated: list[dict[str, Any]] = []
        not_found: list[str] = []
        revoked: list[str] = []
        for ref in refs:
            descriptor = by_ref.get(ref)
            if descriptor is None or descriptor.kind == "internal":
                not_found.append(ref)
                continue
            if self._currently_authorized_descriptor(descriptor) is None:
                revoked.append(ref)
                continue
            activated.append(model_descriptor(descriptor).model_dump(mode="json"))
            self._activated_names.add(descriptor.name)
        self._emit_trace(
            "capability_described",
            {
                "requested": refs,
                "activated": [item["name"] for item in activated],
                "not_found": not_found,
                "revoked": revoked,
            },
        )
        if not activated:
            return _failure(
                "CAPABILITY_NOT_AVAILABLE",
                "请求的能力不存在或已不可用。",
            )
        return {
            "success": True,
            "data": {
                "snapshot_revision": self.manifest.snapshot_revision,
                "activated_capabilities": activated,
                "not_found": not_found,
                "revoked": revoked,
                "notice": "以上能力已在当前 TaskFrame AgentLoop 中激活。",
            },
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
        # Fail safe for old callers that omit operation: loading instructions is
        # non-executing and gives the AgentLoop enough context to choose its next
        # action.  Never turn an omitted field into generated code.
        operation = str(arguments.get("operation") or "read").strip().lower()
        if operation not in {"read", "execute"}:
            return _failure(
                "INVALID_ARGUMENTS",
                "通用技能 operation 只能是 read 或 execute。",
            )
        if operation == "read":
            result = self._read_general_skill_package(skill, metadata, query)
            self._loaded_general_skill_ids.add(skill.id)
            self._emit_trace(
                "general_skill_trace",
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "operation": "read",
                    "phase": "instructions_loaded",
                    "message": "已加载技能说明，等待 AgentLoop 判断执行方式",
                },
            )
            return result

        if skill.id not in self._loaded_general_skill_ids:
            return _failure(
                "GENERAL_SKILL_NOT_INSPECTED",
                (
                    "执行技能包前必须先使用 operation=read 加载说明；"
                    "由 AgentLoop 阅读后判断是否确实需要运行代码。"
                ),
            )

        package = package_from_row(skill)
        snapshot = runtime_snapshot_from_package(skill, package)
        try:
            run_kwargs = {
                "max_attempts": _general_skill_max_attempts(skill),
                "event_sink": lambda item: self._emit_trace(
                    "general_skill_trace",
                    {
                        "skill_slug": skill.slug,
                        "skill_name": skill.name,
                        "operation": "execute",
                        **item,
                    },
                ),
            }
            run_kwargs.update(
                workspace_root=self.workspace_root,
                is_cancelled=self.is_cancelled,
                sandbox_network_mode=self._sandbox_network_mode,
                sandbox_allowed_domains=self._sandbox_allowed_domains,
            )
            runner = GeneralSkillRunner()
            supported = inspect.signature(runner.run).parameters
            if "sandbox_network_mode" not in supported:
                if self._sandbox_network_mode != "all":
                    raise HarnessExecutionError(
                        "SANDBOX_POLICY_UNSUPPORTED",
                        "通用技能执行器不支持当前租户的沙盒网络策略，已拒绝执行。",
                    )
                # Legacy runners cannot weaken an unrestricted policy. Keep
                # this compatibility path only for the explicit `all` mode.
                run_kwargs.pop("sandbox_network_mode", None)
                run_kwargs.pop("sandbox_allowed_domains", None)
            response = runner.run(
                snapshot, query, self.model_config, self.session.user_id, **run_kwargs
            )
        except HarnessExecutionError as exc:
            code = str(exc.error.code or "SANDBOX_EXECUTION_FAILED")
            message = str(exc.error.message or exc)
            self._emit_trace(
                "general_skill_run_finished",
                {
                    "skill_slug": skill.slug,
                    "operation": "execute",
                    "success": False,
                    "error": code,
                    "message": message,
                },
            )
            return _failure(
                code,
                message,
                retryable=False,
                infrastructure_failure=True,
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
        artifact_errors: list[dict[str, str]] = [
            {
                "path": str(item.get("path") or ""),
                "code": str(item.get("code") or "artifact_declaration_invalid"),
                "message": str(item.get("message") or "产物声明无效。"),
            }
            for item in (structured.get("artifact_errors") or [])[:20]
            if isinstance(item, dict)
        ]
        artifacts, publish_errors = self._general_skill_artifacts(
            response.artifacts
            or [
                item
                for item in (structured.get("artifacts") or [])[:20]
                if isinstance(item, dict)
            ],
            skill_slug=skill.slug,
        )
        artifact_errors.extend(publish_errors)
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
            "artifact_errors": artifact_errors,
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
            return {"success": True, "data": data, "artifacts": artifacts}
        return {
            "success": False,
            "data": data,
            "artifacts": artifacts,
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

    def _general_skill_artifacts(
        self,
        declared: list[dict[str, Any]],
        *,
        skill_slug: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        artifacts: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        for item in declared[:20]:
            path = str(item.get("path") or "").strip()
            if not path:
                warnings.append(
                    {
                        "path": "",
                        "code": "artifact_publish_failed",
                        "message": "Artifact path cannot be empty.",
                    }
                )
                continue
            opened = None
            try:
                opened = open_harness_artifact(self.workspace_root, path)
                digest = opened.sha256()
                display_name = _safe_artifact_label(
                    item.get("display_name"),
                    fallback=opened.filename,
                    max_length=180,
                )
                description = _safe_artifact_label(
                    item.get("description"),
                    fallback="",
                    max_length=500,
                )
                artifacts.append(
                    {
                        "type": "workspace_file",
                        "task_frame_id": self.task_frame_id,
                        "path": path,
                        "sandbox_path": _sandbox_path(path),
                        "sha256": digest,
                        "size": opened.size,
                        "display_name": display_name,
                        "description": description or None,
                        "content_type": (
                            mimetypes.guess_type(display_name)[0]
                            or mimetypes.guess_type(opened.filename)[0]
                            or "application/octet-stream"
                        ),
                        "operation": "general_skill.execute",
                        "source": f"general_skill.{skill_slug}",
                    }
                )
            except (HarnessArtifactAccessError, OSError) as exc:
                warnings.append(
                    {
                        "path": path,
                        "code": "artifact_publish_failed",
                        "message": str(exc),
                    }
                )
            finally:
                if opened is not None:
                    opened.close()
        if warnings:
            self._emit_trace(
                "general_skill_artifact_rejected",
                {"skill_slug": skill_slug, "warnings": warnings},
            )
        return artifacts, warnings

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
                    "请由 AgentLoop 判断下一步：仅含 prompt、规则或示例时直接应用说明，"
                    "并按任务需要调用知识库、原装 Tool 或文件工具；只有确实需要运行"
                    "技能包代码时才使用 operation=execute。"
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
        *,
        call_id: str,
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
        try:
            resolved_arguments = self._resolve_json_tool_result_references(
                arguments,
                schema=(tool.input_schema if isinstance(tool.input_schema, dict) else None),
            )
            if tool.tool_type == "mcp":
                resolved_arguments = self._resolve_mcp_workspace_file(
                    resolved_arguments,
                    schema=(
                        tool.input_schema
                        if isinstance(tool.input_schema, dict)
                        else None
                    ),
                )
                resolved_arguments = self._resolve_xiaoming_rps_draft_files(
                    tool,
                    resolved_arguments,
                )
        except HarnessExecutionError as exc:
            return _failure(
                exc.error.code,
                exc.error.message,
                retryable=exc.error.retryable,
                details=dict(exc.error.details),
            )
        result = ToolExecutor(self.db).execute(
            self.tenant_id,
            ToolCall(name=source_tool_name, arguments=resolved_arguments),
            active_skill_id=self.active_skill_id,
            agent_id=self.agent_id,
            timeout_seconds_override=self._remaining_step_seconds(),
            request_user_id=getattr(self.session, "user_id", None) or None,
        )
        payload = result.model_dump(mode="json")
        if payload.get("success") is not True:
            return payload
        if self._is_xiaoming_rps_evidence(source_tool_name):
            payload["data"] = _project_rps_evidence_payload(payload.get("data"))
        data = payload.get("data")
        if not isinstance(data, (dict, list)):
            return payload
        try:
            serialized = json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return payload
        if len(serialized) <= _INLINE_JSON_TOOL_RESULT_MAX_CHARS:
            return payload
        stored = self._file_executor.execute(
            self._file_context,
            HarnessToolCall(
                call_id=f"{call_id}-result",
                name="write_file",
                arguments={
                    "path": f"{_INTERNAL_TOOL_RESULT_DIRECTORY}/{call_id}.json",
                    "content": serialized,
                    "create_parents": True,
                },
            ),
        )
        if not stored.success:
            return _failure(
                "TOOL_RESULT_PERSIST_FAILED",
                "外部工具已返回结果，但完整 JSON 无法写入当前 TaskFrame 沙箱。",
                cause={
                    "code": (
                        stored.error.code
                        if stored.error is not None
                        else "FILE_TOOL_ERROR"
                    ),
                    "message": (
                        stored.error.message
                        if stored.error is not None
                        else "沙箱文件写入失败。"
                    ),
                },
            )
        stored_data = dict(stored.data or {})
        relative_path = str(stored_data.get("path") or "").strip()
        payload["data"] = {
            "kind": _SANDBOX_JSON_FILE_KIND,
            "sandbox_path": _sandbox_path(relative_path),
            "size": stored_data.get("size"),
            "sha256": stored_data.get("sha256"),
        }
        return payload

    def _is_xiaoming_rps_evidence(self, tool_name: str) -> bool:
        return (
            self.agent_id == _XIAOMING_AGENT_ID
            and self.active_skill_id == _RPS_SKILL_ID
            and str(self.active_step_id or "") == _RPS_EVIDENCE_STEP_ID
            and tool_name in _RPS_EVIDENCE_TOOL_NAMES
        )

    def _resolve_mcp_workspace_file(
        self,
        arguments: dict[str, Any],
        *,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if (
            not isinstance(schema, dict)
            or schema.get(_MCP_WORKSPACE_FILE_TRANSFER_SCHEMA_KEY) is not True
        ):
            return arguments
        raw_path = arguments.get("file_path")
        if raw_path is None:
            return arguments
        file_path = str(raw_path).strip()
        prefix = f"{SANDBOX_WORKSPACE}/"
        if not file_path.startswith(prefix):
            raise HarnessExecutionError(
                "MCP_FILE_PATH_OUTSIDE_WORKSPACE",
                "MCP 文件参数必须使用当前 TaskFrame 的 /workspace 沙箱路径。",
                retryable=True,
            )
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not {
            "file_path",
            "filename",
            "content_base64",
        }.issubset(properties):
            raise HarnessExecutionError(
                "MCP_WORKSPACE_TRANSFER_UNSUPPORTED",
                "该 MCP 工具未声明安全的 workspace 文件传递协议。",
            )
        if (
            arguments.get("content_text") is not None
            or arguments.get("content_base64") is not None
        ):
            raise HarnessExecutionError(
                "MCP_FILE_TRANSFER_CONFLICT",
                "file_path 不能与内联文件内容同时提供。",
                retryable=True,
            )
        relative_path = file_path[len(prefix) :]
        try:
            opened = open_harness_artifact(self.workspace_root, relative_path)
        except HarnessArtifactAccessError as exc:
            raise HarnessExecutionError(
                "MCP_WORKSPACE_FILE_UNAVAILABLE",
                "MCP 要读取的 workspace 文件不存在或不可安全读取。",
                retryable=True,
                details={"sandbox_path": file_path},
            ) from exc
        try:
            if opened.size > self._file_context.limits.max_file_bytes:
                raise HarnessExecutionError(
                    "MCP_WORKSPACE_FILE_TOO_LARGE",
                    "MCP 要读取的 workspace 文件超过当前 Harness 单文件上限。",
                    details={
                        "sandbox_path": file_path,
                        "actual_bytes": opened.size,
                        "max_bytes": self._file_context.limits.max_file_bytes,
                    },
                )
            content = b"".join(opened.iter_bytes())
        finally:
            opened.close()
        resolved = dict(arguments)
        resolved.pop("file_path", None)
        resolved["filename"] = opened.filename
        resolved["content_base64"] = base64.b64encode(content).decode("ascii")
        return resolved

    def _resolve_xiaoming_rps_draft_files(
        self,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge small private RPS draft files at the trusted MCP boundary."""
        if not self._is_xiaoming_rps_build(tool):
            return arguments
        raw_paths = arguments.get("draft_sections_files")
        if raw_paths is None:
            return arguments
        if not isinstance(raw_paths, list) or not raw_paths:
            raise HarnessExecutionError(
                "INVALID_RPS_DRAFT_FILES",
                "draft_sections_files 必须包含至少一个当前任务的草案 JSON 文件。",
                retryable=True,
            )
        if len(raw_paths) > 21:
            raise HarnessExecutionError(
                "RPS_DRAFT_FILE_LIMIT_EXCEEDED",
                "RPS 草案最多接受 21 个分段文件，每个文件最多 2 个章节。",
                details={"actual_files": len(raw_paths), "max_files": 21},
            )
        inline_drafts = arguments.get("draft_sections")
        if inline_drafts not in (None, {}):
            raise HarnessExecutionError(
                "RPS_DRAFT_SOURCE_CONFLICT",
                "draft_sections_files 不能与内联 draft_sections 同时提供。",
                retryable=True,
            )

        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        allowed_codes = {
            str(code).strip()
            for code in schema.get("x-rps-draft-section-codes") or []
            if str(code).strip()
        }
        chapter_map = schema.get("x-rps-draft-chapter-map")
        if not allowed_codes or not isinstance(chapter_map, dict):
            raise HarnessExecutionError(
                "RPS_DRAFT_SCHEMA_INVALID",
                "RPS 草案工具未配置有效的产品章节白名单。",
            )
        normalized_chapter_map = {
            str(code).strip(): str(chapter).strip()
            for code, chapter in chapter_map.items()
            if str(code).strip() and str(chapter).strip()
        }
        if set(normalized_chapter_map) != allowed_codes:
            raise HarnessExecutionError(
                "RPS_DRAFT_SCHEMA_INVALID",
                "RPS 草案工具的章节白名单与章节映射不一致。",
            )

        drafts: dict[str, str] = {}
        total_draft_chars = 0
        seen_paths: set[str] = set()
        for raw_path in raw_paths:
            relative_path = self._rps_draft_relative_path(raw_path)
            if relative_path in seen_paths:
                raise HarnessExecutionError(
                    "RPS_DRAFT_FILE_DUPLICATE",
                    "draft_sections_files 不得重复引用同一草案文件。",
                    retryable=True,
                )
            seen_paths.add(relative_path)
            try:
                opened = open_harness_artifact(self.workspace_root, relative_path)
            except HarnessArtifactAccessError as exc:
                raise HarnessExecutionError(
                    "RPS_DRAFT_FILE_UNAVAILABLE",
                    "RPS 草案文件不存在或不可安全读取。",
                    retryable=True,
                    details={"sandbox_path": _sandbox_path(relative_path)},
                ) from exc
            try:
                if opened.size > 100 * 1024:
                    raise HarnessExecutionError(
                        "RPS_DRAFT_FILE_TOO_LARGE",
                        "单个 RPS 草案文件不得超过 100 KiB。",
                        details={
                            "sandbox_path": _sandbox_path(relative_path),
                            "actual_bytes": opened.size,
                            "max_bytes": 100 * 1024,
                        },
                    )
                raw = b"".join(opened.iter_bytes())
            finally:
                opened.close()
            try:
                part = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise HarnessExecutionError(
                    "INVALID_RPS_DRAFT_FILE",
                    "RPS 草案文件必须是 UTF-8 编码的 JSON 对象。",
                    retryable=True,
                    details={"sandbox_path": _sandbox_path(relative_path)},
                ) from exc
            if not isinstance(part, dict) or not part or len(part) > 2:
                raise HarnessExecutionError(
                    "INVALID_RPS_DRAFT_FILE",
                    "每个 RPS 草案文件必须包含 1 至 2 个章节正文。",
                    retryable=True,
                    details={"sandbox_path": _sandbox_path(relative_path)},
                )
            for raw_code, raw_body in part.items():
                code = str(raw_code).strip()
                if code not in allowed_codes:
                    raise HarnessExecutionError(
                        "RPS_DRAFT_CODE_NOT_ALLOWED",
                        "RPS 草案文件包含不在产品章节白名单中的 code。",
                        details={"code": code},
                    )
                if code in drafts:
                    raise HarnessExecutionError(
                        "RPS_DRAFT_CODE_DUPLICATE",
                        "同一 RPS 产品章节只能在一个草案文件中出现一次。",
                        details={"code": code},
                    )
                if not isinstance(raw_body, str) or not raw_body.strip():
                    raise HarnessExecutionError(
                        "INVALID_RPS_DRAFT_FILE",
                        "每个 RPS 章节正文必须是非空字符串。",
                        retryable=True,
                        details={"code": code},
                    )
                if len(raw_body) > 12_000:
                    raise HarnessExecutionError(
                        "RPS_DRAFT_SECTION_TOO_LARGE",
                        "单个 RPS 章节正文不得超过 12,000 个字符。",
                        details={"code": code, "actual_chars": len(raw_body)},
                    )
                total_draft_chars += len(raw_body)
                if total_draft_chars > 120_000:
                    raise HarnessExecutionError(
                        "RPS_DRAFT_TOTAL_TOO_LARGE",
                        "本次 RPS 构建提交的草案正文不得超过 120,000 个字符。",
                        details={"actual_chars": total_draft_chars, "max_chars": 120_000},
                    )
                drafts[code] = raw_body

        required_codes = _selected_rps_draft_codes(
            arguments.get("rps_scope"),
            normalized_chapter_map,
        )
        missing_codes = sorted(required_codes - set(drafts))
        excluded_codes = sorted(set(drafts) - required_codes)
        if excluded_codes:
            raise HarnessExecutionError(
                "RPS_DRAFT_SCOPE_MISMATCH",
                "RPS 草案文件包含当前 rps_scope 未选择的产品章节。",
                retryable=True,
                details={"excluded_codes": excluded_codes},
            )
        if missing_codes:
            raise HarnessExecutionError(
                "DRAFTS_INCOMPLETE",
                "RPS 产品章节尚未全部起草完成，不能构建文件夹。",
                retryable=True,
                details={"missing_codes": missing_codes},
            )
        resolved = dict(arguments)
        resolved.pop("draft_sections_files", None)
        resolved["draft_sections"] = drafts
        return resolved

    def _is_xiaoming_rps_build(self, tool: Tool) -> bool:
        return (
            self.agent_id == _XIAOMING_AGENT_ID
            and self.active_skill_id == _RPS_SKILL_ID
            and str(self.active_step_id or "") == "edit_and_build"
            and tool.id == _RPS_BUILD_TOOL_ID
            and tool.name == _RPS_BUILD_TOOL_NAME
        )

    def _rps_draft_relative_path(self, raw_path: object) -> str:
        file_path = str(raw_path or "").strip().replace("\\", "/")
        prefix = f"{SANDBOX_WORKSPACE}/"
        if not file_path.startswith(prefix):
            raise HarnessExecutionError(
                "RPS_DRAFT_PATH_OUTSIDE_WORKSPACE",
                "RPS 草案文件必须使用当前 TaskFrame 的 /workspace 路径。",
                retryable=True,
            )
        relative_path = file_path[len(prefix) :]
        expected_prefix = f"{_INTERNAL_RPS_DRAFT_DIRECTORY}/"
        if (
            not relative_path.startswith(expected_prefix)
            or "/" in relative_path[len(expected_prefix) :]
            or not relative_path.endswith(".json")
        ):
            raise HarnessExecutionError(
                "RPS_DRAFT_PATH_INVALID",
                "RPS 草案文件必须位于 /workspace/.harness/rps-drafts/ 且使用 .json 扩展名。",
                retryable=True,
            )
        return relative_path

    def _resolve_json_tool_result_references(
        self,
        value: Any,
        *,
        schema: dict[str, Any] | None = None,
        depth: int = 0,
    ) -> Any:
        if depth > 32:
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "工具参数中的 JSON 结果引用嵌套过深。",
            )
        if isinstance(value, list):
            item_schema = (
                schema.get("items")
                if isinstance(schema, dict) and isinstance(schema.get("items"), dict)
                else None
            )
            return [
                self._resolve_json_tool_result_references(
                    item,
                    schema=item_schema,
                    depth=depth + 1,
                )
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        if value.get("kind") == _SANDBOX_JSON_FILE_KIND:
            resolved = self._read_json_tool_result_reference(value)
            if isinstance(schema, dict) and schema.get("type") == "string":
                return json.dumps(
                    resolved,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            return resolved
        properties = (
            schema.get("properties")
            if isinstance(schema, dict) and isinstance(schema.get("properties"), dict)
            else {}
        )
        return {
            key: self._resolve_json_tool_result_references(
                item,
                schema=(properties.get(key) if isinstance(properties.get(key), dict) else None),
                depth=depth + 1,
            )
            for key, item in value.items()
        }

    def _read_json_tool_result_reference(self, reference: dict[str, Any]) -> Any:
        sandbox_path = str(reference.get("sandbox_path") or "").strip()
        prefix = f"{SANDBOX_WORKSPACE}/"
        if not sandbox_path.startswith(prefix):
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "JSON 结果引用必须使用当前 TaskFrame 的 /workspace 沙箱路径。",
            )
        relative_path = sandbox_path[len(prefix) :]
        expected_prefix = f"{_INTERNAL_TOOL_RESULT_DIRECTORY}/"
        if (
            not relative_path.startswith(expected_prefix)
            or "/" in relative_path[len(expected_prefix) :]
            or not relative_path.endswith(".json")
        ):
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "JSON 结果引用不属于 Harness 管理的工具结果目录。",
            )
        try:
            opened = open_harness_artifact(self.workspace_root, relative_path)
        except HarnessArtifactAccessError as exc:
            raise HarnessExecutionError(
                "TOOL_RESULT_REFERENCE_UNAVAILABLE",
                "引用的 JSON 工具结果文件不存在或不可安全读取。",
            ) from exc
        try:
            if opened.size > self._file_context.limits.max_file_bytes:
                raise HarnessExecutionError(
                    "TOOL_RESULT_REFERENCE_TOO_LARGE",
                    "引用的 JSON 工具结果超过当前 Harness 单文件上限。",
                    details={
                        "actual_bytes": opened.size,
                        "max_bytes": self._file_context.limits.max_file_bytes,
                    },
                )
            expected_sha256 = str(reference.get("sha256") or "").strip().lower()
            actual_sha256 = opened.sha256()
            if expected_sha256 and expected_sha256 != actual_sha256:
                raise HarnessExecutionError(
                    "TOOL_RESULT_REFERENCE_CHANGED",
                    "引用的 JSON 工具结果文件已发生变化。",
                    details={
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )
            raw = b"".join(opened.iter_bytes())
        finally:
            opened.close()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "引用的工具结果文件不是有效的 UTF-8 JSON。",
            ) from exc

    def _remaining_step_seconds(self) -> float | None:
        if self.step_deadline_monotonic is None:
            return None
        return max(self.step_deadline_monotonic - time.monotonic(), 0.1)



def _project_rps_evidence_payload(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if isinstance(data.get("results"), list) or isinstance(data.get("chunks"), list):
        return _project_rps_evidence_with_limit(data)
    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("chunks"), list):
        projected = dict(data)
        projected["data"] = _project_rps_evidence_with_limit(nested)
        return projected
    return data


def _project_rps_evidence_with_limit(data: dict[str, Any]) -> dict[str, Any]:
    for text_limit in (
        _RPS_EVIDENCE_TEXT_MAX_CHARS,
        4_000,
        2_000,
        1_000,
        500,
    ):
        projected = _project_rps_evidence_once(data, text_limit=text_limit)
        try:
            size = len(
                json.dumps(
                    projected,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError):
            return data
        if size <= _RPS_EVIDENCE_MAX_CHARS:
            return projected
    return data


def _project_rps_evidence_once(
    data: dict[str, Any],
    *,
    text_limit: int,
) -> dict[str, Any]:
    projected = dict(data)
    for collection_name, body_key, prefix in (
        ("results", "text", "text"),
        ("chunks", "content", "content"),
    ):
        collection = data.get(collection_name)
        if not isinstance(collection, list):
            continue
        projected[collection_name] = [
            _project_rps_evidence_item(
                item,
                body_key=body_key,
                prefix=prefix,
                text_limit=text_limit,
            )
            for item in collection
        ]
    return projected


def _project_rps_evidence_item(
    item: Any,
    *,
    body_key: str,
    prefix: str,
    text_limit: int,
) -> Any:
    if not isinstance(item, dict):
        return item
    body = item.get(body_key)
    if not isinstance(body, str) or len(body) <= text_limit:
        return item
    marker = "\n[…证据正文已由可信边界裁剪…]\n"
    available_chars = max(text_limit - len(marker), 2)
    head_chars = max(1, (available_chars * 3) // 4)
    tail_chars = max(1, available_chars - head_chars)
    clipped = body[:head_chars] + marker + body[-tail_chars:]
    projected = dict(item)
    projected[body_key] = clipped
    projected[f"{prefix}_truncated"] = True
    projected[f"{prefix}_original_chars"] = len(body)
    projected[f"{prefix}_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return projected


def _selected_rps_draft_codes(
    rps_scope: object,
    chapter_map: dict[str, str],
) -> set[str]:
    scope = rps_scope if isinstance(rps_scope, dict) else {}
    raw_requested = scope.get("chapters") or scope.get("include") or []
    if not raw_requested:
        return set(chapter_map)
    if not isinstance(raw_requested, list):
        raise HarnessExecutionError(
            "RPS_SCOPE_INVALID",
            "rps_scope.chapters 或 rps_scope.include 必须是章节名称数组。",
            retryable=True,
        )
    wanted = {str(item).strip() for item in raw_requested if str(item).strip()}
    selected = {
        code
        for code, chapter in chapter_map.items()
        if chapter in wanted or chapter.split()[0] in wanted
    }
    if not selected:
        raise HarnessExecutionError(
            "RPS_SCOPE_INVALID",
            "rps_scope 未匹配任何 RPS 产品章节。",
            retryable=True,
            details={"requested_chapters": sorted(wanted)},
        )
    return selected


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


def _failure(code: str, message: str, **details: Any) -> dict[str, Any]:
    error = {
        "code": code,
        "message": message,
        "retryable": False,
    }
    error.update(details)
    return {
        "success": False,
        "error": error,
    }


def _safe_artifact_label(
    value: Any,
    *,
    fallback: str,
    max_length: int,
) -> str:
    cleaned = "".join(
        character
        for character in str(value or fallback).strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:max_length] or fallback[:max_length]


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
        "CAPABILITY_NOT_ACTIVATED",
        "CAPABILITY_NOT_AVAILABLE",
        "INVALID_ARGUMENTS",
        "MCP_FILE_PATH_OUTSIDE_WORKSPACE",
        "MCP_FILE_TRANSFER_CONFLICT",
        "MCP_WORKSPACE_FILE_TOO_LARGE",
        "MCP_WORKSPACE_FILE_UNAVAILABLE",
        "MCP_WORKSPACE_TRANSFER_UNSUPPORTED",
    }


def _tool_side_effect(tool: Tool) -> str:
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    declared = str(schema.get(_TOOL_SIDE_EFFECT_SCHEMA_KEY) or "").strip().lower()
    if declared in {"read", "write", "delete"}:
        return declared
    method = str(tool.method or "").upper()
    if method == "GET":
        return "read"
    if method == "DELETE":
        return "delete"
    return "write"


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


def _sandbox_path(relative_path: str) -> str:
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if normalized == SANDBOX_WORKSPACE or normalized.startswith(
        f"{SANDBOX_WORKSPACE}/"
    ):
        return normalized
    if normalized in {"", "."}:
        return SANDBOX_WORKSPACE
    return f"{SANDBOX_WORKSPACE}/{normalized.lstrip('/')}"


def _model_visible_file_result(value: Any, *, key: str = "") -> Any:
    path_keys = {"path", "source_path", "destination_path", "cwd"}
    if isinstance(value, dict):
        return {
            item_key: _model_visible_file_result(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_model_visible_file_result(item, key=key) for item in value]
    if isinstance(value, str) and key in path_keys:
        return _sandbox_path(value)
    return value


def _is_user_facing_workspace_file(path: str) -> bool:
    parts = Path(path).parts
    if not parts:
        return False
    first = parts[0]
    if (
        first in {"attachments", ".harness"}
        or first.startswith("general_skill_")
    ):
        return False
    if any(
        part in {
            ".git",
            ".harness-trash",
            ".pytest_cache",
            "__pycache__",
            "node_modules",
        }
        or part.startswith(".tmp-")
        for part in parts
    ):
        return False
    return Path(path).suffix.lower() not in {".pyc", ".pyo", ".part", ".tmp"}
