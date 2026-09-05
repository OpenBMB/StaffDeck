"""Trusted host: select a lifecycle provider; keep authorization outside plugin code."""

from __future__ import annotations

from staffdeck_harness.contracts.errors import ContractIncompatible, ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.sop.contracts import SopDependencies, SopRuntimePort


class SopHost:
    def __init__(self, dependencies: SopDependencies, registry=None):
        self.dependencies = dependencies
        self.registry = registry
        self.guard = None
        self.context = None
        if registry is None:
            # v2 compatibility uses the same implementation without loading DSH/Node.
            from staffdeck_harness.sop.module import SopRuntimeModule

            provider = SopRuntimeModule()
        else:
            candidates = registry.providers(SlotName.RUNTIME_SOP)
            if len(candidates) != 1:
                raise ModuleSdkError("必须装配一个 SOP 运行模块", code="SOP_RUNTIME_UNAVAILABLE")
            provider = candidates[0].provider
        self.core = provider.build(dependencies)
        if not isinstance(self.core, SopRuntimePort):
            raise ContractIncompatible("SOP module does not implement SopRuntimePort")

    def bind_security(self, guard, context):
        self.guard, self.context = guard, context

    def _authorize(self, session, skill):
        if skill is None:
            return
        if skill.tenant_id != session.tenant_id:
            raise ModuleSdkError("SOP 不属于当前租户", code="PERMISSION_DENIED")
        if self.registry is None:
            return  # compatibility entry already uses visible_skill/visible_published_skills
        if self.guard is None or self.context is None:
            raise ModuleSdkError("SOP 权限上下文未绑定", code="PEP_BINDING_MISSING")
        from app.db.models import AgentProfile
        from staffdeck_harness.composition.projection import live_resource_ref

        db = self.dependencies.db
        agent = db.get(AgentProfile, session.agent_id) if session.agent_id else None
        ref = live_resource_ref(db, session.tenant_id, "sop", skill, agent=agent)
        self.guard.require(self.context, "sop.execute/v1", ref)

    def list_published_skills(self, tenant_id, agent_id=None):
        return self.core.list_published_skills(tenant_id, agent_id)

    def get_active_skill(self, tenant_id, skill_id, agent_id=None):
        return self.core.get_active_skill(tenant_id, skill_id, agent_id)

    def drop_unavailable_skill_state(self, tenant_id, session, skills):
        return self.core.drop_unavailable_skill_state(tenant_id, session, skills)

    def activate_frame(self, session, row, skills):
        skill = next((s for s in skills if s.skill_id == row.skill_id), None)
        self._authorize(session, skill)
        return self.core.activate_frame(session, row, skills)

    def after_execution(
        self, tenant_id, session, skill, requirement, result, router_decision, *, remaining_actions
    ):
        self._authorize(session, skill)  # changed permissions cannot authorize a transition
        return self.core.after_execution(
            tenant_id,
            session,
            skill,
            requirement,
            result,
            router_decision,
            remaining_actions=remaining_actions,
        )

    def restore_task_frame(self, session, frame):
        # Restore is a session projection, not authorization to execute. Activation rechecks PEP.
        return self.core.restore_task_frame(session, frame)

    def complete_current_skill(self, session):
        return self.core.complete_current_skill(session)
