"""Handoff ownership facts from the selected Staff source and trusted user projections.

This is a query adapter, not a second assignment policy or Handoff state machine.
An unavailable source is not evidence that an employee has no owner.
"""
from typing import Any

from sqlmodel import select

from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.staff import StaffProfile

OWNER_KEYS = ("owner_user_id", "created_by_user_id", "creator_user_id", "created_by", "owner_id")


def selected_registry(db):
    from staffdeck_harness.modules.registry import peek_registry

    return getattr(db, "info", {}).get("staffdeck_registry") or peek_registry()


def handoff_staff(db, tenant_id, staff_id, *, legacy_profile=None) -> StaffProfile | Any | None:
    if not staff_id:
        return None
    registry = selected_registry(db)
    if registry is None and legacy_profile is not None:
        if (legacy_profile.tenant_id, legacy_profile.id) != (tenant_id, staff_id):
            raise PermissionDenied("handoff employee identity mismatch")
        return legacy_profile
    from fastapi import HTTPException
    from staffdeck_harness.runtime.staff_directory import staff_profile

    try:
        return staff_profile(db, tenant_id, staff_id)
    except HTTPException as exc:
        if registry is None and exc.status_code == 404:
            return None  # Unassembled callers retain the original missing-employee behavior.
        raise ModuleSdkError("无法确认转交员工及负责人，请恢复员工来源后重试",
                             code="HANDOFF_STAFF_UNAVAILABLE") from exc


def owner_candidates(staff, *, require_complete=True) -> tuple[str, ...]:
    if staff is None:
        return ()
    attributes = dict(getattr(getattr(staff, "ref", None), "attributes", {}) or {})
    metadata = dict(getattr(staff, "metadata_json", {}) or {})
    if require_complete and metadata.get("ownership_resolved") is False:
        raise ModuleSdkError("员工来源尚未提供完整负责人事实，请升级来源接口或明确指定处理人",
                             code="HANDOFF_OWNER_FACTS_UNAVAILABLE")
    return tuple(dict.fromkeys(str(value).strip() for key in OWNER_KEYS
        for value in (attributes.get(key, metadata.get(key)),) if value and str(value).strip()))


def internal_user(db, tenant_id, user_id, *, profile=None):
    """Do not treat channel guests or users of a different identity realm as handlers."""
    from app.db.models import User
    from app.config import get_settings
    from staffdeck_harness.security.profile import get_profile

    user = db.get(User, user_id) if user_id else None
    if user is None or user.tenant_id != tenant_id:
        return None
    registry = selected_registry(db)
    profile = profile or getattr(registry, "security_profile", None) or get_profile(get_settings())
    accept = getattr(profile.identity, "is_internal_user", None)
    accepted = accept(user) if callable(accept) else getattr(user, "source", None) == "web"
    return user if accepted else None


def staff_owner_id(db, tenant_id, staff, *, profile=None):
    candidates = owner_candidates(staff)
    for candidate in candidates:
        if internal_user(db, tenant_id, candidate, profile=profile):
            return candidate
    if candidates and selected_registry(db) is not None:
        # A known remote owner may not have been projected locally yet. Do not
        # silently disclose their pending question to the first local admin.
        raise ModuleSdkError("员工负责人尚无有效的内部身份映射，请明确指定处理人或同步身份",
                             code="HANDOFF_OWNER_UNAVAILABLE")
    return None


def tenant_admin_id(db, tenant_id, *, profile=None):
    from app.db.models import User

    for user in db.exec(select(User).where(User.tenant_id == tenant_id, User.role == "admin")
                        .order_by(User.created_at)).all():
        if internal_user(db, tenant_id, user.id, profile=profile):
            return user.id
    return None


def handoff_context(db, session, profile):
    """Resolve the current admitted actor, never turn a raw session owner into a service."""
    from staffdeck_harness.composition.sources import resolve_source
    from staffdeck_harness.contracts.manifest import SlotName
    from staffdeck_harness.contracts.sources import SourceContext

    registry = selected_registry(db)
    if registry is None:
        user = internal_user(db, session.tenant_id, session.user_id, profile=profile)
        if user is None:
            raise PermissionDenied("handoff requester identity unavailable")
        return profile.identity.from_user(user, channel=session.channel)
    context = SourceContext(session.tenant_id, session.agent_id, session.id,
                            session.channel or "web", session.user_id)
    identity = resolve_source(registry, SlotName.IDENTITY_SOURCE, db).resolve(context, profile.identity)
    actor = identity.actor_user_id or identity.principal_id
    if (identity.tenant_id, actor) != (session.tenant_id, session.user_id):
        raise PermissionDenied("handoff requester identity mismatch")
    return identity
