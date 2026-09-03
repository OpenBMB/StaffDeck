"""Deployment-level SecurityProfile selection and the Guard used by every host.

Exactly one profile is active per process. Selection is configuration, never a
per-Staff or per-Turn choice, and a missing/unknown profile fails at startup.

``Guard`` is the small object hosts hold instead of a raw ``PepPort``: it binds
the module id and its ``PolicyActionMapper`` so a host writes

    guard.require(ctx, "knowledge.search/v1", resource)

and gets a ``PermissionDenied`` (or ``AuthorizationUnavailable``) exception
with a stable code. Building a host without a Guard raises ``PepBindingMissing``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Mapping, Sequence

from staffdeck_dsh.contracts.errors import AuthorizationUnavailable, PepBindingMissing, PermissionDenied
from staffdeck_dsh.contracts.security import (
    DEFAULT_ACTION_MAP,
    Decision,
    PolicyActionMapper,
    ResourceRef,
    SecurityContext,
    SecurityProfile,
    SecurityProfileName,
)
from staffdeck_dsh.security.business_base import BaseAuthzConfig, BaseWorkloadConfig, build_business_base_profile
from staffdeck_dsh.security.oss_local import build_oss_local_profile

_lock = threading.Lock()
_active: SecurityProfile | None = None


def _read(settings: Any, name: str, default: Any = "") -> Any:
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    return os.environ.get(name.upper(), default)


def build_profile(settings: Any = None, *, registry: Any = None) -> SecurityProfile:
    """Build the profile for ``settings``.

    With ``registry`` given, the active ``security.pep`` module of *that*
    registry decides (its ``build(settings)``); otherwise the profile named by
    ``security_profile`` (default OSS_LOCAL) is built directly.
    """

    if registry is not None:
        from staffdeck_dsh.contracts.manifest import SlotName

        installed = registry.provider(SlotName.SECURITY_PEP)
        build = getattr(installed.provider, "build", None) if installed is not None else None
        if callable(build):
            return build(settings)
    name = str(_read(settings, "security_profile", "OSS_LOCAL") or "OSS_LOCAL").upper()
    if name == "OSS_LOCAL":
        return build_oss_local_profile()
    if name == "BUSINESS_BASE":
        authz = BaseAuthzConfig(
            url=str(_read(settings, "base_authz_url", "")),
            decision_token=str(_read(settings, "base_authz_decision_token", "")),
            control_token=str(_read(settings, "base_authz_control_token", "")),
            timeout_seconds=float(_read(settings, "base_authz_timeout_seconds", 3.0) or 3.0),
            pending_timeout_seconds=float(_read(settings, "base_authz_pending_timeout_seconds", 3.0) or 3.0),
        )
        internal_url = str(_read(settings, "base_identity_internal_url", ""))
        client_id = str(_read(settings, "base_identity_runtime_client_id", ""))
        client_secret = str(_read(settings, "base_identity_runtime_client_secret", ""))
        workload = (
            BaseWorkloadConfig(internal_url=internal_url, client_id=client_id, client_secret=client_secret,
                               audience=str(_read(settings, "base_workload_identity_audience", "staffdeck-gateway")))
            if internal_url and client_id and client_secret
            else None
        )
        return build_business_base_profile(authz=authz, workload=workload)
    raise ValueError(f"unknown security_profile {name!r}; expected OSS_LOCAL or BUSINESS_BASE")


def install_profile(profile: SecurityProfile) -> SecurityProfile:
    global _active
    with _lock:
        _active = profile
    return profile


def get_profile(settings: Any = None) -> SecurityProfile:
    """The active profile. Only built lazily for a caller that owns settings; hosts get the installed one."""

    global _active
    if _active is None:
        with _lock:
            if _active is None:
                if settings is None:
                    from staffdeck_dsh.contracts.errors import ModuleSdkError

                    raise ModuleSdkError("security profile is not installed yet (runtime starting or restarting)")
                _active = _profile_from_registry(settings) or build_profile(settings)
    return _active


def peek_profile() -> SecurityProfile | None:
    with _lock:
        return _active


def _profile_from_registry(settings: Any) -> SecurityProfile | None:
    """Exactly one ``security.pep`` module is active; it decides the profile."""

    try:
        from staffdeck_dsh.contracts.manifest import SlotName
        from staffdeck_dsh.modules.registry import peek_registry

        reg = peek_registry()
        installed = reg.provider(SlotName.SECURITY_PEP) if reg is not None else None
    except Exception:
        return None
    if installed is None:
        return None
    build = getattr(installed.provider, "build", None)
    return build(settings) if callable(build) else None


def reset_profile() -> None:
    global _active
    with _lock:
        _active = None


class Guard:
    """Host-side PEP binding for one module."""

    def __init__(self, module_id: str, profile: SecurityProfile | None, mapper: PolicyActionMapper | None = None):
        if profile is None:
            raise PepBindingMissing(f"module {module_id!r} was constructed without a SecurityProfile")
        self.module_id = module_id
        self.profile = profile
        self.mapper = mapper or PolicyActionMapper(DEFAULT_ACTION_MAP)

    @property
    def name(self) -> SecurityProfileName:
        return self.profile.name

    def decide(self, ctx: SecurityContext, operation: str, resource: ResourceRef) -> Decision:
        action, _ = self.mapper.map(operation)
        return self.profile.pep.authorize(ctx, self.module_id, action, resource)

    def require(self, ctx: SecurityContext, operation: str, resource: ResourceRef) -> Decision:
        decision = self.decide(ctx, operation, resource)
        if decision.allowed:
            return decision
        if decision.reason.startswith("authorization unavailable") or decision.pending:
            raise AuthorizationUnavailable(decision.reason, details={"operation": operation, "resource": resource.id})
        raise PermissionDenied(
            decision.reason,
            details={"operation": operation, "resource_type": resource.type, "resource": resource.id, "profile": self.name},
        )

    def filter(self, ctx: SecurityContext, operation: str, resources: Sequence[ResourceRef]) -> list[ResourceRef]:
        action, _ = self.mapper.map(operation)
        return self.profile.pep.filter(ctx, self.module_id, action, list(resources))


def guard_for(module_id: str, *, profile: SecurityProfile | None = None, mapping: Mapping[str, tuple[str, str]] | None = None) -> Guard:
    mapper = PolicyActionMapper(mapping) if mapping else None  # type: ignore[arg-type]
    return Guard(module_id, profile or get_profile(), mapper)
