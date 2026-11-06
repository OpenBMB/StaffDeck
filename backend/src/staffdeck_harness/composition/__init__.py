"""Composition layer: StaffComposition projection, logical slots, and the immutable snapshot."""

from staffdeck_harness.composition.compiler import (
    DEFAULT_HOOKS,
    CapabilityGrant,
    CompositionCompiler,
    CompositionSnapshot,
    HookPlan,
    SopExecutionPlan,
    compile_hooks,
)
from staffdeck_harness.composition.slots import (
    RESOURCE_TYPE_FOR_OPERATION,
    ResolvedSlot,
    SlotDeclaration,
    declared_slots,
    resolve_slots,
    sop_slots,
)
from staffdeck_harness.contracts.staff import (
    CapabilityBindingView,
    ChannelView,
    SessionPolicy,
    SopView,
    StaffComposition,
    TeamView,
)


def project_staff(*args, **kwargs):
    """Compatibility export; runtime assembly uses the configured Staff source."""
    from staffdeck_harness.composition.staff import project_staff as project
    return project(*args, **kwargs)

__all__ = [
    "DEFAULT_HOOKS", "CapabilityGrant", "CompositionCompiler", "CompositionSnapshot", "HookPlan",
    "SopExecutionPlan", "compile_hooks", "RESOURCE_TYPE_FOR_OPERATION", "ResolvedSlot", "SlotDeclaration",
    "declared_slots", "resolve_slots", "sop_slots", "CapabilityBindingView", "ChannelView", "SessionPolicy",
    "SopView", "StaffComposition", "TeamView", "project_staff",
]
