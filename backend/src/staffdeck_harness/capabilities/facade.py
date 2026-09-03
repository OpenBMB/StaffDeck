"""Guarded Facades: the only doors into Knowledge, GeneralSkill, Tool/MCP/A2A,
Sandbox and Artifact services.

Each facade:

1. runs the PEP for the operation against the concrete resource (via ``Guard``),
2. re-validates the live row against the frozen snapshot (revoked/archived/
   changed-since-activation), and
3. calls the *existing* legacy service — ``KnowledgeService``, ``ToolExecutor``,
   ``package_from_row`` + workspace materialization, ``HarnessExecutor`` — so no
   second implementation of any capability exists.

A facade never talks to a module's database directly; that rule is what makes
the acceptance criterion "A modules depend on the Module SDK only" hold.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlmodel import Session

from app.capabilities.local_general_skill import package_from_row
from app.core.capability_manifest import general_skill_snapshot_digest, tool_snapshot_digest
from app.core.harness_capability_invoker import (
    _materialize_general_skill_package,
    _sandbox_path,
    _skill_package_preview,
    _workspace_root,
)
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db.models import GeneralSkill, KnowledgeBase, MCPServer, ModelConfig, Tool
from app.harness import (
    HarnessArtifactAccessError,
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
    publish_changed_harness_artifacts,
    register_command_tools,
    register_skill_script_tools,
    snapshot_harness_workspace,
)
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.harness.sandbox import parse_network_policy
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall
from staffdeck_harness.composition import projection
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.security.profile import Guard


@dataclass
class FacadeDeps:
    db: Session
    guard: Guard
    security_context: SecurityContext
    model_config: ModelConfig | None = None
    agent_row: Any | None = None
    trace: Callable[[str, dict[str, Any]], None] | None = None
    remaining_seconds: Callable[[], float | None] | None = None

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.trace:
            self.trace(event, payload)


class KnowledgeFacade:
    module_id = "knowledge"

    def __init__(self, deps: FacadeDeps):
        self.d = deps

    def search(self, inv: ModuleInvocation, *, allowed_ids: set[str], version_by_base: Mapping[str, str] | None = None) -> ModuleResult:
        args = dict(inv.arguments)
        query = str(args.get("query") or "").strip()
        if not query:
            return ModuleResult.fail("INVALID_ARGUMENTS", "知识检索 query 不能为空。")
        requested = {str(i) for i in (args.get("knowledge_base_ids") or []) if str(i).strip()}
        selected = sorted(requested & allowed_ids) if requested else sorted(allowed_ids)
        if requested and not selected:
            return ModuleResult.fail("KNOWLEDGE_NOT_AVAILABLE", "请求的知识库不在当前激活范围内。")
        db, ctx = self.d.db, inv.context
        for kb_id in selected:
            row = db.get(KnowledgeBase, kb_id)
            if row is None or row.tenant_id != ctx.tenant_id or row.status == "deleted":
                return ModuleResult.fail("CAPABILITY_AUTHORIZATION_REVOKED", f"知识库 {kb_id} 已不可用。")
            ref = projection.live_resource_ref(db, ctx.tenant_id, "knowledge_base", row, agent=self.d.agent_row)
            self.d.guard.require(self.d.security_context, "knowledge.search/v1", ref)
        versions = [str((version_by_base or {}).get(k) or "") for k in selected]
        versions = [v for v in versions if v]
        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id=ctx.tenant_id,
                agent_id=ctx.agent_id if ctx.agent_id and not ctx.agent_id.endswith(":overall") else None,
                query=query,
                mode="chat",
                knowledge_base_ids=selected,
                knowledge_base_version_ids=versions,
                max_chunks=max(1, min(int(args.get("max_chunks") or 8), 12)),
            ),
            self.d.model_config,
        )
        payload = response.model_dump(mode="json")
        citations = knowledge_citations_from_results([payload])
        # The model sees a compact, citation-labelled view (like the legacy invoker's inline
        # budget); the full response travels in ``extensions`` for evidence/UI, never to the model.
        return ModuleResult.ok(_model_facing_knowledge(payload, citations), citations=tuple(citations), extensions={"evidence": payload})


_MODEL_EXCERPT_CHARS = 1200
_MODEL_MAX_ITEMS = 8


def _model_facing_knowledge(payload: dict[str, Any], citations: list[dict[str, Any]]) -> dict[str, Any]:
    """What the model gets back from knowledge_search: the evidence texts with their [N] labels.

    Full bucket rows (metadata, traces, route trace, whole documents) stay out of the
    transcript: they blow past the engine's inline result budget and get spilled to a file the
    model cannot use, and they carry nothing the model needs beyond the excerpts.
    """

    by_identity: dict[str, str] = {}
    for c in citations:
        for key in ("chunk_id", "concept_id"):
            if c.get(key):
                by_identity[str(c[key])] = str(c.get("label") or "")
    items: list[dict[str, Any]] = []
    tiers = (payload.get("evidence_pack"), payload.get("chunks"), payload.get("selected_concepts"))
    for raw in tiers:
        if not isinstance(raw, list) or not raw:
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("chunk_id") or item.get("id") or item.get("concept_id") or "")
            text = str(item.get("content") or item.get("text") or item.get("excerpt") or item.get("summary") or "")
            if not text.strip():
                continue
            items.append({
                "label": by_identity.get(ident) or (f"[{len(items) + 1}]"),
                "title": item.get("title") or item.get("section_title") or item.get("document_title") or "",
                "source": item.get("source_path") or item.get("document_id") or item.get("knowledge_base_id") or "",
                "excerpt": text[:_MODEL_EXCERPT_CHARS],
            })
            if len(items) >= _MODEL_MAX_ITEMS:
                break
        if items:
            break
    if not items:
        # fall back to bucket summaries so the model at least knows what exists
        for b in payload.get("selected_buckets") or []:
            if isinstance(b, dict) and (b.get("summary") or b.get("title")):
                items.append({"label": f"[{len(items) + 1}]", "title": b.get("title") or "", "source": b.get("knowledge_base_id") or "", "excerpt": str(b.get("summary") or "")[:_MODEL_EXCERPT_CHARS]})
                if len(items) >= _MODEL_MAX_ITEMS:
                    break
    return {
        "query": payload.get("query"),
        "hit_count": len(items),
        "results": items,
        "citations": [{"label": c.get("label"), "title": c.get("title"), "source": c.get("source_path") or c.get("document_id") or ""} for c in citations],
        "instruction": "回答时用对应的 [N] 标注引用；没有命中的内容不要臆造。",
    }


class GeneralSkillFacade:
    module_id = "general_skill"

    def __init__(self, deps: FacadeDeps, workspace_root: Path):
        self.d = deps
        self.workspace_root = workspace_root

    def consume(self, inv: ModuleInvocation, *, expected_digest: str | None = None) -> ModuleResult:
        db, ctx = self.d.db, inv.context
        args = dict(inv.arguments)
        skill_id = str(args.get("skill_id") or inv.binding_id or "")
        row = db.get(GeneralSkill, skill_id) if skill_id else None
        if row is None or row.tenant_id != ctx.tenant_id or row.status != "published":
            return ModuleResult.fail("SKILL_NOT_AVAILABLE", "通用技能当前不可用。")
        if expected_digest and general_skill_snapshot_digest(row) != expected_digest:
            return ModuleResult.fail("CAPABILITY_SNAPSHOT_CHANGED", "通用技能内容在激活后发生变化，请重新规划。")
        ref = projection.live_resource_ref(db, ctx.tenant_id, "general_skill", row, agent=self.d.agent_row)
        self.d.guard.require(self.d.security_context, "general_skill.consume/v1", ref)
        query = str(args.get("query") or "").strip()
        operation = str(args.get("operation") or "read").lower()
        if operation not in {"read", "execute"}:
            return ModuleResult.fail("INVALID_ARGUMENTS", "通用技能 operation 只能是 read。")
        package = package_from_row(row)
        package_root, file_paths = _materialize_general_skill_package(self.workspace_root, package)
        entry = next((p for p in file_paths if p.removeprefix(package_root + "/") == package.entrypoint), f"{package_root}/{package.entrypoint}")
        data = {
            "kind": "general_skill",
            "slug": row.slug,
            "operation": "read",
            "query": query,
            "package": _skill_package_preview(row),
            "package_root": package_root,
            "sandbox_package_root": _sandbox_path(package_root),
            "entrypoint_path": entry,
            "sandbox_entrypoint_path": _sandbox_path(entry),
            "file_paths": file_paths,
            "skill_markdown": row.skill_markdown,
        }
        if operation == "execute":
            data["requested_operation"] = "execute"
            data["compatibility_notice"] = "execute 已弃用并安全降级为 read。"
        self.d.emit("general_skill_trace", {"skill_slug": row.slug, "skill_name": row.name, "operation": "read", "phase": "instructions_loaded"})
        return ModuleResult.ok(data)


class ToolFacade:
    """HTTP, MCP and A2A tools all go through ``ToolExecutor`` (which already dispatches by tool_type)."""

    module_id = "tool"

    def __init__(self, deps: FacadeDeps):
        self.d = deps

    def invoke(self, inv: ModuleInvocation, *, expected_digest: str | None = None, active_skill_id: str | None = None) -> ModuleResult:
        db, ctx = self.d.db, inv.context
        args = dict(inv.arguments)
        tool_id = str(args.pop("tool_id", None) or inv.binding_id or "")
        row = db.get(Tool, tool_id) if tool_id else None
        if row is None or row.tenant_id != ctx.tenant_id or not row.enabled:
            return ModuleResult.fail("TOOL_NOT_AVAILABLE", "工具当前不可用。")
        if expected_digest and tool_snapshot_digest(db, row) != expected_digest:
            return ModuleResult.fail("CAPABILITY_SNAPSHOT_CHANGED", "工具配置在激活后发生变化，请重新规划。")
        ref = projection.live_resource_ref(db, ctx.tenant_id, "tool", row, agent=self.d.agent_row)
        op = {"http": "tool.invoke/v1", "mcp": "mcp.invoke/v1", "a2a": "a2a.invoke/v1"}.get(row.tool_type, "tool.invoke/v1")
        self.d.guard.require(self.d.security_context, op, ref)
        if row.tool_type == "mcp" and row.mcp_server_id:
            server = db.get(MCPServer, row.mcp_server_id)
            if server is not None:
                self.d.guard.require(self.d.security_context, "mcp.invoke/v1", projection.live_resource_ref(db, ctx.tenant_id, "mcp_server", server, agent=self.d.agent_row))
        remaining = self.d.remaining_seconds() if self.d.remaining_seconds else None
        result = ToolExecutor(db).execute(
            ctx.tenant_id,
            ToolCall(name=row.name, arguments=args),
            active_skill_id=active_skill_id,
            agent_id=None if not ctx.agent_id or ctx.agent_id.endswith(":overall") else ctx.agent_id,
            session_id=ctx.session_id,
            invocation_id=inv.invocation_id,
            timeout_seconds_override=remaining,
        )
        payload = result.model_dump(mode="json")
        app_descriptor = payload.pop("mcp_app", None)
        payload.pop("mcp_metadata", None)
        if payload.get("success") is not True:
            err = payload.get("error") or {}
            return ModuleResult.fail(str(err.get("code") or "TOOL_ERROR"), str(err.get("message") or "工具调用失败"), extensions={"raw": payload})
        if isinstance(app_descriptor, dict):
            app_descriptor["initial_result"] = payload.get("data")
            self.d.emit("harness_mcp_app_view", {"tool_name": row.name, "mcp_app": app_descriptor})
        return ModuleResult.ok(payload.get("data"), artifacts=tuple(payload.get("artifacts") or ()))


class SandboxFacade:
    """Sandboxed file/command tools (the legacy ``HarnessExecutor`` registry)."""

    module_id = "sandbox"

    def __init__(self, deps: FacadeDeps, *, workspace_root: Path, run_id: str, task_frame_id: str, sandbox_enabled: bool, network_mode: str, allowed_domains: tuple[str, ...]):
        self.d = deps
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._snapshot = snapshot_harness_workspace(self.workspace_root)
        registry = build_file_tool_registry()
        register_command_tools(registry)
        register_skill_script_tools(registry)
        self._registry = registry
        self._executor = HarnessExecutor(registry)
        ctx = deps.security_context
        self._context = HarnessToolContext(
            run_id=run_id,
            task_frame_id=task_frame_id,
            tenant_id=ctx.tenant_id,
            workspace_root=self.workspace_root,
            sandbox_enabled=sandbox_enabled,
            sandbox_network_mode=parse_network_policy(network_mode),
            sandbox_allowed_domains=allowed_domains,
        )

    def tool_names(self) -> list[str]:
        return [spec.name for spec in self._registry.specs()]

    def schemas(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "description": s.description, "input_schema": dict(s.input_schema)} for s in self._registry.specs()]

    def execute(self, inv: ModuleInvocation) -> ModuleResult:
        ref = projection.ResourceRef(type="capability", id=f"sandbox:{inv.arguments.get('tool') or inv.operation}", tenant_id=inv.context.tenant_id, attributes={"binding_status": "active", "private_to_agent": True})
        self.d.guard.require(self.d.security_context, "sandbox.execute/v1", ref)
        name = str(inv.arguments.get("tool") or "")
        args = dict(inv.arguments.get("arguments") or {})
        result = self._executor.execute(self._context, HarnessToolCall(call_id=inv.invocation_id, name=name, arguments=args))
        if not result.success:
            err = result.error
            return ModuleResult.fail(err.code if err else "SANDBOX_ERROR", err.message if err else "沙箱执行失败", extensions={"details": dict(err.details) if err else {}})
        data = dict(result.data or {})
        if name in {"exec_command", "run_skill_script"} and data.get("ok") is not True:
            timed_out = bool(data.get("timed_out"))
            return ModuleResult.fail(
                "COMMAND_TIMEOUT" if timed_out else "COMMAND_EXIT_NONZERO",
                "受控进程执行超时。" if timed_out else "受控进程执行完成，但返回了非零退出码。",
                extensions={"data": data},
            )
        return ModuleResult.ok(data)

    def discover_artifacts(self, task_frame_id: str) -> list[dict[str, Any]]:
        try:
            discovered = publish_changed_harness_artifacts(self.workspace_root, task_frame_id, self._snapshot, operation="workspace_discovery")
        except (HarnessArtifactAccessError, OSError):
            return []
        out: list[dict[str, Any]] = []
        for raw in discovered:
            item = dict(raw)
            rel = str(item.get("path") or "")
            name = Path(rel).name
            item.update({"sandbox_path": _sandbox_path(rel), "display_name": name, "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "source": "harness_v3.workspace_discovery"})
            out.append(item)
        return out


def workspace_for(ctx: InvocationContext, db: Session) -> Path:
    return _workspace_root(ctx.tenant_id, ctx.session_id, ctx.task_frame_id or ctx.turn_id, db=db)


__all__ = ["FacadeDeps", "KnowledgeFacade", "GeneralSkillFacade", "ToolFacade", "SandboxFacade", "workspace_for", "SANDBOX_WORKSPACE", "harness_task_workspace_path", "PermissionDenied"]
