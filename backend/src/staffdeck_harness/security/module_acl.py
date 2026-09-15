"""ACL of StaffDeck-owned collaboration objects, independent of identity vendor.

AI collaboration teams are not Base organization teams. Identity/role must already
be verified by the installed PEP; each member's execution still requires Staff-use.
"""
from staffdeck_harness.contracts.security import Decision


def authorize_team(ctx, action, resource):
    if ctx.tenant_id != resource.tenant_id:
        return Decision.deny("tenant boundary", source="MODULE_ACL")
    if resource.attributes.get("status") == "deleted":
        return Decision.deny("team deleted", source="MODULE_ACL")
    if action in {"manage", "edit", "delete", "share", "create"}:
        if ctx.is_admin or resource.attributes.get("owner_user_id") == ctx.principal_id:
            return Decision.allow("team owner", source="MODULE_ACL")
        return Decision.deny("only team owner or administrator", source="MODULE_ACL")
    if action in {"view", "read", "use", "execute", "delegate"}:
        return Decision.allow("tenant member", source="MODULE_ACL")
    return Decision.deny("unsupported team operation", source="MODULE_ACL")
