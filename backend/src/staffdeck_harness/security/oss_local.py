"""OSS_LOCAL security profile.

This is the real authorization model of the open-source deployment, expressed
through the ``PepPort`` interface. It is *not* a no-op: the rules below are the
same tenant/role/owner/binding rules the legacy ``app.security.permissions`` and
``app.agents.branching`` modules enforce, centralized so every trusted host
calls one place.

Rules (deny by default):

- Tenant boundary: principal.tenant_id must equal resource.tenant_id.
- Tenant admin may do anything inside the tenant, except that ``is_overall``
  agents are manageable by admins only (also true for admins — that is allow).
- ``agent``: view/use when owner, admin, is_overall, or published_to_gallery;
  manage/edit/delete/share when owner or admin (never non-admin on is_overall).
- Bound resources (``skill``/``tool``/``knowledge_base``/``mcp_server``): use/view
  when an active binding to the acting agent exists and the resource is either
  private-to-agent or open-gallery; manage when admin or resource owner.
- ``sop``: view/use/execute/advance/resume follow the bound-resource rule via
  the Skill row; edit/manage need owner or admin.
- ``channel``: receive/send require an active ChannelBinding for the tenant.
- ``handoff``: create by any tenant member on their own session; manage/edit
  by assignee, agent owner, or admin.
- ``session``/``runtime``/``model_config``: same-tenant member.

Resource attributes needed by the rules are carried on ``ResourceRef.attributes``
(the projection layer fills them from ORM rows), so this class stays free of
SQLAlchemy and is unit-testable with plain dataclasses.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from staffdeck_harness.contracts.security import (
    Decision,
    IdentityPort,
    PepPort,
    ResourceRef,
    SecurityContext,
    SecurityProfile,
    WorkloadContextPort,
)

_MANAGE_ACTIONS = {"manage", "edit", "delete", "share", "create"}
_USE_ACTIONS = {"view", "use", "execute", "advance", "resume", "read", "download"}


def _attr(resource: ResourceRef, key: str, default: Any = None) -> Any:
    return resource.attributes.get(key, default)


class LocalPep(PepPort):
    profile = "OSS_LOCAL"

    def authorize(self, ctx: SecurityContext, module: str, action: str, resource: ResourceRef) -> Decision:
        if ctx.tenant_id != resource.tenant_id:
            return Decision.deny("tenant boundary", source="OSS_LOCAL")
        if _attr(resource, "status") == "deleted":
            return Decision.deny("resource deleted", source="OSS_LOCAL")

        # Service/workload principals act on behalf of the platform (scheduler,
        # channel daemons). They are same-tenant by construction and limited to
        # use-type actions; management stays with humans.
        if ctx.principal_type in {"service", "workload"}:
            if action not in _USE_ACTIONS and action not in {"receive", "send", "write"}:
                return Decision.deny("service principal cannot manage", source="OSS_LOCAL")
            # Resource state still binds the platform: a disabled binding or a
            # disabled model is unusable no matter who asks.
            if resource.type == "channel" and _attr(resource, "binding_status", "active") != "active":
                return Decision.deny("channel binding inactive", source="OSS_LOCAL")
            if resource.type == "model_config" and not bool(_attr(resource, "enabled", True)):
                return Decision.deny("model disabled", source="OSS_LOCAL")
            if _attr(resource, "enabled", True) is False:
                return Decision.deny("resource disabled", source="OSS_LOCAL")
            return Decision.allow("service principal", source="OSS_LOCAL")

        handler = getattr(self, f"_authorize_{resource.type}", None)
        if handler is None:
            return self._authorize_tenant_member(ctx, action, resource)
        return handler(ctx, action, resource)

    def filter(
        self, ctx: SecurityContext, module: str, action: str, resources: Sequence[ResourceRef]
    ) -> list[ResourceRef]:
        return [r for r in resources if self.authorize(ctx, module, action, r).allowed]

    # -- per resource type ---------------------------------------------------

    def _authorize_tenant(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            return self._admin_only(ctx, "tenant settings")
        return Decision.allow("tenant member", source="OSS_LOCAL")

    def _authorize_agent(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        owner = _attr(resource, "owner_user_id")
        is_overall = bool(_attr(resource, "is_overall", False))
        published = _attr(resource, "published_to_gallery") is True
        if action in _MANAGE_ACTIONS:
            if ctx.is_admin:
                return Decision.allow("tenant admin", source="OSS_LOCAL")
            if is_overall:
                return Decision.deny("only administrator can manage overall agent", source="OSS_LOCAL")
            if owner == ctx.principal_id:
                return Decision.allow("agent owner", source="OSS_LOCAL")
            return Decision.deny("only the creator or administrator can manage this staff", source="OSS_LOCAL")
        if ctx.is_admin or is_overall or owner == ctx.principal_id or published:
            return Decision.allow("agent visible", source="OSS_LOCAL")
        # Explicit shares (agent_shares) are projected as a set of user ids.
        shared_to = set(_attr(resource, "shared_user_ids", ()) or ())
        if ctx.principal_id in shared_to:
            return Decision.allow("agent shared", source="OSS_LOCAL")
        return Decision.deny("cannot access this staff", source="OSS_LOCAL")

    def _authorize_bound_resource(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            if ctx.is_admin:
                return Decision.allow("tenant admin", source="OSS_LOCAL")
            if _attr(resource, "owner_user_id") == ctx.principal_id:
                return Decision.allow("resource owner", source="OSS_LOCAL")
            return Decision.deny("only owner or administrator can manage this resource", source="OSS_LOCAL")
        # use/view: admin, owner, open-gallery, or bound to the acting agent.
        if ctx.is_admin:
            return Decision.allow("tenant admin", source="OSS_LOCAL")
        if _attr(resource, "owner_user_id") == ctx.principal_id:
            return Decision.allow("resource owner", source="OSS_LOCAL")
        binding_status = _attr(resource, "binding_status")
        if binding_status == "active" and (
            _attr(resource, "private_to_agent") or _attr(resource, "open_gallery")
        ):
            return Decision.allow("bound to agent", source="OSS_LOCAL")
        if _attr(resource, "open_gallery") and _attr(resource, "agent_is_overall"):
            return Decision.allow("open gallery via overall agent", source="OSS_LOCAL")
        shared_to = set(_attr(resource, "shared_user_ids", ()) or ())
        if ctx.principal_id in shared_to:
            return Decision.allow("resource shared", source="OSS_LOCAL")
        return Decision.deny("resource not bound to this staff", source="OSS_LOCAL")

    _authorize_skill = _authorize_bound_resource
    _authorize_tool = _authorize_bound_resource
    _authorize_mcp_server = _authorize_bound_resource
    _authorize_knowledge_base = _authorize_bound_resource
    _authorize_document = _authorize_bound_resource
    _authorize_sop = _authorize_bound_resource
    _authorize_capability = _authorize_bound_resource

    def _authorize_channel(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            return self._admin_only(ctx, "channel bindings")
        if _attr(resource, "binding_status", "active") != "active":
            return Decision.deny("channel binding inactive", source="OSS_LOCAL")
        return Decision.allow("channel bound", source="OSS_LOCAL")

    def _authorize_handoff(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action == "create":
            return Decision.allow("tenant member may request handoff", source="OSS_LOCAL")
        if ctx.is_admin:
            return Decision.allow("tenant admin", source="OSS_LOCAL")
        if ctx.principal_id in {_attr(resource, "assignee_user_id"), _attr(resource, "agent_owner_user_id"), _attr(resource, "requester_user_id")}:
            return Decision.allow("handoff participant", source="OSS_LOCAL")
        return Decision.deny("not a participant of this handoff", source="OSS_LOCAL")

    def _authorize_team(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            if ctx.is_admin or _attr(resource, "owner_user_id") == ctx.principal_id:
                return Decision.allow("team owner", source="OSS_LOCAL")
            return Decision.deny("only team owner or administrator", source="OSS_LOCAL")
        return Decision.allow("tenant member", source="OSS_LOCAL")

    def _authorize_model_config(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            return self._admin_only(ctx, "model configuration")
        if not bool(_attr(resource, "enabled", True)):
            return Decision.deny("model disabled", source="OSS_LOCAL")
        return Decision.allow("tenant member", source="OSS_LOCAL")

    def _authorize_session(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        owner = _attr(resource, "user_id")
        if ctx.is_admin or owner in (None, ctx.principal_id):
            return Decision.allow("session owner", source="OSS_LOCAL")
        return Decision.deny("not the session owner", source="OSS_LOCAL")

    def _authorize_runtime(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        return Decision.allow("tenant member", source="OSS_LOCAL")

    def _authorize_tenant_member(self, ctx: SecurityContext, action: str, resource: ResourceRef) -> Decision:
        if action in _MANAGE_ACTIONS:
            return self._admin_only(ctx, resource.type)
        return Decision.allow("tenant member", source="OSS_LOCAL")

    def _admin_only(self, ctx: SecurityContext, what: str) -> Decision:
        if ctx.is_admin:
            return Decision.allow("tenant admin", source="OSS_LOCAL")
        return Decision.deny(f"only administrator can manage {what}", source="OSS_LOCAL")


class LocalIdentity(IdentityPort):
    def from_user(self, user: Any, *, channel: str | None = None) -> SecurityContext:
        return SecurityContext(
            principal_id=str(user.id),
            tenant_id=str(user.tenant_id),
            principal_type="user",
            tenant_role="admin" if getattr(user, "role", "member") == "admin" else "member",
            username=getattr(user, "username", None),
            display_name=getattr(user, "display_name", None),
            provider="channel" if channel and channel != "web" else "local",
            channel=channel,
        )

    def from_service(self, service_id: str, tenant_id: str, *, workload: Any = None) -> SecurityContext:
        return SecurityContext(
            principal_id=service_id,
            tenant_id=tenant_id,
            principal_type="service",
            tenant_role="service",
            provider="local",
            workload=dict(workload or {}),
        )


class LocalWorkload(WorkloadContextPort):
    """Process-local workload context: an opaque token id scoped to the tenant."""

    def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> dict[str, Any]:
        return {
            "kind": "local",
            "audience": audience,
            "tenant_id": ctx.tenant_id,
            "principal_id": ctx.principal_id,
            "token_id": uuid.uuid4().hex,
            "ttl_seconds": int(ttl_seconds),
        }


def build_oss_local_profile() -> SecurityProfile:
    return SecurityProfile(name="OSS_LOCAL", identity=LocalIdentity(), pep=LocalPep(), workload=LocalWorkload())
