"""Revalidate an admitted actor before deferred work; credentials never enter job payloads."""
def bind_actor(db, actor):
    from staffdeck_harness.runtime.control_auth import provider, project_subject
    from staffdeck_harness.runtime.services import bind_authenticated_session
    control = provider()
    if control is not None:
        actor = project_subject(db, control.credential_subject(actor))
    elif db.info.get("staffdeck_runtime_services") is not None:
        db.info["staffdeck_actor_id"] = actor.id
        return actor
    db.info.pop("staffdeck_execution", None)
    db.info.pop("staffdeck_runtime_services", None)
    db.info["staffdeck_actor_id"] = actor.id
    bind_authenticated_session(db)
    return actor


def capture_actor(db, *, legacy_owner=None):
    from staffdeck_harness.runtime.control_auth import provider
    from staffdeck_harness.runtime.session_binding import current_binding
    from staffdeck_harness.contracts.errors import PermissionDenied
    subject = db.info.get("staffdeck_control_subject")
    execution = db.info.get("staffdeck_execution")
    actor_id = subject.user_id if subject else execution.actor_user_id if execution else db.info.get("staffdeck_actor_id")
    if not actor_id and provider() is None:
        actor_id = legacy_owner  # Existing unassembled/local callers name their explicit team owner.
    if not actor_id:
        raise PermissionDenied("后台任务缺少可信发起人")
    from dataclasses import asdict
    scope = subject.channel_scope if subject else execution.channel_scope if execution else None
    return {"actor_id": actor_id, "binding": current_binding(db),
            **({"channel_scope": asdict(scope)} if scope else {})}


def restore_actor(db, tenant_id, captured):
    from app.db.models import User
    from staffdeck_harness.runtime.session_binding import current_binding
    from staffdeck_harness.contracts.errors import PermissionDenied
    if not captured or not captured.get("actor_id"):
        raise PermissionDenied("后台任务缺少已登记的身份")
    if captured.get("binding") != current_binding(db):
        raise PermissionDenied("后台任务属于其他装配，不能跨数据域继续执行")
    actor = db.get(User, captured["actor_id"])
    if actor is None or actor.tenant_id != tenant_id:
        raise PermissionDenied("后台任务发起人已不可用")
    if captured.get("channel_scope") is not None:
        from staffdeck_harness.contracts.runtime_services import ChannelExecutionScope, ActorIdentity
        from app.channels.execution_scope import require_scope
        scope = ChannelExecutionScope(**captured["channel_scope"])
        if (scope.tenant_id, scope.actor_id) != (tenant_id, actor.id):
            raise PermissionDenied("后台渠道身份范围不匹配")
        require_scope(scope)
        db.info["staffdeck_actor_id"] = actor.id
        db.info["staffdeck_control_subject"] = ActorIdentity(actor.id, tenant_id, actor.username,
            actor.display_name or actor.username, "member", "channel_guest", scope)
        return actor
    return bind_actor(db, actor)
