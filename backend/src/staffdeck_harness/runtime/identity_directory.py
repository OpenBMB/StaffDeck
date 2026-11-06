"""Resolve member facts from their authority, never from an optional login projection."""
from fastapi import HTTPException
from sqlmodel import select
from staffdeck_harness.contracts.members import MemberRecord


def resolve_members(db, tenant_id, user_ids):
    from app.db.models import User
    from staffdeck_harness.runtime.control_auth import provider
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    control = provider()
    if control is None:
        rows = db.exec(select(User).where(User.tenant_id == tenant_id, User.id.in_(ids))).all()
        return {row.id: MemberRecord(id=row.id, tenant_id=row.tenant_id, username=row.username,
                    display_name=row.display_name or row.username, source=row.source, role=row.role)
                for row in rows if row.source == 'web'}
    reader = getattr(control, 'resolve_members', None)
    if not callable(reader):
        raise HTTPException(503, {'code': 'MEMBER_DIRECTORY_UNAVAILABLE', 'message': '当前身份来源未提供成员解析接口'})
    source = getattr(control, 'member_identity_source', None)
    if not isinstance(source, str) or not source:
        raise HTTPException(503, {'code': 'MEMBER_DIRECTORY_INVALID', 'message': '成员目录未声明身份域'})
    try:
        rows = [MemberRecord.model_validate(row) for row in reader(tenant_id, ids)]
        if any(row.tenant_id != tenant_id or row.id not in ids or not row.username
               or not row.source
               or row.source != source
               or row.role not in {'admin', 'member'} for row in rows) or len({r.id for r in rows}) != len(rows):
            raise ValueError('Invalid member directory response')
    except (ValueError, TypeError):
        raise HTTPException(503, {'code': 'MEMBER_DIRECTORY_INVALID', 'message': '成员目录返回的身份或数据归属不符合契约'}) from None
    return {row.id: row for row in rows}


def require_internal_member(db, tenant_id, user_id, *, materialize=False, resolved=None):
    member = (resolve_members(db, tenant_id, [user_id]) if resolved is None else resolved).get(user_id)
    if member is None:
        raise HTTPException(404, {'code': 'MEMBER_NOT_FOUND', 'message': '成员不存在于当前租户的身份目录'})
    if member.disabled:
        raise HTTPException(409, {'code': 'MEMBER_DISABLED', 'message': '该成员已停用，不能绑定或分配任务'})
    if not materialize:
        return member
    # Public identity projection only: not a new login, password or permission grant.
    from app.db.models import User
    user = db.get(User, member.id)
    if user and (user.tenant_id, user.source) != (member.tenant_id, member.source):
        raise HTTPException(409, {'code': 'MEMBER_IDENTITY_CONFLICT', 'message': '成员标识与已有身份域冲突'})
    if user is None:
        user = User(id=member.id, tenant_id=member.tenant_id, username=member.username,
                    source=member.source, password_hash='external-authentication-only')
    user.username, user.display_name, user.role = member.username, member.display_name, member.role
    db.add(user)
    from sqlalchemy.exc import IntegrityError
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, {'code': 'MEMBER_IDENTITY_CONFLICT', 'message': '成员投影与已有账号标识冲突，请检查身份映射'}) from None
    return user


def user_names(db, tenant_id, user_ids):
    """Names are display facts only; missing names never authorize an operation."""
    from app.db.models import User
    from staffdeck_harness.runtime.control_auth import provider
    control = provider()
    source = getattr(control, 'member_identity_source', None) if control else 'web'
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    cache = db.info.setdefault('staffdeck_display_names', {})
    scope = (tenant_id, db.info.get('staffdeck_namespace'))
    uncached = [key for key in ids if (scope, key) not in cache]
    if not uncached:
        return {key: cache[scope, key] for key in ids if cache[scope, key] is not None}
    # Channel guests are owned by this runtime, not the enterprise member directory.
    local = db.exec(select(User).where(User.tenant_id == tenant_id, User.id.in_(uncached))).all()
    names = {row.id: row.display_name or row.username for row in local if row.source != source}
    try:
        rows = resolve_members(db, tenant_id, list(set(uncached) - names.keys()))
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        import logging
        logging.getLogger(__name__).warning('Member display directory unavailable; names omitted')
        rows = {}  # Display-only degradation. require_internal_member never uses this cache.
    names.update({key: row.display_name or row.username for key, row in rows.items()})
    cache.update({(scope, key): names.get(key) for key in uncached})
    return {key: cache[scope, key] for key in ids if cache[scope, key] is not None}


def is_internal_actor(user):
    if user is None:
        return False
    from staffdeck_harness.runtime.control_auth import provider
    control = provider()
    source = getattr(control, 'member_identity_source', None) if control else None
    if source:
        # A projected member remains a member when the runtime PEP is replaced.
        # This classification is not an active-account authorization check.
        return getattr(user, 'source', None) == source
    from app.config import get_settings
    from staffdeck_harness.security.profile import get_profile
    identity = get_profile(get_settings()).identity
    accept = getattr(identity, "is_internal_user", None)
    return bool(accept(user)) if callable(accept) else getattr(user, "source", None) == "web"
