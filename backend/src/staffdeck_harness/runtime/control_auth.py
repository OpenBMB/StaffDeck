"""Deployment-owned control authentication, independent from replaceable runtime PEP.

Only deployment settings choose this adapter. RuntimeOverrides intentionally cannot edit it.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module


from staffdeck_harness.contracts.runtime_services import ActorIdentity as ControlSubject


@dataclass(frozen=True)
class ControlLogin:
    token: str = field(repr=False)
    subject: ControlSubject
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ControlPasswordChange:
    token: str = field(repr=False)
    user_id: str


@lru_cache(maxsize=8)
def _load(spec):
    from staffdeck_harness.modules.registry import validate_spec
    module, _, attr = validate_spec(spec).partition(":")
    provider = getattr(import_module(module), attr or "build")()
    if any(not callable(getattr(provider, name, None)) for name in ("login", "current")):
        raise RuntimeError("Control authentication adapter must implement login and current")
    return provider


def provider():
    from app.config import get_settings
    spec = str(getattr(get_settings(), "harness_control_auth_provider", "") or "")
    return _load(spec) if spec else None


def project_subject(db, subject):
    """Only a verified public identity is stored. No Base password or token is persisted."""
    from fastapi import HTTPException
    from app.db.models import Tenant, User
    if not isinstance(subject, ControlSubject) or subject.role not in {"admin", "member"} or not all(
        (subject.user_id, subject.tenant_id, subject.username, subject.provider)
    ):
        raise HTTPException(401, "Invalid control identity")
    user = db.get(User, subject.user_id)
    if user and (user.tenant_id != subject.tenant_id or user.source != subject.provider):
        raise HTTPException(409, "Control identity conflicts with existing local account")
    if user is None and db.get(Tenant, subject.tenant_id) is None:
        db.add(Tenant(id=subject.tenant_id, name="Harness v3 测试租户"))
    changed = user is None or (user.username, user.display_name, user.role) != (subject.username, subject.display_name, subject.role)
    if user is None:
        user = User(id=subject.user_id, tenant_id=subject.tenant_id, username=subject.username,
                    password_hash="external-authentication-only", source=subject.provider)
    user.username, user.display_name, user.role = subject.username, subject.display_name, subject.role
    if changed:
        db.add(user)
        db.commit()
        db.refresh(user)
    db.info["staffdeck_control_subject"] = subject
    return user
