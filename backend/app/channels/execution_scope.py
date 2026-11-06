"""Channel-owned scope validation for an external guest; no Base or edition branches."""
from dataclasses import asdict
import hmac
import os

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select

from app.channels.storage import channel_session
from app.db.models import ChannelBinding, ChannelBindingAgent, ChannelIdentity, ChannelInboundEvent, ChatSession, Team, TeamMember, User
from staffdeck_harness.contracts.runtime_services import ChannelExecutionScope
from staffdeck_harness.runtime.services import runtime_services


def scope_is_current(db, scope):
    if runtime_services(db).namespace != scope.namespace:
        return False
    binding = db.get(ChannelBinding, scope.binding_id)
    session = db.get(ChatSession, scope.session_id)
    event = db.get(ChannelInboundEvent, scope.inbound_event_id)
    if not binding or not session or not event:
        return False
    actor = db.get(User, scope.actor_id)
    if actor is None or actor.tenant_id != scope.tenant_id or actor.source != binding.channel:
        return False
    if (binding.tenant_id != scope.tenant_id or binding.status != "active"
            or binding.config_revision != scope.binding_revision or binding.external_account_key != scope.account_key):
        return False
    if (session.tenant_id, session.user_id, session.agent_id) != (scope.tenant_id, scope.actor_id, scope.agent_id):
        return False
    if event.tenant_id != scope.tenant_id or event.binding_id != scope.binding_id or event.status not in {"processing", "done"}:
        return False
    # Stored only by the admitted ingress path, not inferred from guest-supplied arguments.
    receipt = (event.payload_json or {}).get("_staffdeck_actor") or {}
    if receipt.get("actor_id") != scope.actor_id or receipt.get("event_id") != scope.inbound_event_id:
        return False
    root = db.get(ChatSession, receipt.get("session_id", ""))
    if root is None or (root.tenant_id, root.user_id, root.channel_binding_id, root.channel_account_key) != (
            scope.tenant_id, scope.actor_id, scope.binding_id, scope.account_key):
        return False
    if session.id != root.id and (not binding.team_id or session.team_id != binding.team_id
            or (session.context_state_json or {}).get("channel_origin_session_id") != root.id):
        return False
    identity = db.get(ChannelIdentity, receipt.get("identity_id", ""))
    if identity is None or (identity.tenant_id, identity.staffdeck_user_id, identity.channel) != (scope.tenant_id, scope.actor_id, binding.channel):
        return False
    from app.channels.service_identity import external_account_scope
    if identity.external_account_scope != external_account_scope(db, binding):
        return False
    if binding.team_id:
        team = db.get(Team, binding.team_id)
        if not team or team.tenant_id != scope.tenant_id or team.status != "active":
            return False
        return db.exec(select(TeamMember.id).where(TeamMember.team_id == team.id,
            TeamMember.agent_id == scope.agent_id)).first() is not None
    mounted = db.exec(select(ChannelBindingAgent.agent_id).where(ChannelBindingAgent.binding_id == binding.id,
        ChannelBindingAgent.tenant_id == scope.tenant_id)).all()
    return scope.agent_id in (mounted if mounted else [binding.agent_id])


def require_scope(scope):
    with channel_session() as db:
        if not scope_is_current(db, scope):
            from staffdeck_harness.contracts.errors import PermissionDenied
            raise PermissionDenied("渠道已停用、员工已解除挂载或访客会话范围已失效")


def prepare_channel_execution(db, binding, event, chat_session, user, inbound):
    from app.config import get_settings
    from staffdeck_harness.security.profile import get_profile
    from staffdeck_harness.runtime.session_binding import bind_session
    profile = get_profile(get_settings())
    prepare = getattr(profile.identity, "channel_subject", None)
    db.info["staffdeck_actor_id"] = user.id
    if not callable(prepare):
        return
    from app.channels.service_identity import external_account_scope, external_identity_for_message, find_channel_identity
    account_scope = external_account_scope(db, binding)
    external_id, _ = external_identity_for_message(binding.channel, is_group=inbound.is_group,
        conv_key=inbound.conv_key, from_user_id=inbound.from_user_id, account_scope=account_scope)
    mapping = find_channel_identity(db, binding.tenant_id, binding.channel, external_id, account_scope)
    if mapping is None or mapping.staffdeck_user_id != user.id:
        raise HTTPException(409, "渠道身份绑定已变化，请重新发送消息")
    state = dict(chat_session.context_state_json or {})
    receipt = {"actor_id": user.id, "event_id": event.id, "identity_id": mapping.id, "session_id": chat_session.id}
    state["channel_ingress_receipt"] = receipt
    event.payload_json = {**event.payload_json, "_staffdeck_actor": receipt}
    db.add(event)
    chat_session.context_state_json = state
    scope = ChannelExecutionScope(binding.tenant_id, user.id, chat_session.agent_id, chat_session.id,
        binding.id, binding.config_revision, binding.external_account_key or binding.id,
        event.id, runtime_services(db).namespace)
    subject = prepare(db, user, scope)
    if subject.channel_scope is not None:
        chat_session.context_state_json = {**chat_session.context_state_json, "channel_execution_scope": asdict(scope)}
    db.info["staffdeck_control_subject"] = subject
    bind_session(db, chat_session, created=not state.get("runtime_binding"))
    db.add(chat_session)
    db.commit()


router = APIRouter(prefix="/internal/runtime", tags=["internal:channel-scope"])


def _require_service(x_channel_scope_token):
    expected = os.environ.get("CHANNEL_SCOPE_VERIFIER_TOKEN", "")
    if not expected or not x_channel_scope_token or not hmac.compare_digest(expected, x_channel_scope_token):
        raise HTTPException(403, "Channel scope verifier requires a registered service")


@router.get("/channel-scope/health")
def scope_health(x_channel_scope_token: str | None = Header(default=None)):
    _require_service(x_channel_scope_token)
    with channel_session() as db:
        db.exec(select(ChannelBinding.id).limit(0)).all()
    return {"channel_scope": "v1", "ready": True}


@router.post("/channel-scope/verify")
def verify_scope(scope: ChannelExecutionScope, x_channel_scope_token: str | None = Header(default=None)):
    _require_service(x_channel_scope_token)
    with channel_session() as db:
        allowed = scope_is_current(db, scope)
    return {"allowed": allowed, "scope": asdict(scope)}
