"""构建 Harness 每轮冻结能力清单，并把实时授权投影为可调用描述符。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import (
    get_agent,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    visible_knowledge_base_versions,
    visible_tool_rows,
)
from app.capabilities.local_general_skill import package_from_row
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    current_step_capability_refs,
)
from app.db.models import (
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    MCPServer,
    Skill,
    Tool,
    UIConfig,
)
from app.harness import (
    build_file_tool_registry,
    register_command_tools,
    register_skill_script_tools,
)
from app.harness.sandbox import available_backend
from app.knowledge.access import KnowledgeAccessService
from app.knowledge.errors import KnowledgeError

RESERVED_HARNESS_CAPABILITY_NAMES = {
    "capability_search",
    "capability_describe",
    "list_published_deliverables",
    "read_published_deliverable",
    "exec_command",
    "run_skill_script",
    "knowledge_search",
    "knowledge_list_versions",
    "knowledge_create_draft",
    "knowledge_update_draft",
    "knowledge_publish_draft",
    "knowledge_reject_draft",
    "knowledge_rollback",
}


@dataclass(frozen=True)
class _ManifestKnowledgeAccess:
    """能力清单中一项已授权知识的冻结版本与实时权限。"""

    version: KnowledgeBaseVersion
    permission: str
    is_default_write: bool
    mode: str = "dedicated"


class CapabilityAuthorizationError(RuntimeError):
    pass


class CapabilityManifestBuilder:
    def __init__(self, db: Session):
        self.db = db

    def build(
        self,
        tenant_id: str,
        agent_id: str | None,
        skill: Skill | None,
        step_id: str | None,
        *,
        team_id: str | None = None,
        frozen_knowledge_versions: dict[str, str] | None = None,
    ) -> CapabilityManifest:
        """构建当前上下文能力清单，并可固定本轮已经选择的知识版本。"""
        # 先验证员工身份并解析当前 SOP 的显式能力引用，建立 fail-closed 起点。
        if agent_id and get_agent(self.db, tenant_id, agent_id) is None:
            raise CapabilityAuthorizationError("当前员工不存在、已归档或不属于该租户。")
        refs = current_step_capability_refs(skill, step_id)
        available: list[CapabilityDescriptor] = []
        unavailable: list[CapabilityDescriptor] = []

        # 再加入 Harness 内核与本地文件能力，它们不依赖租户资源可见性。
        available.extend(_internal_capability_descriptors())
        ui_config = self.db.get(UIConfig, tenant_id)
        sandbox_enabled = bool(getattr(ui_config, "sandbox_enabled", False))

        builtin_registry = build_file_tool_registry()
        register_command_tools(builtin_registry)
        register_skill_script_tools(builtin_registry)
        for spec in builtin_registry.specs():
            is_command = spec.name in {"exec_command", "run_skill_script"}
            available.append(
                CapabilityDescriptor(
                    capability_id=(
                        f"builtin.command.{spec.name}" if is_command else f"builtin.fs.{spec.name}"
                    ),
                    name=spec.name,
                    kind="file",
                    description=spec.description,
                    input_schema=dict(spec.input_schema),
                    metadata={
                        "provider": ("builtin.command" if is_command else "builtin.fs"),
                        "side_effect": spec.side_effect,
                        **(
                            {
                                "sandbox": (
                                    available_backend() or "unavailable"
                                    if sandbox_enabled
                                    else "disabled_by_admin"
                                )
                            }
                            if is_command
                            else {}
                        ),
                    },
                )
            )

        # 然后按员工可见性和 SOP 范围投影通用技能。
        visible_general = _visible_general_skills(self.db, tenant_id, agent_id)
        general_by_ref = {ref: row for row in visible_general for ref in (row.id, row.slug)}
        for row in visible_general:
            scope = _scope(row)
            if scope is None:
                unavailable.append(
                    _unavailable(
                        row.id,
                        f"general_skill.{row.slug}",
                        "general_skill",
                        "general",
                        "能力范围配置无效，已按 fail-closed 禁用。",
                    )
                )
                continue
            explicitly_allowed = any(
                general_by_ref.get(ref) is row for ref in refs["general_skill_ids"]
            )
            if scope == "sop_specific" and not explicitly_allowed:
                continue
            available.append(
                CapabilityDescriptor(
                    capability_id=row.id,
                    name=f"general_skill.{row.slug}",
                    kind="general_skill",
                    capability_scope=scope,
                    description=row.description or row.name,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "当前 TaskFrame 中要交给该技能处理的具体需求。",
                            },
                            "operation": {
                                "type": "string",
                                "enum": ["read"],
                                "description": (
                                    "使用 read 将经过快照校验的 SKILL.md 和包内文件说明加载到 "
                                    "当前 AgentLoop；技能只提供执行指导，不会生成或运行临时代码。"
                                ),
                            },
                        },
                        "required": ["query", "operation"],
                    },
                    metadata={
                        "slug": row.slug,
                        "display_name": row.name,
                        "content_digest": general_skill_snapshot_digest(row),
                        "package_digest": package_from_row(row).digest,
                        "execution_policy": "instructions_only",
                        "script_execution": "use_harness_tools",
                        "permissions": dict(row.permissions_json or {}),
                        "runtime_config": dict(row.runtime_config_json or {}),
                        "sop_explicitly_allowed": explicitly_allowed,
                    },
                )
            )

        # 外部工具沿用同一范围门禁，并保留执行时需要的不可见内部元数据。
        visible_tools = visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)
        tool_by_ref = {ref: row for row in visible_tools for ref in (row.id, row.name)}
        for row in visible_tools:
            app_config = (row.config_json or {}).get("mcp_apps")
            if isinstance(app_config, dict):
                visibility = app_config.get("visibility")
                if isinstance(visibility, list) and "model" not in visibility:
                    # App-only controls stay callable from their isolated view but are not
                    # advertised to the conversation model.
                    continue
            scope = _scope(row)
            if scope is None:
                unavailable.append(
                    _unavailable(
                        row.id,
                        row.name,
                        "tool",
                        "general",
                        "能力范围配置无效，已按 fail-closed 禁用。",
                    )
                )
                continue
            explicitly_allowed = any(tool_by_ref.get(ref) is row for ref in refs["tool_ids"])
            if scope == "sop_specific" and not explicitly_allowed:
                continue
            if row.allowed_skills_json and (
                skill is None or skill.skill_id not in row.allowed_skills_json
            ):
                if explicitly_allowed:
                    unavailable.append(
                        _unavailable(
                            row.id,
                            row.name,
                            "tool",
                            scope,
                            "当前工具的 allowed_skills 未授权该 SOP。",
                        )
                    )
                continue
            invocation_name = _available_invocation_name(row.name, row.id, available)
            available.append(
                CapabilityDescriptor(
                    capability_id=row.id,
                    name=invocation_name,
                    kind="tool",
                    capability_scope=scope,
                    description=row.description or row.display_name or row.name,
                    input_schema=dict(row.input_schema or {}),
                    metadata={
                        "tool_type": row.tool_type,
                        "method": row.method,
                        "source_tool_name": row.name,
                        "display_name": row.display_name or row.name,
                        "content_digest": tool_snapshot_digest(self.db, row),
                        "sop_explicitly_allowed": explicitly_allowed,
                    },
                )
            )

        # 知识能力最后解析，以便同时冻结本轮版本并按团队实时授权生成维护动作。
        visible_knowledge = _visible_knowledge_versions_for_manifest(
            self.db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            team_id=team_id,
            frozen_knowledge_versions=frozen_knowledge_versions,
        )
        allowed_knowledge_ids: list[str] = []
        allowed_knowledge_version_ids: list[str] = []
        knowledge_version_by_base_id: dict[str, str] = {}
        knowledge_scope_by_base_id: dict[str, str] = {}
        knowledge_scopes: list[str] = []
        valid_knowledge_ids: set[str] = set()
        for kb_id, knowledge_access in visible_knowledge.items():
            version = knowledge_access.version
            scope = _scope(version)
            if scope == "general":
                root = self.db.get(KnowledgeBase, kb_id)
                scope = _scope(root) if root is not None else scope
            if scope is None:
                if kb_id in refs["knowledge_base_ids"]:
                    unavailable.append(
                        _unavailable(
                            kb_id,
                            kb_id,
                            "knowledge",
                            "general",
                            "能力范围配置无效，已按 fail-closed 禁用。",
                        )
                    )
                continue
            if scope == "sop_specific" and kb_id not in refs["knowledge_base_ids"]:
                continue
            valid_knowledge_ids.add(kb_id)
            allowed_knowledge_ids.append(kb_id)
            allowed_knowledge_version_ids.append(version.id)
            knowledge_version_by_base_id[kb_id] = version.id
            knowledge_scope_by_base_id[kb_id] = scope
            knowledge_scopes.append(scope)
        if allowed_knowledge_ids:
            available.append(
                CapabilityDescriptor(
                    capability_id="knowledge.search",
                    name="knowledge_search",
                    kind="knowledge",
                    capability_scope=(
                        "sop_specific"
                        if all(scope == "sop_specific" for scope in knowledge_scopes)
                        else "general"
                    ),
                    description="检索当前 TaskFrame 已授权的企业知识库。",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "knowledge_base_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "max_chunks": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 12,
                            },
                        },
                        "required": ["query"],
                    },
                    metadata={
                        "allowed_knowledge_base_ids": allowed_knowledge_ids,
                        "allowed_knowledge_base_version_ids": (allowed_knowledge_version_ids),
                        "knowledge_version_by_base_id": knowledge_version_by_base_id,
                        "knowledge_scope_by_base_id": knowledge_scope_by_base_id,
                    },
                )
            )
        if team_id is not None and agent_id is not None:
            available.extend(
                _shared_knowledge_action_descriptors(
                    visible_knowledge=visible_knowledge,
                    allowed_knowledge_ids=allowed_knowledge_ids,
                    knowledge_version_by_base_id=knowledge_version_by_base_id,
                    knowledge_scope_by_base_id=knowledge_scope_by_base_id,
                )
            )

        # 最后记录显式引用但不可用的资源，并对完整冻结清单计算稳定修订号。
        unavailable.extend(
            self._unavailable_explicit_refs(
                tenant_id,
                refs,
                general_by_ref,
                tool_by_ref,
                valid_knowledge_ids,
            )
        )
        snapshot_revision = _snapshot_revision(available, unavailable)
        return CapabilityManifest(
            available=available,
            unavailable_references=unavailable,
            snapshot_revision=snapshot_revision,
        )

    def _unavailable_explicit_refs(
        self,
        tenant_id: str,
        refs: dict[str, list[str]],
        general_by_ref: dict[str, GeneralSkill],
        tool_by_ref: dict[str, Tool],
        knowledge_ids: set[str],
    ) -> list[CapabilityDescriptor]:
        unavailable: list[CapabilityDescriptor] = []
        for ref in refs["general_skill_ids"]:
            if ref not in general_by_ref:
                unavailable.append(
                    _unavailable(
                        ref,
                        ref,
                        "general_skill",
                        "sop_specific",
                        _explicit_reason(self.db.get(GeneralSkill, ref), tenant_id),
                    )
                )
        for ref in refs["tool_ids"]:
            if ref not in tool_by_ref:
                unavailable.append(
                    _unavailable(
                        ref,
                        ref,
                        "tool",
                        "sop_specific",
                        _explicit_reason(self.db.get(Tool, ref), tenant_id),
                    )
                )
        for ref in refs["knowledge_base_ids"]:
            if ref not in knowledge_ids:
                unavailable.append(
                    _unavailable(
                        ref,
                        ref,
                        "knowledge",
                        "sop_specific",
                        _explicit_reason(self.db.get(KnowledgeBase, ref), tenant_id),
                    )
                )
        return unavailable


def _visible_knowledge_versions_for_manifest(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str | None,
    team_id: str | None,
    frozen_knowledge_versions: dict[str, str] | None,
) -> dict[str, _ManifestKnowledgeAccess]:
    """按可信上下文返回知识冻结版本，并保留动作过滤所需的实时权限。"""
    # 无员工身份只支持原有租户级可见路径；团队上下文必须有员工身份。
    if agent_id is None:
        if team_id is not None:
            raise CapabilityAuthorizationError("团队知识上下文缺少员工身份。")
        visible = visible_knowledge_base_versions(
            db,
            tenant_id,
            agent_id,
            include_inactive=False,
        )
        selected = (
            visible
            if frozen_knowledge_versions is None
            else {
                knowledge_base_id: version
                for knowledge_base_id, version in visible.items()
                if frozen_knowledge_versions.get(knowledge_base_id) == version.id
            }
        )
        return {
            knowledge_base_id: _ManifestKnowledgeAccess(
                version=version,
                permission="reader",
                is_default_write=False,
            )
            for knowledge_base_id, version in selected.items()
        }

    # 有员工身份时统一走知识访问解析器，避免能力清单复制授权规则。
    try:
        projections = KnowledgeAccessService(db).resolve_projections(
            tenant_id=tenant_id,
            agent_id=agent_id,
            team_id=team_id,
        )
    except KnowledgeError as exc:
        raise CapabilityAuthorizationError(exc.message) from exc

    # 冻结映射只锁定版本，不锁定权限；权限仍来自本次实时解析。
    visible: dict[str, _ManifestKnowledgeAccess] = {}
    for projection in projections:
        if frozen_knowledge_versions is not None:
            version_id = frozen_knowledge_versions.get(projection.knowledge_base_id)
            if version_id is None:
                continue
        else:
            version_id = projection.knowledge_base_version_id
        version = db.get(KnowledgeBaseVersion, version_id)
        if (
            version is None
            or version.tenant_id != tenant_id
            or version.knowledge_base_id != projection.knowledge_base_id
        ):
            continue
        if projection.mode == "shared" and version.publication_state != "released":
            continue
        visible[projection.knowledge_base_id] = _ManifestKnowledgeAccess(
            version=version,
            permission=projection.permission,
            is_default_write=projection.is_default_write,
            mode=projection.mode,
        )
    return visible


def _shared_knowledge_action_descriptors(
    *,
    visible_knowledge: dict[str, _ManifestKnowledgeAccess],
    allowed_knowledge_ids: list[str],
    knowledge_version_by_base_id: dict[str, str],
    knowledge_scope_by_base_id: dict[str, str],
) -> list[CapabilityDescriptor]:
    """按每个共享库的实时授权等级生成 Agent 可发现的显式维护动作。"""
    # 先把批准合同固化为动作、最低权限、说明和模型输入约束。
    action_specs: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
        (
            "knowledge_list_versions",
            "reader",
            "列出当前团队已授权共享知识库的正式版本、草稿和允许动作。",
            {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "publication_state": {
                        "type": "string",
                        "enum": ["draft", "released", "rejected"],
                    },
                },
            },
        ),
        (
            "knowledge_create_draft",
            "editor",
            "从共享知识库当前正式版本创建可编辑草稿。",
            {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "expected_published_version_id": {"type": "string"},
                    "change_reason": {"type": "string", "minLength": 1},
                    "source_task_id": {"type": "string"},
                    "source_references": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": ["change_reason", "idempotency_key"],
            },
        ),
        (
            "knowledge_update_draft",
            "editor",
            "向已授权共享知识草稿写入一份带来源的文本或 Markdown 文档。",
            {
                "type": "object",
                "properties": {
                    "draft_version_id": {"type": "string"},
                    "title": {"type": "string", "minLength": 1},
                    "filename": {"type": "string", "minLength": 1},
                    "content": {"type": "string", "minLength": 1},
                    "source_references": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "draft_version_id",
                    "title",
                    "filename",
                    "content",
                    "idempotency_key",
                ],
            },
        ),
        (
            "knowledge_publish_draft",
            "publisher",
            "校验共享知识草稿后原子发布为所有绑定团队的唯一正式版本。",
            {
                "type": "object",
                "properties": {
                    "draft_version_id": {"type": "string"},
                    "expected_published_version_id": {"type": "string"},
                    "change_reason": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "draft_version_id",
                    "expected_published_version_id",
                    "change_reason",
                    "idempotency_key",
                ],
            },
        ),
        (
            "knowledge_reject_draft",
            "publisher",
            "驳回共享知识草稿并保留其历史与来源。",
            {
                "type": "object",
                "properties": {
                    "draft_version_id": {"type": "string"},
                    "change_reason": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "draft_version_id",
                    "change_reason",
                    "idempotency_key",
                ],
            },
        ),
        (
            "knowledge_rollback",
            "publisher",
            "把共享知识库全局正式指针回滚到同库历史正式版本。",
            {
                "type": "object",
                "properties": {
                    "knowledge_base_id": {"type": "string"},
                    "target_version_id": {"type": "string"},
                    "expected_published_version_id": {"type": "string"},
                    "change_reason": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "target_version_id",
                    "expected_published_version_id",
                    "change_reason",
                    "idempotency_key",
                ],
            },
        ),
    )
    # 再逐动作筛选可操作知识库；没有任何合格目标的动作不进入冻结清单。
    descriptors: list[CapabilityDescriptor] = []
    for name, required_permission, description, input_schema in action_specs:
        permitted_ids = [
            knowledge_base_id
            for knowledge_base_id in allowed_knowledge_ids
            if visible_knowledge[knowledge_base_id].mode == "shared"
            and KnowledgeAccessService.permission_allows(
                visible_knowledge[knowledge_base_id].permission,
                required_permission,
            )
        ]
        if not permitted_ids:
            continue
        frozen_versions = {
            knowledge_base_id: knowledge_version_by_base_id[knowledge_base_id]
            for knowledge_base_id in permitted_ids
        }
        # 每个动作携带同一轮的正式版本映射，实时执行时仅收窄权限而不换版本。
        descriptors.append(
            CapabilityDescriptor(
                capability_id=f"knowledge.{name.removeprefix('knowledge_')}",
                name=name,
                kind="knowledge",
                capability_scope=(
                    "sop_specific"
                    if all(
                        knowledge_scope_by_base_id[knowledge_base_id] == "sop_specific"
                        for knowledge_base_id in permitted_ids
                    )
                    else "general"
                ),
                description=description,
                input_schema=input_schema,
                metadata={
                    "allowed_knowledge_base_ids": permitted_ids,
                    "allowed_knowledge_base_version_ids": list(frozen_versions.values()),
                    "knowledge_version_by_base_id": frozen_versions,
                    "required_permission": required_permission,
                },
            )
        )
    return descriptors


def _internal_capability_descriptors() -> list[CapabilityDescriptor]:
    return [
        CapabilityDescriptor(
            capability_id="builtin.deliverables.list",
            name="list_published_deliverables",
            kind="internal",
            description=(
                "List recent files published by earlier TaskFrames in this same conversation. "
                "Use this before continuing work from a document delivered in a previous turn."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 20,
                    },
                },
                "additionalProperties": False,
            },
            metadata={"provider": "harness", "side_effect": "read"},
        ),
        CapabilityDescriptor(
            capability_id="builtin.deliverables.read",
            name="read_published_deliverable",
            kind="internal",
            description=(
                "Read UTF-8 content from one file returned by list_published_deliverables. "
                "Pass its task_frame_id and path exactly; use the continuation token when truncated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_frame_id": {"type": "string", "minLength": 1},
                    "path": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "max_bytes": {"type": "integer", "minimum": 1},
                    "continuation_token": {"type": "string", "minLength": 1},
                },
                "required": ["task_frame_id", "path"],
                "additionalProperties": False,
            },
            metadata={"provider": "harness", "side_effect": "read"},
        ),
        CapabilityDescriptor(
            capability_id="builtin.discovery.search",
            name="capability_search",
            kind="internal",
            description=(
                "Search the complete frozen capability catalog for skills and tools "
                "relevant to the current TaskRequirement. Use this when the compact "
                "catalog is truncated or no visible candidate is clearly suitable."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["general_skill", "tool", "knowledge", "file"],
                        },
                        "uniqueItems": True,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            metadata={"provider": "harness", "side_effect": "read"},
        ),
        CapabilityDescriptor(
            capability_id="builtin.discovery.describe",
            name="capability_describe",
            kind="internal",
            description=(
                "Load the full input schema for one or more authorized capabilities "
                "from the compact catalog or capability_search results. Described "
                "capabilities become callable in this TaskFrame AgentLoop."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "description": "Capability IDs or invocation names to activate.",
                    }
                },
                "required": ["capabilities"],
                "additionalProperties": False,
            },
            metadata={"provider": "harness", "side_effect": "read"},
        ),
    ]


def tool_snapshot_digest(db: Session, tool: Tool) -> str:
    """Hash every persisted field that can change an external tool invocation."""

    server_payload: dict[str, Any] | None = None
    if tool.mcp_server_id:
        server = db.get(MCPServer, tool.mcp_server_id)
        if server is not None:
            server_payload = {
                "id": server.id,
                "tenant_id": server.tenant_id,
                "transport": server.transport,
                "url": server.url,
                "headers": server.headers_json or {},
                "command": server.command,
                "args": server.args_json or [],
                "env": server.env_json or {},
                "cwd": server.cwd,
                "enabled": server.enabled,
            }
    payload = {
        "id": tool.id,
        "tenant_id": tool.tenant_id,
        "name": tool.name,
        "tool_type": tool.tool_type,
        "method": tool.method,
        "url": tool.url,
        "headers": tool.headers_json or {},
        "auth": tool.auth_json or {},
        "config": tool.config_json or {},
        "input_schema": tool.input_schema or {},
        "output_schema": tool.output_schema or {},
        "allowed_skills": tool.allowed_skills_json or [],
        "mcp_server_id": tool.mcp_server_id,
        "capability_scope": tool.capability_scope,
        "enabled": tool.enabled,
        "mcp_server": server_payload,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def general_skill_snapshot_digest(skill: GeneralSkill) -> str:
    """Hash the package and every persisted field used by the skill runner."""

    package = package_from_row(skill)
    payload = {
        "id": skill.id,
        "tenant_id": skill.tenant_id,
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "homepage": skill.homepage,
        "package_digest": package.digest,
        "metadata": skill.metadata_json or {},
        "permissions": skill.permissions_json or {},
        "runtime_config": skill.runtime_config_json or {},
        "capability_scope": skill.capability_scope,
        "status": skill.status,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _visible_general_skills(
    db: Session, tenant_id: str, agent_id: str | None
) -> list[GeneralSkill]:
    agent = get_agent(db, tenant_id, agent_id)
    rows = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id,
            GeneralSkill.status == "published",
        )
    ).all()
    if agent_id and not agent:
        return []
    if not agent or agent.is_overall:
        return [
            row for row in rows if is_open_gallery_resource(db, tenant_id, "general_skill", row)
        ]
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.status == "active",
        )
    ).all()
    by_id = {row.id: row for row in rows}
    visible: list[GeneralSkill] = []
    for binding in bindings:
        row = by_id.get(binding.resource_id)
        if row is not None and is_bound_resource_visible_for_agent(
            db, tenant_id, "general_skill", row, binding
        ):
            visible.append(row)
    return visible


def _scope(row: object | None) -> str | None:
    value = str(getattr(row, "capability_scope", "") or "").strip()
    return value if value in {"general", "sop_specific"} else None


def _unavailable(
    capability_id: str,
    name: str,
    kind: str,
    scope: str,
    reason: str,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        capability_scope=scope,  # type: ignore[arg-type]
        available=False,
        unavailable_reason=reason,
    )


def _explicit_reason(row: object | None, tenant_id: str) -> str:
    if row is None or str(getattr(row, "tenant_id", "")) != tenant_id:
        return "SOP 引用的能力不存在。"
    return "SOP 引用的能力未发布、未启用或未绑定到当前员工。"


def _snapshot_revision(
    available: list[CapabilityDescriptor],
    unavailable: list[CapabilityDescriptor],
) -> str:
    def stable_key(item: CapabilityDescriptor) -> tuple[str, str, str]:
        return (item.kind, item.name, item.capability_id)

    payload = {
        "available": [item.model_dump(mode="json") for item in sorted(available, key=stable_key)],
        "unavailable": [
            item.model_dump(mode="json") for item in sorted(unavailable, key=stable_key)
        ],
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )


def _available_invocation_name(
    preferred: str,
    capability_id: str,
    available: list[CapabilityDescriptor],
) -> str:
    # Knowledge is appended after external tools, so reserve its stable builtin
    # name up front as well as every descriptor already emitted.
    used = {item.name for item in available} | RESERVED_HARNESS_CAPABILITY_NAMES
    if preferred not in used:
        return preferred
    base = f"external_tool.{capability_id}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}.{suffix}"
        suffix += 1
    return candidate
