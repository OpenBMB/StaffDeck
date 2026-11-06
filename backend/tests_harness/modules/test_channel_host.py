"""Module tests for ``channel.host`` (消息收发中心): the trusted Receive/Send PEP around the durable inbox/outbox."""

from __future__ import annotations

import re

import pytest
from sqlmodel import select

from app.channels.adapters import ChannelInbound, get_channel_adapter
from app.db.models import AgentProfile, ChannelBinding, ChannelDelivery, ChatSession, Message, User
from staffdeck_harness.channels.host import ChannelHost, ReceiveDecision
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules.kernel import ChannelHostModule
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "channel.host"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


def _placement(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed in taxonomy tree")


def _inbound() -> ChannelInbound:
    return ChannelInbound(channel="feishu", event_id="e1", from_user_id="ou_1", to_user_id="bot", session_id="p2p_1", group_id="", context_token="", text="hi", is_group=False, raw={})


@pytest.fixture
def channel_db(db):
    """Extend the shared DB with an active feishu binding (cb_active), a disabled one (cb_off) and a private staff binding (cb_private)."""

    db.add(AgentProfile(id="a_private", tenant_id="t1", name="Private", status="active", metadata_json={"owner_user_id": "someone"}))
    db.add(ChannelBinding(id="cb_active", tenant_id="t1", agent_id="a1", channel="feishu", status="active", external_account_key="feishu:app:1:cli"))
    db.add(ChannelBinding(id="cb_off", tenant_id="t1", agent_id="a1", channel="feishu", status="disabled", external_account_key="feishu:app:2:cli"))
    db.add(ChannelBinding(id="cb_private", tenant_id="t1", agent_id="a_private", channel="feishu", status="active", external_account_key="feishu:app:3:cli"))
    db.commit()
    return db


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_host(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "T"
    assert item.slot.value == "staff.channel"
    assert m.attaches_to == (item.slot,)
    assert set(m.provides_operations) == {"channel.receive/v1", "channel.send/v1"}
    assert m.requires_operations == ()
    assert set(m.policy_actions) == {"channel.receive/v1", "channel.send/v1"}
    assert m.hooks == ()
    assert SEMVER_RE.match(m.version)
    assert not m.metadata.get("switchable")
    d = _described(registry)
    assert d["name"] == "消息收发中心"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "T" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["switchable"] is False
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_host(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.message"
    # explicit taxonomy entry, not the staff.channel slot default (channel.im)
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.message", "source": "taxonomy"}
    assert m["switchable"] is False
    assert m["movable"] is True  # T modules outside engine/pep slots may be re-parented for display
    assert sub["kind"] == "T" and [x["module_id"] for x in sub["modules"]] == [MODULE_ID]


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_host_is_refused_by_admin_validation(registry, settings):
    """T module without ``switchable`` metadata: the admin API rejects it ("是平台核心组成部分，不能停用")."""

    d = _described(registry)
    assert d["kind"] == "T" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}  # refused on the switchable rule, not the engine/pep rule
    # Deployment discovery also refuses to disable protected platform infrastructure.
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert reg.get(MODULE_ID).enabled is True and _described(reg)["switchable"] is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_host_build_returns_channel_host(module, db, profile):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, ChannelHostModule)
    assert provider.module_id == MODULE_ID
    host = provider.build(db, profile)
    assert isinstance(host, ChannelHost)
    assert host.db is db and host.profile is profile
    assert host.guard.module_id == "channel" and host.guard.name == "OSS_LOCAL"
    # adapter lookups delegate to the legacy adapter registry (the channel.* modules)
    assert ChannelHost.adapter("feishu") is get_channel_adapter("feishu")
    with pytest.raises(ValueError):
        ChannelHost.adapter("no_such_channel")


def test_provider_channel_host_requires_profile(module, db):
    from staffdeck_harness.contracts.errors import PepBindingMissing

    with pytest.raises(PepBindingMissing):
        module(MODULE_ID).provider.build(db, None)


def test_provider_channel_host_receive_pep(module, channel_db, profile):
    host = module(MODULE_ID).provider.build(channel_db, profile)
    alice = channel_db.get(User, "u1")
    # unmapped channel identity is refused, never auto-created
    d = host.authorize_receive(channel_db.get(ChannelBinding, "cb_active"), _inbound(), None)
    assert isinstance(d, ReceiveDecision) and d.allowed is False and "unmapped" in d.reason and d.security_context is None
    # inactive binding → channel.receive denied
    d = host.authorize_receive(channel_db.get(ChannelBinding, "cb_off"), _inbound(), alice)
    assert d.allowed is False and "inactive" in d.reason
    # binding to a staff the user may not use → staff.use denied
    d = host.authorize_receive(channel_db.get(ChannelBinding, "cb_private"), _inbound(), alice)
    assert d.allowed is False and "staff" in d.reason
    # happy path: mapped user, active binding, visible staff → context carries the channel
    d = host.authorize_receive(channel_db.get(ChannelBinding, "cb_active"), _inbound(), alice)
    assert d.allowed is True and d.reason == "allowed"
    assert d.security_context.principal_id == "u1" and d.security_context.tenant_id == "t1"
    assert d.security_context.channel == "feishu" and d.security_context.provider == "channel"
    # cross-tenant user is stopped at the tenant boundary
    channel_db.add(User(id="u_other", tenant_id="t2", username="bob", role="admin", password_hash="x"))
    channel_db.commit()
    d = host.authorize_receive(channel_db.get(ChannelBinding, "cb_active"), _inbound(), channel_db.get(User, "u_other"))
    assert d.allowed is False and "tenant boundary" in d.reason


def test_provider_channel_host_send_pep_and_outbox(module, channel_db, profile):
    host = module(MODULE_ID).provider.build(channel_db, profile)
    session = ChatSession(id="s_ch", tenant_id="t1", user_id="u1", agent_id="a1", channel="feishu", channel_binding_id="cb_active", channel_account_key="feishu:app:1:cli", channel_target_json={"receive_id": "ou_1", "receive_id_type": "open_id"}, status="active")
    channel_db.add(session)
    msg = Message(id="msg_reply", tenant_id="t1", session_id="s_ch", role="assistant", content="reviewed reply")
    channel_db.add(msg)
    channel_db.commit()
    # raw (unsupervised) text never reaches the outbox
    with pytest.raises(PermissionDenied):
        host.stage_send(session, msg, supervised=False)
    assert channel_db.exec(select(ChannelDelivery)).all() == []
    # web sessions (no binding) are a no-op
    web = ChatSession(id="s_web", tenant_id="t1", user_id="u1", agent_id="a1", status="active")
    host.stage_send(web, Message(tenant_id="t1", session_id="s_web", role="assistant", content="x"))
    assert channel_db.exec(select(ChannelDelivery)).all() == []
    # inactive binding → send PEP denies before staging
    off = ChatSession(id="s_off", tenant_id="t1", user_id="u1", agent_id="a1", channel="feishu", channel_binding_id="cb_off", channel_account_key="feishu:app:2:cli", status="active")
    with pytest.raises(PermissionDenied):
        host.stage_send(off, Message(tenant_id="t1", session_id="s_off", role="assistant", content="x"))
    assert channel_db.exec(select(ChannelDelivery)).all() == []
    # supervised text on an active binding is staged into the legacy outbox
    host.stage_send(session, msg, supervised=True)
    channel_db.commit()
    deliveries = channel_db.exec(select(ChannelDelivery)).all()
    assert len(deliveries) == 1
    dlv = deliveries[0]
    assert dlv.binding_id == "cb_active" and dlv.session_id == "s_ch" and dlv.message_id == "msg_reply"
    assert dlv.status == "pending" and dlv.kind == "reply" and dlv.text == "reviewed reply"
    assert dlv.target_json == {"receive_id": "ou_1", "receive_id_type": "open_id"}
    # staging is idempotent per message
    host.stage_send(session, msg, supervised=True)
    channel_db.commit()
    assert len(channel_db.exec(select(ChannelDelivery)).all()) == 1


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_host_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "feishu"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == operation
    # the send side runs as the platform service principal; still fenced by tenant and binding state
    svc = security_ctx(principal_id="staffdeck.runtime", principal_type="service", tenant_role="service")
    with pytest.raises(PermissionDenied):
        g.require(svc, operation, foreign)
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "feishu"})
    assert g.require(svc, operation, own).allowed is True
    with pytest.raises(PermissionDenied):
        g.require(svc, operation, ResourceRef(type="channel", id="cb_2", tenant_id="t1", attributes={"binding_status": "expired"}))
