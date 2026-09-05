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
