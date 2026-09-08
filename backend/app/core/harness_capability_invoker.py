from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.capabilities.local_general_skill import (
    package_from_row,
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
from app.core.published_deliverables import (
    MAX_PUBLISHED_DELIVERABLES,
    find_published_deliverable,
    list_published_deliverables,
)
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
from app.harness import (
    HarnessArtifactAccessError,
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
    open_harness_artifact,
    publish_changed_harness_artifacts,
    register_command_tools,
    register_skill_script_tools,
    snapshot_harness_workspace,
)
from app.harness.errors import HarnessExecutionError
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.harness.sandbox import parse_network_policy
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


_INLINE_JSON_TOOL_RESULT_MAX_CHARS = 2_000
_INTERNAL_TOOL_RESULT_DIRECTORY = ".harness/tool-results"
_GENERAL_SKILL_PACKAGE_DIRECTORY = ".harness/skill-packages"
_SANDBOX_JSON_FILE_KIND = "sandbox_json_file"


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
            tenant_id, session.id, task_frame_id, db=self.db
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._workspace_snapshot = snapshot_harness_workspace(self.workspace_root)
        ui_config = self.db.get(UIConfig, tenant_id)
        sandbox_enabled = bool(getattr(ui_config, "sandbox_enabled", False))
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
        register_skill_script_tools(self._file_registry)
        self._file_executor = HarnessExecutor(self._file_registry)
        self._file_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=tenant_id,
            workspace_root=self.workspace_root,
            sandbox_enabled=sandbox_enabled,
            sandbox_network_mode=sandbox_mode,
            sandbox_allowed_domains=sandbox_domains,
        )
        self._sandbox_network_mode = sandbox_mode
        self._sandbox_allowed_domains = sandbox_domains
        self._sandbox_enabled = sandbox_enabled
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
                    call_id=call_id,
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
        if descriptor.kind == "internal" and descriptor.name == "lark_cli":
            from app.lark_cli.policy import logical_write_signature

            signature = logical_write_signature(arguments)
            if signature is None:
                return None
            canonical = json.dumps(
                {
                    "tenant_id": self.tenant_id,
                    "task_frame_id": self.task_frame_id,
                    "step_id": self.active_step_id,
                    "tool_id": "builtin.lark_cli",
                    "signature": signature,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
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
            if name in {"exec_command", "run_skill_script"} and data.get("ok") is not True:
                visible_data = _model_visible_file_result(data)
                timed_out = bool(data.get("timed_out"))
                return {
                    "success": False,
                    "data": visible_data,
                    "error": {
                        "code": (
                            "COMMAND_TIMEOUT"
                            if timed_out
                            else "COMMAND_EXIT_NONZERO"
                        ),
                        "message": (
                            "受控进程执行超时。"
                            if timed_out
                            else "受控进程执行完成，但返回了非零退出码。"
                        ),
                        "retryable": False,
                        "details": visible_data,
                    },
                    "duration_ms": result.duration_ms,
                }
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
        if name == "list_published_deliverables":
            return self._list_published_deliverables(arguments)
        if name == "read_published_deliverable":
            return self._read_published_deliverable(arguments)
        if name == "lark_cli":
            from app.lark_cli.service import invoke_lark_cli

            return invoke_lark_cli(
                self.db,
                tenant_id=self.tenant_id,
                session=self.session,
                task_frame_id=self.task_frame_id,
                agent_id=self.agent_id,
                arguments=arguments,
                active_skill=self.active_skill,
                active_step_id=self.active_step_id,
            )
        return _failure(
            "UNSUPPORTED_INTERNAL_CAPABILITY",
            "不支持的 Harness 内部能力。",
        )

    def _list_published_deliverables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_limit = arguments.get("limit", MAX_PUBLISHED_DELIVERABLES)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return _failure("INVALID_ARGUMENTS", "limit 必须是整数。")
        rows = list_published_deliverables(
            self.db,
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            query=str(arguments.get("query") or ""),
            limit=raw_limit,
            exclude_task_frame_id=self.task_frame_id,
        )
        return {
            "success": True,
            "data": {
                "deliverables": rows,
                "count": len(rows),
                "notice": (
                    "使用 read_published_deliverable 读取所需文件，"
                    "不要用 read_file 猜测旧工作区路径。"
                ),
            },
        }

    def _read_published_deliverable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_frame_id = str(arguments.get("task_frame_id") or "").strip()
        path = str(arguments.get("path") or "").strip()
        if not task_frame_id or not path:
            return _failure("INVALID_ARGUMENTS", "task_frame_id 和 path 不能为空。")
        artifact = find_published_deliverable(
            self.db,
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            task_frame_id=task_frame_id,
            path=path,
        )
        if artifact is None:
            return _failure(
                "PUBLISHED_DELIVERABLE_NOT_FOUND",
                "当前会话中没有该历史交付物。",
            )
        workspace_root = _workspace_root(
            self.tenant_id,
            self.session.id,
            task_frame_id,
            db=self.db,
        )
        opened = None
        try:
            opened = open_harness_artifact(workspace_root, path)
            actual_sha256 = opened.sha256()
            expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
            expected_size = artifact.get("size")
            if (
                (expected_sha256 and expected_sha256 != actual_sha256.lower())
                or (isinstance(expected_size, int) and expected_size != opened.size)
            ):
                return _failure(
                    "PUBLISHED_DELIVERABLE_CHANGED",
                    "历史交付物在发布后已发生变化，拒绝读取。",
                )
        except (HarnessArtifactAccessError, OSError):
            return _failure(
                "PUBLISHED_DELIVERABLE_NOT_FOUND",
                "历史交付物不存在或无法安全读取。",
            )
        finally:
            if opened is not None:
                opened.close()

        read_arguments = {
            key: arguments[key]
            for key in ("path", "offset", "max_bytes", "continuation_token")
            if key in arguments
        }
        read_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=self.tenant_id,
            workspace_root=workspace_root,
            limits=self._file_context.limits,
            sandbox_enabled=self._file_context.sandbox_enabled,
            sandbox_network_mode=self._file_context.sandbox_network_mode,
            sandbox_allowed_domains=self._file_context.sandbox_allowed_domains,
        )
        result = self._file_executor.execute(
            read_context,
            HarnessToolCall(
                call_id=new_id("hcall"),
                name="read_file",
                arguments=read_arguments,
            ),
        )
        if not result.success:
            return {
                "success": False,
                "error": {
                    "code": (
                        result.error.code
                        if result.error
                        else "PUBLISHED_DELIVERABLE_READ_FAILED"
                    ),
                    "message": (
                        result.error.message if result.error else "历史交付物读取失败。"
                    ),
                    "retryable": bool(result.error.retryable) if result.error else False,
                    "details": dict(result.error.details) if result.error else {},
                },
            }
        data = dict(result.data or {})
        data.update(
            {
                "task_frame_id": task_frame_id,
                "display_name": artifact.get("display_name"),
                "published_at": artifact.get("published_at"),
            }
        )
        return {"success": True, "data": data}

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
        # GeneralSkill is an instruction package.  Loading it enriches the same
        # isolated AgentLoop transcript; execution remains the responsibility of
        # the regular Harness tools.  Accept legacy ``execute`` calls as a safe
        # alias for ``read`` so persisted model/tool calls do not suddenly fail,
        # but never start the old generated-runner pipeline from business runs.
        requested_operation = str(arguments.get("operation") or "read").strip().lower()
        if requested_operation not in {"read", "execute"}:
            return _failure(
                "INVALID_ARGUMENTS",
                "通用技能 operation 只能是 read。",
            )
        result = self._read_general_skill_package(skill, metadata, query)
        if requested_operation == "execute":
            result["data"]["requested_operation"] = "execute"
            result["data"]["compatibility_notice"] = (
                "execute 已弃用并安全降级为 read；请按 SKILL.md 指导调用现有 Harness 工具。"
            )
        self._emit_trace(
            "general_skill_trace",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "operation": "read",
                "requested_operation": requested_operation,
                "phase": "instructions_loaded",
                "message": "已加载技能说明，AgentLoop 将按说明选择 Harness 工具",
            },
        )
        return result

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
        package = package_from_row(skill)
        package_root, file_paths = _materialize_general_skill_package(
            self.workspace_root,
            package,
        )
        entrypoint_path = next(
            (
                item
                for item in file_paths
                if item.removeprefix(package_root + "/") == package.entrypoint
            ),
            f"{package_root}/{package.entrypoint}",
        )
        return {
            "success": True,
            "data": {
                "kind": "general_skill",
                "slug": metadata.get("slug"),
                "operation": "read",
                "query": query,
                "package": _skill_package_preview(skill),
                "package_root": package_root,
                "sandbox_package_root": _sandbox_path(package_root),
                "entrypoint_path": entrypoint_path,
                "sandbox_entrypoint_path": _sandbox_path(entrypoint_path),
                "file_paths": file_paths,
                "notice": (
                    "技能包说明已加载到当前隔离 Harness transcript；"
                    "包内文件已物化到 package_root，请只使用返回的 entrypoint_path 和"
                    " file_paths 定位文件；"
                    "请由 AgentLoop 直接应用其中的 prompt、规则和示例，并按任务需要调用"
                    "知识库、原装 Tool、exec_command 或 typed 文件工具；Skill 本身不会"
                    "生成临时代码或启动第二套 runner。"
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
        self,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
        *,
        call_id: str,
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
        result = {
            "success": True,
            "data": payload,
            "citations": knowledge_citations_from_results([payload]),
        }
        return self._persist_large_json_result(result, call_id=call_id)

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
            session_id=self.session.id,
            invocation_id=call_id,
            timeout_seconds_override=self._remaining_step_seconds(),
        )
        payload = result.model_dump(mode="json")
        # MCP Apps payloads belong to the host UI, not to the isolated model
        # transcript. Emit a dedicated trace event so the frontend receives
        # the complete descriptor while the model only receives tool data.
        app_descriptor = payload.pop("mcp_app", None)
        payload.pop("mcp_metadata", None)
        if payload.get("success") is not True:
            return payload
        a2a_artifacts = self._materialize_a2a_artifacts(payload, call_id=call_id)
        if a2a_artifacts:
            payload["artifacts"] = [*(payload.get("artifacts") or []), *a2a_artifacts]
        payload = self._persist_large_json_result(payload, call_id=call_id)
        if isinstance(app_descriptor, dict) and payload.get("success") is True:
            app_descriptor["initial_result"] = payload.get("data")
            self._emit_trace(
                "harness_mcp_app_view",
                {
                    "tool_name": name,
                    "mcp_app": app_descriptor,
                },
            )
        return payload

    def _materialize_a2a_artifacts(
        self,
        payload: dict[str, Any],
        *,
        call_id: str,
    ) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
            return []
        published: list[dict[str, Any]] = []
        directory = self.workspace_root / "artifacts" / "a2a" / _safe_artifact_name(call_id)
        for artifact_index, artifact in enumerate(data["artifacts"], start=1):
            if not isinstance(artifact, dict):
                continue
            for part_index, part in enumerate(artifact.get("parts") or [], start=1):
                if not isinstance(part, dict) or not isinstance(part.get("file"), dict):
                    continue
                file_part = part["file"]
                encoded = file_part.get("bytes")
                if not isinstance(encoded, str) or not encoded:
                    continue
                try:
                    content = base64.b64decode(encoded, validate=True)
                except ValueError:
                    continue
                requested_name = str(file_part.get("name") or "").strip()
                filename = _safe_artifact_name(
                    requested_name or f"artifact-{artifact_index}-{part_index}.bin"
                )
                directory.mkdir(parents=True, exist_ok=True)
                output = directory / filename
                suffix = 2
                while output.exists():
                    output = directory / f"{Path(filename).stem}-{suffix}{Path(filename).suffix}"
                    suffix += 1
                output.write_bytes(content)
                relative = output.relative_to(self.workspace_root).as_posix()
                sha256 = hashlib.sha256(content).hexdigest()
                content_type = str(file_part.get("mimeType") or "").strip() or (
                    mimetypes.guess_type(filename)[0] or "application/octet-stream"
                )
                file_part.pop("bytes", None)
                file_part["path"] = relative
                file_part["sandbox_path"] = _sandbox_path(relative)
                file_part["sha256"] = sha256
                published.append(
                    {
                        "type": "workspace_file",
                        "task_frame_id": self.task_frame_id,
                        "path": relative,
                        "sandbox_path": _sandbox_path(relative),
                        "sha256": sha256,
                        "size": len(content),
                        "display_name": requested_name or filename,
                        "description": str(artifact.get("description") or "A2A Artifact"),
                        "content_type": content_type,
                        "operation": "a2a_artifact",
                        "source": "a2a",
                    }
                )
        return published

    def _persist_large_json_result(
        self,
        payload: dict[str, Any],
        *,
        call_id: str,
    ) -> dict[str, Any]:
        """Keep large knowledge/tool JSON out of the isolated model transcript."""

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
                "能力已返回结果，但完整 JSON 无法写入当前 TaskFrame 沙箱。",
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
        reference = {
            "kind": _SANDBOX_JSON_FILE_KIND,
            "sandbox_path": _sandbox_path(relative_path),
            "size": stored_data.get("size"),
            "sha256": stored_data.get("sha256"),
        }
        payload["data"] = reference
        app_descriptor = payload.get("mcp_app")
        if isinstance(app_descriptor, dict):
            app_descriptor["initial_result"] = dict(reference)
        return payload

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


def _workspace_root(
    tenant_id: str, session_id: str, task_frame_id: str, *, db: Session | None = None
) -> Path:
    return harness_task_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )


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


def _materialize_general_skill_package(
    workspace_root: Path,
    package: Any,
) -> tuple[str, list[str]]:
    """Write an immutable GeneralSkill snapshot into the current TaskFrame workspace."""

    safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(package.slug or "skill")).strip(
        ".-"
    ) or "skill"
    digest_suffix = re.sub(r"[^a-fA-F0-9]", "", str(package.digest or ""))[:12]
    directory_name = safe_slug + (f"-{digest_suffix}" if digest_suffix else "")
    relative_root = f"{_GENERAL_SKILL_PACKAGE_DIRECTORY}/{directory_name}"
    target_root = workspace_root / relative_root
    workspace_resolved = workspace_root.resolve()
    target_resolved = target_root.resolve(strict=False)
    if not target_resolved.is_relative_to(workspace_resolved):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package path escapes the TaskFrame workspace.",
        )
    if target_root.exists() and (target_root.is_symlink() or not target_root.is_dir()):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package destination is not a safe directory.",
        )
    target_root.mkdir(parents=True, exist_ok=True)
    file_paths: list[str] = []
    for item in package.files:
        relative_path = _safe_general_skill_file_path(str(item.path or ""))
        target = target_root / relative_path
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise HarnessExecutionError(
                "INVALID_SKILL_PACKAGE",
                "GeneralSkill package contains an unsafe existing file path.",
                details={"path": relative_path},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(workspace_resolved):
            raise HarnessExecutionError(
                "INVALID_SKILL_PACKAGE",
                "GeneralSkill package file escapes the TaskFrame workspace.",
                details={"path": relative_path},
            )
        target.write_text(str(item.content or ""), encoding="utf-8")
        file_paths.append(f"{relative_root}/{relative_path}")
    return relative_root, file_paths


def _safe_general_skill_file_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if (
        not parts
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part == ".." for part in parts)
        or any("\x00" in part for part in parts)
    ):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package contains an unsafe file path.",
            details={"path": path},
        )
    return "/".join(parts)


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
        elif isinstance(value, list):
            audited[str(key)] = _redact_secret_flag_values(value)
        else:
            audited[str(key)] = value
    return audited


def _redact_secret_flag_values(items: list[Any]) -> list[Any]:
    """argv 风格列表里跟在敏感 flag（如 --app-secret）后的值不落审计记录。"""

    redacted = list(items)
    for index, token in enumerate(redacted[:-1]):
        if not isinstance(token, str) or not token.startswith("-"):
            continue
        lowered = token.lower()
        if any(part in lowered for part in ("secret", "token", "password")):
            redacted[index + 1] = "<redacted>"
    return redacted


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


def _safe_artifact_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in Path(str(value or "artifact").replace("\\", "/")).name
    ).strip(".-")
    return cleaned[:180] or "artifact"


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
