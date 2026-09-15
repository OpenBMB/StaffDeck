"""Built-in source adapters. All local resource ORM knowledge stays here."""
from __future__ import annotations

from dataclasses import replace

from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.sources import ResourceDescriptor


class LocalStaffSource:
    def __init__(self, db):
        self.db = db

    def directory(self, context, *, authorization=""):
        from sqlmodel import select
        from app.db.models import AgentProfile, AgentUsage, User
        from app.api.agents import agent_read, _bindings_by_agent, _agent_hidden_from_staffdeck
        from staffdeck_harness.composition.projection import agent_ref
        from staffdeck_harness.security.oss_local import build_oss_local_profile
        from staffdeck_harness.modules.registry import peek_registry
        registry = self.db.info.get('staffdeck_registry') or peek_registry()
        profile = getattr(registry, 'security_profile', None) or build_oss_local_profile()
        user = self.db.get(User, context.user_id)
        if user is None or user.tenant_id != context.tenant_id:
            raise PermissionDenied('员工目录身份不匹配')
        identity = profile.identity.from_user(user)
        rows = self.db.exec(select(AgentProfile).where(AgentProfile.tenant_id == context.tenant_id)
            .order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())).all()
        bindings = _bindings_by_agent(self.db, context.tenant_id)
        added = set(self.db.exec(select(AgentUsage.agent_id).where(AgentUsage.tenant_id == context.tenant_id,
                                                                 AgentUsage.user_id == context.user_id)).all())
        entries = []
        for row in rows:
            if _agent_hidden_from_staffdeck(row):
                continue
            actions = [action for action in ('view', 'use', 'manage')
                if profile.pep.authorize(identity, 'directory', action, agent_ref(row)).allowed]
            if 'view' not in actions:
                continue
            entries.append({'profile': agent_read(row, bindings.get(row.id, [])).model_dump(),
                'owner_user_id': (row.metadata_json or {}).get('owner_user_id'),
                'public': (row.metadata_json or {}).get('published_to_gallery') is True,
                'shared': False, 'added': row.id in added, 'allowed_actions': actions})
        return {'schema_version': 1, 'tenant_id': context.tenant_id, 'subject_user_id': context.user_id, 'entries': entries}

    def resource_timeline(self, context, *, authorization=""):
        from datetime import datetime
        from app.api.agents import _local_agent_resource_timeline_events
        from staffdeck_harness.contracts.staff import StaffResourceAssignment
        return tuple(StaffResourceAssignment(row.id, context.tenant_id, context.staff_id, row.kind,
            datetime.fromisoformat(row.timestamp.replace("Z", "+00:00")), row.label)
            for row in _local_agent_resource_timeline_events(self.db, context.tenant_id, context.staff_id))

    def profile(self, context):
        from app.agents.branching import get_agent
        from staffdeck_harness.contracts.staff import StaffProfile
        from staffdeck_harness.composition.projection import agent_ref
        row = get_agent(self.db, context.tenant_id, context.staff_id)
        if row is None or row.tenant_id != context.tenant_id:
            raise ModuleSdkError("员工不存在于当前来源", code="STAFF_NOT_FOUND")
        return StaffProfile(row.id, row.tenant_id, row.name, row.status, agent_ref(row),
            row.is_overall, row.description, row.persona_prompt, row.harness_max_actions, dict(row.metadata_json or {}))

    def profiles(self, context, staff_ids):
        from sqlmodel import select
        from app.db.models import AgentProfile
        from staffdeck_harness.contracts.staff import StaffProfile
        from staffdeck_harness.composition.projection import agent_ref
        rows = self.db.exec(select(AgentProfile).where(AgentProfile.tenant_id == context.tenant_id,
                                                     AgentProfile.id.in_(staff_ids))).all()
        return [StaffProfile(row.id, row.tenant_id, row.name, row.status, agent_ref(row),
                row.is_overall, row.description, metadata_json=dict(row.metadata_json or {})) for row in rows]

    def reference(self, context):
        from app.agents.branching import get_agent, get_overall_agent
        from app.db.models import ChatSession
        from staffdeck_harness.composition.projection import agent_ref, runtime_staff_ref
        from staffdeck_harness.contracts.security import ResourceRef

        agent = (get_agent(self.db, context.tenant_id, context.staff_id)
                 if context.staff_id else get_overall_agent(self.db, context.tenant_id))
        if agent is None and context.staff_id:
            raise ModuleSdkError("target Staff unavailable", code="STAFF_NOT_FOUND")
        if agent is not None and (agent.tenant_id != context.tenant_id or agent.status != "active"):
            raise PermissionDenied("target Staff unavailable")
        ref = agent_ref(agent) if agent else ResourceRef(
            "agent", f"{context.tenant_id}:overall", context.tenant_id,
            {"is_overall": True},
        )
        session = self.db.get(ChatSession, context.session_id) if context.session_id else None
        return runtime_staff_ref(self.db, ref, session) if session else ref

    def resolve(self, context):
        from staffdeck_harness.composition.staff import project_staff

        return project_staff(self.db, context.tenant_id, context.staff_id, include_sops=False)

    def model(self, context, model_id=None, role="default"):
        from app.llm.model_config_resolver import resolve_model_config_for_runtime
        from app.agents.branching import model_for_agent
        if model_id:
            return resolve_model_config_for_runtime(self.db, context.tenant_id, model_id)
        return model_for_agent(self.db, context.tenant_id, context.staff_id, role)


class LocalSopSource:
    def __init__(self, db):
        self.db = db

    def resolve(self, context, staff):
        from app.agents.branching import get_agent
        from staffdeck_harness.composition.staff import _sops

        agent = get_agent(self.db, context.tenant_id, staff.staff_id)
        return _sops(self.db, context.tenant_id, agent)

    def reference(self, context, sop_id):
        from app.agents.branching import visible_skill, get_agent
        from staffdeck_harness.composition.projection import live_resource_ref
        # The caller's Session may have cached old binding state before a long model call.
        from sqlmodel import Session
        with Session(self.db.get_bind()) as fresh:
            aid = None if context.staff_id and context.staff_id.endswith(":overall") else context.staff_id
            skill = visible_skill(fresh, context.tenant_id, sop_id, aid)
            if skill is None:
                raise PermissionDenied("SOP 已下线或解绑")
            return live_resource_ref(fresh, context.tenant_id, "sop", skill,
                                     agent=get_agent(fresh, context.tenant_id, aid))


class LocalIdentitySource:
    def __init__(self, db):
        self.db = db

    def resolve(self, context, identity):
        from app.db.models import User

        user = self.db.get(User, context.user_id) if context.user_id else None
        if context.user_id and (user is None or user.tenant_id != context.tenant_id):
            raise PermissionDenied("request identity unavailable")
        if user is not None:
            return identity.from_user(user, channel=context.channel)
        return identity.from_service(f"channel:{context.channel}", context.tenant_id)


class LocalResourceCatalog:
    def __init__(self, db):
        self.db = db

    def resolve(self, context, resource_type, resource_id, operation):
        from app.agents.branching import get_agent
        from app.core.capability_manifest import general_skill_snapshot_digest, tool_snapshot_digest
        from app.db.models import GeneralSkill, KnowledgeBase, MCPServer, Skill, Tool
        from staffdeck_harness.composition.projection import live_resource_ref

        model = {"tool": Tool, "mcp_server": MCPServer, "general_skill": GeneralSkill,
                 "knowledge_base": KnowledgeBase, "sop": Skill, "skill": Skill}.get(resource_type)
        row = self.db.get(model, resource_id) if model else None
        if row is None or row.tenant_id != context.tenant_id:
            raise ModuleSdkError("资源不存在或不属于当前租户", code="RESOURCE_UNAVAILABLE")
        agent = get_agent(self.db, context.tenant_id, context.staff_id)
        ref = live_resource_ref(self.db, context.tenant_id, resource_type, row, agent=agent)
        descriptor = ResourceDescriptor(
            ref=ref, name=getattr(row, "name", resource_id), operation=operation,
            available=getattr(row, "enabled", True) and getattr(row, "status", "active")
            not in {"disabled", "deleted", "archived"},
        )
        if resource_type == "tool":
            config = row.config_json or {}
            idem = config.get("idempotency") or {}
            related = ()
            if row.tool_type == "mcp" and row.mcp_server_id:
                server = self.db.get(MCPServer, row.mcp_server_id)
                if server is None or server.tenant_id != context.tenant_id:
                    raise ModuleSdkError("MCP 服务不可用", code="RESOURCE_UNAVAILABLE")
                related = (live_resource_ref(self.db, context.tenant_id, "mcp_server", server, agent=agent),)
            return replace(descriptor, description=row.description or "",
                input_schema=dict(row.input_schema or {}), digest=tool_snapshot_digest(self.db, row),
                operation={"mcp": "mcp.invoke/v1", "a2a": "a2a.invoke/v1"}.get(row.tool_type, operation),
                side_effecting=row.tool_type != "a2a" and str(row.method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"},
                replayable=idem.get("enabled") is not False,
                idempotency_key_fields=tuple(idem.get("key_fields") or ()),
                metadata={"tool_type": row.tool_type, "source_tool_name": row.name,
                          "display_name": row.display_name}, related_resources=related)
        if resource_type == "general_skill":
            return replace(descriptor, description=str((row.metadata_json or {}).get("description") or ""),
                digest=general_skill_snapshot_digest(row), available=row.status == "published",
                metadata={"slug": row.slug})
        return descriptor


class SourceModule:
    def __init__(self, adapter):
        self.adapter = adapter

    def build(self, services):
        return self.adapter(services)

    def binding_manager(self, db, request, agent_id):
        from contextlib import nullcontext
        from staffdeck_harness.runtime.resource_bindings import LocalEmployeeBindings
        if self.adapter is not LocalStaffSource:
            raise ModuleSdkError('该来源不管理员工绑定', code='BINDING_MANAGEMENT_UNAVAILABLE')
        return nullcontext(LocalEmployeeBindings(db, request, agent_id))


def local_agent(db, context):
    from app.agents.branching import get_agent
    return get_agent(db, context.tenant_id,
                     None if context.staff_id and context.staff_id.endswith(":overall") else context.staff_id)


def register_sources(registry):
    from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
    from staffdeck_harness.modules.registry import manifest

    for name, label, slot, adapter in (
        ("source.staff.local", "本地员工配置来源", SlotName.STAFF_SOURCE, LocalStaffSource),
        ("source.sop.local", "本地 SOP 定义来源", SlotName.SOP_SOURCE, LocalSopSource),
        ("source.identity.local", "本地执行身份来源", SlotName.IDENTITY_SOURCE, LocalIdentitySource),
        ("resource.catalog.local", "本地能力目录", SlotName.RESOURCE_CATALOG, LocalResourceCatalog),
    ):
        registry.install(manifest(name, label, kind=ModuleKind.TRUSTED, slots=[slot],
            metadata={"switchable": True, "source_contract": "v1",
                      "exports_features": ["staff.ids.oss", "staff.directory/v1", "staff.binding-management/v1"] if slot == SlotName.STAFF_SOURCE else ['catalog.descriptor.local/v1'] if slot == SlotName.RESOURCE_CATALOG else [],
                      "requires_features": ["staff.ids.oss"] if slot == SlotName.SOP_SOURCE else []}), SourceModule(adapter), slot=slot)
