"""Shared service-side policy boundary, independent of the selected AgentLoop.

Existing OSS domain checks remain authoritative; this boundary invokes the configured
PEP as an additional decision, so BUSINESS_BASE applies outside the Harness as well.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.config import get_settings
from staffdeck_harness.contracts.errors import AuthorizationUnavailable, PermissionDenied
from staffdeck_harness.contracts.security import PolicyActionMapper, ResourceRef
from staffdeck_harness.security.profile import Guard, get_profile


def visible_resources(user, resources, *, module):
    """One identity projection and one optional batch decision per read catalog."""
    from dataclasses import replace
    profile = get_profile(get_settings())
    identity = profile.identity.from_user(user)
    refs = [replace(ref, attributes={**ref.attributes, 'local_boundary_checked':True}) for ref in resources]
    batch = getattr(profile.pep, 'authorize_many', None)
    decisions = batch(identity, module, 'view', refs) if callable(batch) else [
        profile.pep.authorize(identity, module, 'view', ref) for ref in refs]
    if len(decisions) != len(refs):
        raise HTTPException(503, '权限批量响应与请求不一致')
    if any(d.pending or d.reason.startswith('authorization unavailable') for d in decisions):
        raise HTTPException(503, '权限服务暂不可用')
    return [d.allowed for d in decisions]


def require_resource(
    user: Any, resource: ResourceRef, action: str, *, module: str, local_checked: bool = False
) -> None:
    profile = get_profile(get_settings())
    if local_checked:
        from dataclasses import replace

        resource = replace(
            resource, attributes={**resource.attributes, "local_boundary_checked": True}
        )
    guard = Guard(module, profile, PolicyActionMapper({"access/v1": (action, resource.type)}))
    try:
        guard.require(profile.identity.from_user(user), "access/v1", resource)
    except AuthorizationUnavailable as exc:
        raise HTTPException(503, detail=exc.message) from exc
    except PermissionDenied as exc:
        raise HTTPException(403, detail=exc.message) from exc
