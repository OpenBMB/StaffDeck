"""SOP host: owns authorization and applies isolated state changes in the caller transaction."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import fields
from types import SimpleNamespace
from staffdeck_harness.contracts.errors import ContractIncompatible, ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.sop import SopDefinition, SopState
from staffdeck_harness.sop.contracts import SopDependencies, SopRuntimePort


class SopHost:
    def __init__(self, dependencies: SopDependencies, registry=None):
        self.dependencies, self.registry = dependencies, registry
        self.guard = self.context = None
        self._definitions = None
        self._refs = {}
        self._live_ref = None
        self._effects = []
        if registry is None:
            from staffdeck_harness.sop.module import SopRuntimeModule
            provider = SopRuntimeModule()
        else:
            candidates = registry.providers(SlotName.RUNTIME_SOP)
            if len(candidates) != 1:
                raise ModuleSdkError("必须装配一个 SOP 运行模块", code="SOP_RUNTIME_UNAVAILABLE")
            provider = candidates[0].provider
        # Providers can only queue effects; they never receive a Session or write callback.
        events = SimpleNamespace(record=lambda *a: self._effects.append(("event", deepcopy(a))),
                                 execution_engine="harness_v3")
        self.core = provider.build(SopDependencies(events,
            lambda *a: self._effects.append(("handoff", deepcopy(a)))))
        if not isinstance(self.core, SopRuntimePort):
            raise ContractIncompatible("SOP module does not implement SopRuntimePort")

    def bind_definitions(self, views, tenant_id, *, reference_resolver=None):
        self._definitions = [SopDefinition(v.row_id, tenant_id, v.skill_id, v.version,
            v.name, deepcopy(dict(v.content))) for v in views]
        self._refs = {v.skill_id: v.ref for v in views}
        self._live_ref = reference_resolver

    def bind_security(self, guard, context):
        self.guard, self.context = guard, context

    def _authorize(self, session, skill):
        if skill is None:
            return
        if skill.tenant_id != session.tenant_id:
            raise ModuleSdkError("SOP 不属于当前租户", code="PERMISSION_DENIED")
        if self.registry is None:
            return
        if self.guard is None or self.context is None:
            raise ModuleSdkError("SOP 权限上下文未绑定", code="PEP_BINDING_MISSING")
        ref = self._refs.get(skill.skill_id)
        if ref is None:
            raise PermissionDenied("SOP 未绑定到本轮装配")
        if self._live_ref is not None:
            ref = self._live_ref(skill.skill_id)
        if ref.tenant_id != session.tenant_id or ref.type != "sop":
            raise PermissionDenied("SOP 来源返回了错误的授权对象")
        self.guard.require(self.context, "sop.execute/v1", ref)

    def list_published_skills(self, tenant_id, agent_id=None):
        if self._definitions is None:
            raise ModuleSdkError("SOP 来源尚未装配", code="SOURCE_UNAVAILABLE")
        return deepcopy([s for s in self._definitions if s.tenant_id == tenant_id])

    def get_active_skill(self, tenant_id, skill_id, agent_id=None):
        return next((s for s in self.list_published_skills(tenant_id, agent_id) if s.skill_id == skill_id), None)

    @staticmethod
    def _definition(skill):
        if skill is None:
            return None
        return SopDefinition(**{f.name: deepcopy(getattr(skill, f.name, f.default))
                                for f in fields(SopDefinition)})

    def _transition(self, method, session, *args, prefix=(), **kwargs):
        state = SopState(**{f.name: deepcopy(getattr(session, f.name, f.default))
                            for f in fields(SopState)})
        identity = (state.id, state.tenant_id, state.agent_id, state.user_id)
        self._effects.clear()
        try:
            result = getattr(self.core, method)(*prefix, state, *args, **kwargs)
            if identity != (state.id, state.tenant_id, state.agent_id, state.user_id):
                raise ModuleSdkError("SOP 模块不得更改执行身份", code="SOP_STATE_INVALID")
            # Reauthorize immediately before applying a transition, even if the provider ran slowly.
            skill = next((s for s in self._definitions or () if s.skill_id == session.active_skill_id), None)
            self._authorize(session, skill)
            for kind, payload in self._effects:
                if kind == "event" and payload[0:2] != (session.tenant_id, session.id):
                    raise ModuleSdkError("SOP event scope mismatch", code="SOP_STATE_INVALID")
                if kind == "handoff" and (payload[0], payload[1].id) != (session.tenant_id, session.id):
                    raise ModuleSdkError("SOP handoff scope mismatch", code="SOP_STATE_INVALID")
            for f in fields(SopState):
                if f.name not in {"id", "tenant_id", "agent_id", "user_id"}:
                    setattr(session, f.name, deepcopy(getattr(state, f.name)))
            for kind, payload in self._effects:
                if kind == "event":
                    self.dependencies.events.record(*payload)
                else:
                    tenant, _, definition, step = payload
                    self.dependencies.create_handoff(tenant, session, definition, step)
            return session if result is state else result
        finally:
            self._effects.clear()

    def drop_unavailable_skill_state(self, tenant_id, session, skills):
        if session.active_skill_id and not any(s.skill_id == session.active_skill_id for s in skills):
            raise ModuleSdkError("正在执行的 SOP 暂不可用，已保留原执行状态", code="SOP_NOT_AVAILABLE")
        return self._transition("drop_unavailable_skill_state", session,
            [self._definition(s) for s in skills], prefix=(tenant_id,))

    def activate_frame(self, session, row, skills):
        skill = next((s for s in skills if s.skill_id == row.skill_id), None)
        self._authorize(session, skill)
        frame = SimpleNamespace(**{key: deepcopy(getattr(row, key)) for key in
            ("tenant_id", "session_id", "kind", "skill_id", "decision", "step_id", "task_id", "slots_json")})
        return self._transition("activate_frame", session, frame, [self._definition(s) for s in skills])

    def after_execution(self, tenant_id, session, skill, requirement, result,
                        router_decision, *, remaining_actions):
        self._authorize(session, skill)
        outcome = self._transition("after_execution", session, self._definition(skill),
            requirement, result, router_decision, prefix=(tenant_id,), remaining_actions=remaining_actions)
        # Handoff creation is applied by the host after the isolated transition. Its durable
        # id therefore becomes available only now, not inside the lifecycle provider.
        if result.status == "handoff":
            from staffdeck_harness.sop.results import append_session_handoff_artifact
            handoff_id = (session.awaiting_input_json or {}).get("handoff_id")
            if handoff_id and not any(a.get("handoff_id") == handoff_id for a in result.artifacts):
                append_session_handoff_artifact(result, session)
        return outcome

    def restore_task_frame(self, session, frame):
        return self._transition("restore_task_frame", session, deepcopy(frame))

    def complete_current_skill(self, session):
        return self._transition("complete_current_skill", session)
