"""Module SDK error taxonomy.

Every error carries a stable ``code`` so hosts, facades, and the Bridge can map
failures onto wire responses without string matching. Publish-time validation
errors (slots, contracts, cycles) and runtime denials (PEP) live here so a
module author depends on one package only.
"""

from __future__ import annotations


class ModuleSdkError(Exception):
    code: str = "MODULE_SDK_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


class ContractIncompatible(ModuleSdkError):
    code = "CONTRACT_INCOMPATIBLE"


class RequiredSlotMissing(ModuleSdkError):
    code = "REQUIRED_SLOT_MISSING"


class SlotNotBound(ModuleSdkError):
    code = "SLOT_NOT_BOUND"


class SlotKindMismatch(ModuleSdkError):
    code = "SLOT_KIND_MISMATCH"


class DependencyCycle(ModuleSdkError):
    code = "DEPENDENCY_CYCLE"


class HookCycle(ModuleSdkError):
    code = "HOOK_CYCLE"


class PepBindingMissing(ModuleSdkError):
    """A protected host was constructed without a PepPort. Fail at startup."""

    code = "PEP_BINDING_MISSING"


class PermissionDenied(ModuleSdkError):
    code = "PERMISSION_DENIED"


class AuthorizationUnavailable(ModuleSdkError):
    """The external authorizer cannot answer. Business deployments fail closed."""

    code = "AUTHORIZATION_UNAVAILABLE"


class InvocationRejected(ModuleSdkError):
    code = "INVOCATION_REJECTED"


class OutcomeUnknown(ModuleSdkError):
    """A side-effecting invocation may have run; replay is forbidden until reconciled."""

    code = "OUTCOME_UNKNOWN"


class ActivationFenced(ModuleSdkError):
    """A stale generation or closed turn tried to act. Never a permission decision."""

    code = "ACTIVATION_FENCED"


class EngineUnavailable(ModuleSdkError):
    code = "ENGINE_UNAVAILABLE"
