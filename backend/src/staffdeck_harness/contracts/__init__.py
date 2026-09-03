"""Module SDK contracts. Every module depends on this package and nothing else in staffdeck_harness."""

from staffdeck_harness.contracts.errors import (
    ActivationFenced,
    AuthorizationUnavailable,
    ContractIncompatible,
    DependencyCycle,
    EngineUnavailable,
    HookCycle,
    InvocationRejected,
    ModuleSdkError,
    OutcomeUnknown,
    PepBindingMissing,
    PermissionDenied,
    RequiredSlotMissing,
    SlotKindMismatch,
    SlotNotBound,
)
from staffdeck_harness.contracts.hooks import HookContext, HookDecision, merge_decisions
from staffdeck_harness.contracts.invocation import (
    CancelCommand,
    InvocationContext,
    InvocationStatus,
    ModuleInvocation,
    ModuleResult,
    Receipt,
)
from staffdeck_harness.contracts.manifest import (
    HOOK_POINTS,
    SLOT_FOR_OPERATION,
    CapabilityOperation,
    DurableBindingRef,
    HookContribution,
    HookPoint,
    ModuleKind,
    ModuleManifest,
    SlotBinding,
    SlotName,
)
from staffdeck_harness.contracts.security import (
    DEFAULT_ACTION_MAP,
    Action,
    Decision,
    IdentityPort,
    PepPort,
    PolicyActionMapper,
    ResourceRef,
    ResourceType,
    SecurityContext,
    SecurityProfile,
    SecurityProfileName,
    WorkloadContextPort,
)

__all__ = [
    "ActivationFenced", "AuthorizationUnavailable", "ContractIncompatible", "DependencyCycle",
    "EngineUnavailable", "HookCycle", "InvocationRejected", "ModuleSdkError", "OutcomeUnknown",
    "PepBindingMissing", "PermissionDenied", "RequiredSlotMissing", "SlotKindMismatch", "SlotNotBound",
    "HookContext", "HookDecision", "merge_decisions",
    "CancelCommand", "InvocationContext", "InvocationStatus", "ModuleInvocation", "ModuleResult", "Receipt",
    "HOOK_POINTS", "SLOT_FOR_OPERATION", "CapabilityOperation", "DurableBindingRef", "HookContribution",
    "HookPoint", "ModuleKind", "ModuleManifest", "SlotBinding", "SlotName",
    "DEFAULT_ACTION_MAP", "Action", "Decision", "IdentityPort", "PepPort", "PolicyActionMapper",
    "ResourceRef", "ResourceType", "SecurityContext", "SecurityProfile", "SecurityProfileName",
    "WorkloadContextPort",
]
