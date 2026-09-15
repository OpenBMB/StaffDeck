"""One source/identity/PEP boundary for staff references outside the AgentLoop."""
from fastapi import HTTPException
from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.sources import SourceContext
from staffdeck_harness.contracts.staff import StaffProfile
from staffdeck_harness.contracts.security import PolicyActionMapper
from staffdeck_harness.composition.sources import resolve_source


def directory_context(db, tenant_id, staff_id, *, user=None):
    info = getattr(db, "info", {})
    subject = info.get("staffdeck_control_subject")
    execution = info.get("staffdeck_execution")
    actor = user.id if user is not None else subject.user_id if subject else info.get("staffdeck_actor_id")
    # Resolving another member is not an execution by the current member.
    if execution is not None and execution.staff_id != staff_id:
        execution = None
    return SourceContext(tenant_id, staff_id, user_id=actor, subject=subject, execution=execution)


def staff_profile(db, tenant_id, staff_id, *, user=None, action=None, active_only=False):
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.security.profile import get_profile, Guard
    context = directory_context(db, tenant_id, staff_id, user=user)
    registry = getattr(db, "info", {}).get("staffdeck_registry") or peek_registry()
    try:
        if registry is None:
            from staffdeck_harness.composition.local_sources import LocalStaffSource
            source = LocalStaffSource(db)
        else:
            source = resolve_source(registry, SlotName.STAFF_SOURCE, db)
        authorized_ref = None
        if action:
            from app.config import get_settings
            profile = getattr(registry, "security_profile", None) or get_profile(get_settings())
            if registry:
                identity = resolve_source(registry, SlotName.IDENTITY_SOURCE, db).resolve(context, profile.identity)
            else:
                from staffdeck_harness.composition.local_sources import LocalIdentitySource
                identity = LocalIdentitySource(db).resolve(context, profile.identity)
            actor = identity.actor_user_id if identity.principal_type == "workload" else identity.principal_id
            if identity.tenant_id != tenant_id or not context.user_id or actor != context.user_id:
                raise PermissionDenied("员工访问身份不匹配")
            authorized_ref = source.reference(context)
            if (authorized_ref.id, authorized_ref.tenant_id, authorized_ref.type) != (staff_id, tenant_id, "agent"):
                raise PermissionDenied("员工来源返回了不匹配的引用")
            Guard("staff", profile, PolicyActionMapper({"directory/v1": (action, "agent")})).require(identity, "directory/v1", authorized_ref)
        lookup = getattr(source, "profile", None)
        if callable(lookup):
            row = lookup(context)
        else:
            # Third-party v1 sources still resolve their own data, never local shadow rows.
            value = source.resolve(context)
            row = StaffProfile(value.staff_id, value.tenant_id, value.name, value.status, value.ref,
                value.is_overall, None, value.persona, value.session_policy.max_actions, dict(value.metadata))
        if not isinstance(row, StaffProfile) or (row.id, row.tenant_id, row.ref.id, row.ref.tenant_id, row.ref.type) != (
                staff_id, tenant_id, staff_id, tenant_id, "agent"):
            raise PermissionDenied("员工来源返回了不匹配的引用")
        if active_only and row.status != "active":
            raise HTTPException(409, "员工已停用")
        return row
    except ModuleSdkError as exc:
        status = 404 if exc.code == "STAFF_NOT_FOUND" else 403 if isinstance(exc, PermissionDenied) else 503
        raise HTTPException(status, exc.message) from exc


def ensure_staff_manager(db, tenant_id, staff_id, current_user):
    if current_user.tenant_id != tenant_id:
        raise HTTPException(403, "Tenant mismatch")
    return staff_profile(db, tenant_id, staff_id, user=current_user, action="manage", active_only=True)


def staff_names(db, tenant_id, staff_ids):
    names = {}
    for staff_id in dict.fromkeys(staff_ids):
        try:
            names[staff_id] = staff_profile(db, tenant_id, staff_id).name
        except HTTPException as exc:
            if exc.status_code not in (403, 404, 409):
                raise
    return names


def optional_staff_profile(db, tenant_id, staff_id):
    try:
        return staff_profile(db, tenant_id, staff_id)
    except HTTPException as exc:
        if exc.status_code in (403, 404, 409):
            return None
        raise
