"""Module tests for ``channel.wecom`` (企业微信接入): the legacy WeComAdapter surfaced as an A module."""

from __future__ import annotations

import re

import pytest

from app.channels.adapters import ChannelInbound, get_channel_adapter
from app.channels.adapters.wecom import WeComAdapter
from app.db.models import ChannelBinding
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "channel.wecom"
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


def _frame(**overrides) -> dict:
    frame = {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "req_1"},
        "body": {"msgid": "msg_1", "aibotid": "aib_bot1", "chattype": "single", "from": {"userid": "zhangsan", "name": "张三"}, "msgtype": "text", "text": {"content": "你好"}},
    }
    for key, value in overrides.items():
        if key == "headers":
            frame["headers"] = value
        else:
            frame["body"][key] = value
    return frame


def _binding() -> ChannelBinding:
    return ChannelBinding(id="cb_wecom", tenant_id="t1", agent_id="a1", channel="wecom", status="active", config_json={"corp_id": "corp", "bot_id": "bot"}, external_account_key="wecom:corp:bot", config_revision=1)


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_wecom(registry, module):
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
    assert d["name"] == "企业微信接入"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_wecom(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.im"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.im", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_wecom(settings):
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    assert reg.for_operation("channel.send/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_wecom_is_the_registered_legacy_adapter(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, WeComAdapter)
    assert provider is get_channel_adapter("wecom")
    for name in ("normalize", "send", "start_ingress", "stop_ingress", "download_media"):
        assert callable(getattr(provider, name)), name


def test_provider_channel_wecom_normalize_offline(module):
    provider = module(MODULE_ID).provider
    inbound = provider.normalize(_frame())
    assert isinstance(inbound, ChannelInbound)
    assert inbound.channel == "wecom"
    assert inbound.event_id == "msg_1" and inbound.from_user_id == "zhangsan" and inbound.to_user_id == "aib_bot1"
    assert inbound.text == "你好" and inbound.is_group is False and inbound.sender_name == "张三"
    assert inbound.context_token == "zhangsan" and inbound.external_conv_id == "wecom_p2p_zhangsan"
    # voice frames carry the transcript
    voice = provider.normalize(_frame(msgtype="voice", text=None, voice={"content": "我下午三点到"}))
    assert voice is not None and voice.text == "我下午三点到"
    # group frame with a leading bot mention
    group = provider.normalize(_frame(chattype="group", chatid="grp_1", text={"content": "@bot 你好"}))
    assert group is not None and group.is_group is True and group.group_id == "grp_1" and group.text == "你好"
    assert group.external_conv_id == "wecom_group_grp_1"
    # discarded: the bot's own echo, empty text, missing sender, non-dict frame
    assert provider.normalize(_frame(**{"from": {"userid": "aib_bot1"}})) is None
    assert provider.normalize(_frame(text={"content": "   "})) is None
    assert provider.normalize(_frame(**{"from": {}})) is None
    assert provider.normalize(None) is None


def test_provider_channel_wecom_send_rejects_missing_target_offline(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(ValueError):
        provider.send(_binding(), {}, "hello", idempotency_key="k1")


def test_provider_channel_wecom_send_requires_live_stream(module, monkeypatch):
    """Without a connected WS stream the adapter fails loudly instead of dropping the reply."""

    import app.channels as channels_pkg

    class _NoStreams:
        def get_stream(self, binding_id):
            return None

    monkeypatch.setattr(channels_pkg, "get_wecom_stream_manager", lambda: _NoStreams())
    provider = module(MODULE_ID).provider
    with pytest.raises(RuntimeError, match="企微连接未就绪"):
        provider.send(_binding(), {"to_user_id": "zhangsan"}, "hello", idempotency_key="k1")


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_wecom_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "wecom"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == operation
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "wecom"})
    assert g.require(security_ctx(), operation, own).allowed is True
