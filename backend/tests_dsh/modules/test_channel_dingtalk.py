"""Module tests for ``channel.dingtalk`` (钉钉接入): the legacy DingTalkAdapter surfaced as an A module."""

from __future__ import annotations

import re

import httpx
import pytest

from app.channels.adapters import ChannelInbound, get_channel_adapter
from app.channels.adapters.dingtalk import DingTalkAdapter, DingTalkPermanentError, DingTalkTransientError
from app.channels.crypto import encrypt_channel_secret
from app.db.models import ChannelBinding
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.security import ResourceRef
from staffdeck_dsh.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

MODULE_ID = "channel.dingtalk"
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


class FakeClient:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def post(self, url, **kwargs):
        return self.handler(url, kwargs)


def _binding(**overrides) -> ChannelBinding:
    values = dict(
        id="cb_dingtalk", tenant_id="t1", agent_id="a1", channel="dingtalk", status="active",
        config_json={"client_id": "client-1"}, credentials_enc=encrypt_channel_secret("secret"),
        external_account_key="dingtalk:client-1", config_revision=1,
    )
    values.update(overrides)
    return ChannelBinding(**values)


def _raw(**overrides) -> dict:
    value = {
        "msgtype": "text", "msgId": "msg-1", "conversationId": "conv-1", "conversationType": "1", "isInAtList": True,
        "senderStaffId": "staff-1", "senderNick": "Alice", "chatbotUserId": "robot-1", "chatbotCorpId": "corp-1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x", "text": {"content": "hello"},
    }
    value.update(overrides)
    return value


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_dingtalk(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind.value == "A"
    assert item.slot.value == "staff.channel"
    assert m.attaches_to == (item.slot,)
    assert set(m.provides_operations) == {"channel.receive/v1", "channel.send/v1"}
    assert m.requires_operations == ()
    assert set(m.policy_actions) == {"channel.receive/v1", "channel.send/v1"}
    assert m.hooks == ()
    assert SEMVER_RE.match(m.version)
    d = _described(registry)
    assert d["name"] == "钉钉接入"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_dingtalk(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.im"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.im", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_dingtalk(settings):
    settings.dsh_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    assert reg.for_operation("channel.receive/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_dingtalk_is_the_registered_legacy_adapter(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, DingTalkAdapter)
    assert provider is get_channel_adapter("dingtalk")
    for name in ("normalize", "send", "start_ingress", "stop_ingress", "add_reaction", "remove_reaction"):
        assert callable(getattr(provider, name)), name
    assert provider.reaction_token and provider.reaction_attach_idempotent is True


def test_provider_channel_dingtalk_normalize_offline(module):
    provider = module(MODULE_ID).provider
    inbound = provider.normalize(_raw())
    assert isinstance(inbound, ChannelInbound)
    assert inbound.channel == "dingtalk"
    assert inbound.event_id == "msg-1" and inbound.from_user_id == "staff-1" and inbound.to_user_id == "robot-1"
    assert inbound.text == "hello" and inbound.is_group is False and inbound.sender_name == "Alice"
    assert inbound.context_token.startswith("https://oapi.dingtalk.com/")
    assert inbound.external_conv_id == "dingtalk_p2p_staff-1"
    # filters: bot's own message, group without @-mention, missing webhook, unsupported msgtype
    assert provider.normalize(_raw(senderStaffId="robot-1")) is None
    assert provider.normalize(_raw(conversationType="2", isInAtList=False)) is None
    assert provider.normalize(_raw(sessionWebhook="")) is None
    assert provider.normalize(_raw(msgtype="video")) is None
    assert provider.normalize("not-a-dict") is None
    grouped = provider.normalize(_raw(conversationType="2", text={"content": " @robot-1 hello "}))
    assert grouped is not None and grouped.is_group is True and grouped.text == "hello" and grouped.group_id == "conv-1"


def test_provider_channel_dingtalk_send_rejects_invalid_target_offline(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(DingTalkPermanentError):
        provider.send(_binding(), {}, "hello", idempotency_key="k1")
    with pytest.raises(DingTalkPermanentError):
        provider.send(_binding(), {"session_webhook": "https://attacker.example/steal"}, "hello", idempotency_key="k1")
    with pytest.raises(DingTalkPermanentError):
        provider.send(_binding(), {"session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x"}, "   ", idempotency_key="k1")
    with pytest.raises(DingTalkPermanentError):
        provider.send(_binding(), {"session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x", "session_webhook_expired_time": 1}, "hello")


def test_provider_channel_dingtalk_send_success_and_transient_failure():
    calls: list[tuple[str, dict]] = []
    status = {"code": 200}

    def handler(url, kwargs):
        calls.append((url, kwargs))
        return httpx.Response(status["code"], json={"errcode": 0}, request=httpx.Request("POST", url))

    adapter = DingTalkAdapter(client_factory=lambda: FakeClient(handler))
    webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=x"
    assert adapter.send(_binding(), {"session_webhook": webhook}, "hello", idempotency_key="dlv-1") is None
    assert len(calls) == 1 and calls[0][0] == webhook
    assert calls[0][1]["json"] == {"msgtype": "text", "text": {"content": "hello"}}
    status["code"] = 503
    with pytest.raises(DingTalkTransientError):
        adapter.send(_binding(), {"context_token": webhook}, "again", idempotency_key="dlv-2")


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_dingtalk_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "dingtalk"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == operation
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "dingtalk"})
    assert g.require(security_ctx(), operation, own).allowed is True
