"""Module tests for ``channel.wechat_kf`` (微信客服接入): the legacy WeChatKfAdapter surfaced as an A module."""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import httpx
import pytest

import app.channels.adapters.wechat_kf as wechat_kf_module
from app.channels.adapters import ChannelInbound, get_channel_adapter
from app.channels.adapters.wechat_kf import WeChatKfAdapter, WeChatKfPermanentError, WeChatKfTransientError
from app.db.models import ChannelBinding
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.security import ResourceRef
from staffdeck_dsh.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

MODULE_ID = "channel.wechat_kf"
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


def _raw(**overrides) -> dict:
    value = {"msgid": "kf-msg-1", "open_kfid": "wk1234567890", "external_userid": "external-1", "origin": 3, "msgtype": "text", "text": {"content": "你好"}}
    value.update(overrides)
    return value


def _binding() -> ChannelBinding:
    return ChannelBinding(id="cb_kf", tenant_id="t1", agent_id="a1", channel="wechat_kf", status="active", config_json={"corp_id": "corp"}, external_account_key="wechat_kf:corp:wk1234567890", config_revision=1)


class FakeTokens:
    def get(self, binding):
        return "kf-token"

    def invalidate(self, binding):
        return None


class FakeClient:
    """Stand-in for ``httpx.Client(timeout=...)`` inside the adapter's ``_post``."""

    calls: list[tuple[str, dict]] = []
    payload: dict = {"errcode": 0, "msgid": "kf-out"}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def post(self, url, **kwargs):
        FakeClient.calls.append((url, kwargs))
        return httpx.Response(200, json=dict(FakeClient.payload), request=httpx.Request("POST", url))


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.payload = {"errcode": 0, "msgid": "kf-out"}
    monkeypatch.setattr(wechat_kf_module, "httpx", SimpleNamespace(Client=FakeClient, HTTPError=httpx.HTTPError))
    return FakeClient


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_wechat_kf(registry, module):
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
    assert d["name"] == "微信客服接入"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_wechat_kf(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.im"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.im", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_wechat_kf(settings):
    settings.dsh_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    assert reg.for_operation("channel.receive/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_wechat_kf_is_the_registered_legacy_adapter(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, WeChatKfAdapter)
    assert provider is get_channel_adapter("wechat_kf")
    for name in ("normalize", "send", "start_ingress", "stop_ingress", "download_media"):
        assert callable(getattr(provider, name)), name


def test_provider_channel_wechat_kf_normalize_offline(module):
    provider = module(MODULE_ID).provider
    inbound = provider.normalize(_raw())
    assert isinstance(inbound, ChannelInbound)
    assert inbound.channel == "wechat_kf"
    assert inbound.event_id == "kf-msg-1" and inbound.from_user_id == "external-1" and inbound.to_user_id == "wk1234567890"
    assert inbound.text == "你好" and inbound.is_group is False and inbound.group_id == ""
    assert inbound.context_token == "wk1234567890" and inbound.session_id == "external-1"
    assert inbound.external_conv_id == "wechat_kf_p2p_external-1"
    # image message carries an attachment and no text
    image = provider.normalize(_raw(msgtype="image", text=None, image={"media_id": "media-1"}))
    assert image is not None and image.text == "" and image.attachments[0].media_id == "media-1" and image.attachments[0].kind == "image"
    # discarded: non-customer origin, missing ids, empty text, malformed text payload, non-dict
    assert provider.normalize(_raw(origin=4)) is None
    assert provider.normalize(_raw(origin="abc")) is None
    assert provider.normalize(_raw(external_userid="")) is None
    assert provider.normalize(_raw(text={"content": ""})) is None
    assert provider.normalize(_raw(text="plain")) is None
    assert provider.normalize("nope") is None


def test_provider_channel_wechat_kf_send_rejects_invalid_target_offline(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(WeChatKfPermanentError):
        provider.send(_binding(), {"to_user_id": "external-1"}, "hello", idempotency_key="k1")   # missing open_kfid
    with pytest.raises(WeChatKfPermanentError):
        provider.send(_binding(), {"open_kfid": "wk1"}, "hello", idempotency_key="k1")            # missing to_user_id


def test_provider_channel_wechat_kf_send_success_and_token_expiry(fake_http):
    adapter = WeChatKfAdapter(token_provider=FakeTokens())
    adapter.send(_binding(), {"to_user_id": "external-1", "open_kfid": "wk1"}, "hello", idempotency_key="dlv-1")
    assert len(fake_http.calls) == 1
    url, kwargs = fake_http.calls[0]
    assert url.endswith("/kf/send_msg")
    body = kwargs["json"]
    assert body["touser"] == "external-1" and body["open_kfid"] == "wk1" and body["msgtype"] == "text"
    assert body["text"] == {"content": "hello"}
    assert body["msgid"] == hashlib.sha256(b"dlv-1").hexdigest()[:32]
    # an expired token is reported as transient so the outbox retries
    fake_http.payload = {"errcode": 42001, "errmsg": "access_token expired"}
    with pytest.raises(WeChatKfTransientError):
        adapter.send(_binding(), {"to_user_id": "external-1", "open_kfid": "wk1"}, "hello", idempotency_key="dlv-2")
    fake_http.payload = {"errcode": 40003, "errmsg": "invalid openid"}
    with pytest.raises(WeChatKfPermanentError):
        adapter.send(_binding(), {"to_user_id": "external-1", "open_kfid": "wk1"}, "hello", idempotency_key="dlv-3")


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_wechat_kf_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "wechat_kf"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED" and exc.value.details["operation"] == operation
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "wechat_kf"})
    assert g.require(security_ctx(), operation, own).allowed is True
