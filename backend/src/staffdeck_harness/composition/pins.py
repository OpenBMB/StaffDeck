"""Pin SOP content and bindings per TaskFrame using the existing session state store."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json

from staffdeck_harness.composition.compiler import CapabilityGrant, SopExecutionPlan
from staffdeck_harness.composition.slots import ResolvedSlot, SlotDeclaration
from staffdeck_harness.contracts.errors import ModuleSdkError


def pin_sop(snapshot, staff, session, row, skill):
    if row.kind != "sop" or skill is None:
        return snapshot, skill
    state = deepcopy(session.context_state_json or {})
    pins = state.setdefault("sop_module_pins", {})
    saved = pins.get(row.task_id)
    identity = [session.tenant_id, session.id, snapshot.staff_id, row.skill_id]
    if saved is None:
        plan = next((s for s in snapshot.sops if s.skill_id == row.skill_id), None)
        if plan is None:
            raise ModuleSdkError("SOP 执行定义不在装配中", code="SOP_NOT_AVAILABLE")
        # Respect a server-selected scheduled definition, too.
        plan = replace(plan, version=skill.version, content=deepcopy(skill.content_json))
        saved = {"identity": identity, "plan": asdict(plan),
                 "grants": [asdict(g) for g in snapshot.grants if g.sop_id in {None, row.skill_id}]}
        pins[row.task_id] = saved
        session.context_state_json = state
    if saved.get("identity") != identity:
        raise ModuleSdkError("SOP 实例快照身份不匹配", code="SOP_SNAPSHOT_INVALID")
    grants = tuple(CapabilityGrant(**g) for g in saved["grants"])
    live = {(c.resource_type, c.resource_id) for c in staff.capabilities}
    if any((g.resource_type, g.resource_id) not in live for g in grants):
        raise ModuleSdkError("SOP 固定版本所需资源已解绑", code="SOP_BINDING_REVOKED")
    raw = saved["plan"]
    plan = SopExecutionPlan(**{**raw, "sub_sop_ids": tuple(raw["sub_sop_ids"]),
        "resolved_slots": tuple(ResolvedSlot(**{**s, "declaration": SlotDeclaration(**s["declaration"])})
                                for s in raw["resolved_slots"])})
    digest = hashlib.sha256(json.dumps(saved, sort_keys=True, default=str).encode()).hexdigest()
    pinned = replace(snapshot, snapshot_id=digest, grants=grants,
        sops=tuple(plan if s.skill_id == plan.skill_id else s for s in snapshot.sops))
    skill = deepcopy(skill)
    skill.version, skill.content_json = plan.version, deepcopy(dict(plan.content))
    return pinned, skill
