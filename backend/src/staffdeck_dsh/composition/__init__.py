"""Composition layer: StaffComposition projection, logical slots, and the immutable snapshot."""

from staffdeck_dsh.composition.compiler import (
    DEFAULT_HOOKS,
    CapabilityGrant,
    CompositionCompiler,
    CompositionSnapshot,
    HookPlan,
    SopExecutionPlan,
    compile_hooks,
)
from staffdeck_dsh.composition.slots import (
    RESOURCE_TYPE_FOR_OPERATION,
    ResolvedSlot,
    SlotDeclaration,
    declared_slots,
    resolve_slots,
    sop_slots,
)
from staffdeck_dsh.composition.staff import (
    CapabilityBindingView,
    ChannelView,
    SessionPolicy,
    SopView,
    StaffComposition,
    TeamView,
    project_staff,
)

__all__ = [
    "DEFAULT_HOOKS", "CapabilityGrant", "CompositionCompiler", "CompositionSnapshot", "HookPlan",
    "SopExecutionPlan", "compile_hooks", "RESOURCE_TYPE_FOR_OPERATION", "ResolvedSlot", "SlotDeclaration",
    "declared_slots", "resolve_slots", "sop_slots", "CapabilityBindingView", "ChannelView", "SessionPolicy",
    "SopView", "StaffComposition", "TeamView", "project_staff",
]
