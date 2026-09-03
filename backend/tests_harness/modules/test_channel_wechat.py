"""Module tests for ``channel.wechat`` (微信接入): the legacy WeChatAdapter surfaced as an A module."""

from __future__ import annotations

import re

import pytest

from app.channels.adapters import ChannelInbound, get_channel_adapter
from app.channels.adapters.wechat import WeChatAdapter
from app.db.models import ChannelBinding
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

MODULE_ID = "channel.wechat"
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


def _msg(**overrides) -> dict:
    msg = {
        "seq": 429, "message_id": 9812451782375, "from_user_id": "user_ab12cd34@im.wechat", "to_user_id": "bot_1@im.bot",
        "client_id": "wx-msg-1", "session_id": "user_ab12cd34@im.wechat#bot_1@im.bot", "message_type": 1, "message_state": 2,
        "context_token": "ctx_token_1", "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
    }
    msg.update(overrides)
    return msg


def _binding() -> ChannelBinding:
    return ChannelBinding(id="cb_wechat", tenant_id="t1", agent_id="a1", channel="wechat", status="active", config_json={"ilink_bot_id": "bot_1@im.bot"}, external_account_key="wechat:bot_1", config_revision=1)


class FakeWeChatClient:
    def __init__(self):
        self.sent: list[tuple[str, str, str, str]] = []

    def send_message(self, to_user_id, context_token, text, client_id=""):
        self.sent.append((to_user_id, context_token, text, client_id))


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_wechat(registry, module):
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
    assert d["name"] == "微信接入"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_wechat(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.im"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.im", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_wechat(settings):
    settings.harness_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    assert reg.for_operation("channel.receive/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_wechat_is_the_registered_legacy_adapter(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, WeChatAdapter)
    assert provider is get_channel_adapter("wechat")
    for name in ("normalize", "send", "send_typing", "start_ingress", "stop_ingress", "download_media"):
        assert callable(getattr(provider, name)), name


def test_provider_channel_wechat_normalize_offline(module):
    provider = module(MODULE_ID).provider
    inbound = provider.normalize(_msg())
    assert isinstance(inbound, ChannelInbound)
    assert inbound.channel == "wechat"
    assert inbound.event_id == "9812451782375"
    assert inbound.from_user_id == "user_ab12cd34@im.wechat" and inbound.to_user_id == "bot_1@im.bot"
    assert inbound.text == "你好" and inbound.context_token == "ctx_token_1" and inbound.is_group is False
    assert inbound.external_conv_id == "wechat_p2p_user_ab12cd34@im.wechat"
    # image item without text still yields an inbound carrying the attachment
    image = provider.normalize(_msg(item_list=[{"type": 2, "image_item": {"media_id": "image-1"}}]))
    assert image is not None and image.text == "" and image.attachments[0].media_id == "image-1"
    assert image.attachments[0].download_params["context_token"] == "ctx_token_1"
    # group message: group_id set
    group = provider.normalize(_msg(group_id="grp_1", session_id="grp_1"))
    assert group is not None and group.is_group is True and group.external_conv_id == "wechat_group_grp_1"
    # discarded: bot's own message (message_type 2), missing context token, no text/attachment, non-dict
    assert provider.normalize(_msg(message_type=2)) is None
    assert provider.normalize(_msg(context_token="")) is None
    assert provider.normalize(_msg(item_list=[])) is None
    assert provider.normalize([]) is None


def test_provider_channel_wechat_send_rejects_missing_target_offline(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(ValueError):
        provider.send(_binding(), {"to_user_id": "user_1"}, "hello", idempotency_key="k1")   # no context_token
    with pytest.raises(ValueError):
        provider.send(_binding(), {"context_token": "ctx"}, "hello", idempotency_key="k1")   # no to_user_id


def test_provider_channel_wechat_send_success_uses_idempotent_client_ids():
    client = FakeWeChatClient()
    adapter = WeChatAdapter(client_factory=lambda binding: client)
    adapter.send(_binding(), {"to_user_id": "user_1", "context_token": "ctx_1"}, "hello", idempotency_key="dlv-1")
    assert client.sent == [("user_1", "ctx_1", "hello", "staffdeck:dlv-1:0")]
    # long text is chunked; each chunk gets a stable per-index client id
    adapter.send(_binding(), {"to_user_id": "user_1", "context_token": "ctx_1"}, "x" * 4100, idempotency_key="dlv-2")
    chunks = client.sent[1:]
    assert len(chunks) >= 2 and [c[3] for c in chunks] == [f"staffdeck:dlv-2:{i}" for i in range(len(chunks))]
    assert "".join(c[2] for c in chunks) == "x" * 4100


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_wechat_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "wechat"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == operation
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "wechat"})
    assert g.require(security_ctx(), operation, own).allowed is True
