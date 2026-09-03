"""Module tests for ``channel.feishu`` (飞书接入): the legacy FeishuAdapter surfaced as an A module."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.channels.adapters import get_channel_adapter
from app.channels.adapters.feishu import FeishuAdapter, FeishuPermanentError
from app.channels.crypto import encrypt_channel_secret
from app.db.models import ChannelBinding
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.security import ResourceRef
from staffdeck_dsh.modules.registry import SEMVER_RE, ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

MODULE_ID = "channel.feishu"
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
    """Context-manager HTTP client the adapter accepts through ``client_factory``."""

    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def post(self, url, **kwargs):
        return self.handler(url, kwargs)

    def get(self, url, **kwargs):
        return self.handler(url, {**kwargs, "_method": "GET"})


def _binding(**overrides) -> ChannelBinding:
    values = dict(
        id="cb_feishu", tenant_id="t1", agent_id="a1", channel="feishu", status="active",
        config_json={"app_id": "cli_app"}, credentials_enc=encrypt_channel_secret("secret"),
        external_account_key="feishu:app:1:cli_app", config_revision=1,
    )
    values.update(overrides)
    return ChannelBinding(**values)


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_channel_feishu(registry, module):
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
    assert d["name"] == "飞书接入"
    assert d["summary"] and CJK.search(d["summary"])
    assert d["kind"] == "A" and d["slot"] == "staff.channel" and d["enabled"] is True
    assert d["guarded"] is True and bool(d["policy_actions"]) is True
    assert d["source"] == "builtin"


def test_manifest_channel_feishu_slot_guarded_by_builtin_registration(settings):
    """The builtin registrar itself marks staff.channel as PEP-bound; no fixture help needed to seal."""

    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert _described(reg)["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_channel_feishu(registry):
    big, sub, m = _placement(registry)
    assert big["id"] == "channel"
    assert sub["id"] == "channel.im"
    assert m["placement"] == {"big_id": "channel", "sub_id": "channel.im", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_channel_feishu(settings):
    settings.dsh_disabled_modules = MODULE_ID
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _described(reg)["enabled"] is False
    assert MODULE_ID not in {i.manifest.module_id for i in reg.providers(item.slot)}
    # the other adapters (and channel.host) still satisfy the channel contracts
    assert reg.for_operation("channel.receive/v1") is not None
    assert reg.for_operation("channel.send/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_channel_feishu_is_the_registered_legacy_adapter(module):
    provider = module(MODULE_ID).provider
    assert isinstance(provider, FeishuAdapter)
    assert provider is get_channel_adapter("feishu")
    for name in ("normalize", "send", "start_ingress", "stop_ingress", "add_reaction", "remove_reaction"):
        assert callable(getattr(provider, name)), name
    # reaction capability advertised for the intake/outbox gates
    assert provider.reaction_token and provider.reaction_attach_idempotent is False
    # feishu inbound frames are normalised by the event dispatcher, not the adapter
    assert provider.normalize({"any": "thing"}) is None


def test_provider_channel_feishu_send_rejects_invalid_target_offline(module):
    provider = module(MODULE_ID).provider
    with pytest.raises(FeishuPermanentError):
        provider.send(_binding(), {}, "hello", idempotency_key="k1")          # no message_id / receive_id
    with pytest.raises(FeishuPermanentError):
        provider.send(_binding(), {"message_id": "om_1"}, "   ", idempotency_key="k1")  # empty text
    with pytest.raises(FeishuPermanentError):
        provider.send(_binding(), {"message_id": "om_1"}, "hello")            # missing idempotency key


def test_provider_channel_feishu_send_success_returns_message_id():
    calls: list[tuple[str, dict]] = []

    def handler(url, kwargs):
        calls.append((url, kwargs))
        if "/auth/" in url:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tok", "expire": 7200}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_created"}}, request=httpx.Request("POST", url))

    adapter = FeishuAdapter(client_factory=lambda: FakeClient(handler))
    created = adapter.send(_binding(), {"receive_id": "ou_user", "receive_id_type": "open_id"}, "hello", idempotency_key="dlv-1")
    assert created == "om_created"
    send = next(c for c in calls if c[0].endswith("/im/v1/messages"))
    assert send[1]["params"] == {"receive_id_type": "open_id"}
    assert send[1]["headers"] == {"Authorization": "Bearer tok"}
    body = send[1]["json"]
    assert body["receive_id"] == "ou_user" and body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "hello"}
    assert len(body["uuid"]) == 40


# --------------------------------------------------------------------------- 5. PEP

@pytest.mark.parametrize("operation", ["channel.receive/v1", "channel.send/v1"])
def test_pep_channel_feishu_denies_cross_tenant(guard, security_ctx, operation):
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="channel", id="cb_x", tenant_id="t2", attributes={"binding_status": "active", "channel": "feishu"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), operation, foreign)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details["operation"] == operation and exc.value.details["profile"] == "OSS_LOCAL"
    # same-tenant active binding is allowed, inactive binding denied
    own = ResourceRef(type="channel", id="cb_1", tenant_id="t1", attributes={"binding_status": "active", "channel": "feishu"})
    assert g.require(security_ctx(), operation, own).allowed is True
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), operation, ResourceRef(type="channel", id="cb_2", tenant_id="t1", attributes={"binding_status": "disabled"}))
