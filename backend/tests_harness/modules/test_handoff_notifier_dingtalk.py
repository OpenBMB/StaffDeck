"""Module tests: ``handoff.notifier.dingtalk`` (钉钉通知) — ChannelNotifier("dingtalk")."""

from __future__ import annotations

import re

import pytest
from sqlmodel import select

from app.db.models import AgentEvent, ChannelBinding, ChannelDelivery, ChannelIdentity, HumanHandoffRequest
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.handoff.core import ChannelNotifier, HandoffCore, Notifier
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "handoff.notifier.dingtalk"
CHANNEL = "dingtalk"
CJK = re.compile(r"[一-鿿]")
SEMVER = re.compile(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$")


def _describe(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


def _placed(registry, module_id: str = MODULE_ID) -> dict:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return m
    raise AssertionError(f"{module_id} not placed in the taxonomy tree")


@pytest.fixture
def handoff(db) -> HumanHandoffRequest:
    row = HumanHandoffRequest(id="h1", tenant_id="t1", session_id="s1", agent_id="a1", requester_user_id="u1", assignee_user_id="admin", pending_question="需要人工确认", context_summary="用户申请退款", status="assigned")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def dingtalk_binding(db) -> ChannelBinding:
    binding = ChannelBinding(id="bind-dingtalk", tenant_id="t1", agent_id="a1", channel=CHANNEL, status="active", config_json={"provider_tenant_key": "corp-ding"})
    db.add(binding)
    # dingtalk identities are scoped by provider_tenant_key (scope_from_config)
    db.add(ChannelIdentity(tenant_id="t1", channel=CHANNEL, external_account_scope="corp-ding", external_user_id="ding_admin", staffdeck_user_id="admin"))
    db.commit()
    return binding


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_notifier_dingtalk(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_NOTIFIER and m.attaches_to == (SlotName.HANDOFF_NOTIFIER,)
    assert m.provides_operations == ("handoff.request/v1",)
    assert m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, ChannelNotifier)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.notifier" and d["enabled"] is False and d["source"] == "builtin"   # outbox cannot DM on this channel yet
    assert d["name"] == "钉钉通知"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.request/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["guarded"] is True
    assert d["guarded"] or not d["policy_actions"]


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_notifier_dingtalk(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.notification", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_notifier_dingtalk():
    class Disabled(FakeSettings):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _describe(reg)["enabled"] is False
    names = {getattr(i.provider, "name", None) for i in reg.providers(SlotName.HANDOFF_NOTIFIER)}
    assert CHANNEL not in names and "web" in names


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_notifier_dingtalk_without_binding_is_noop(module, db, handoff):
    notifier = module(MODULE_ID).provider
    assert notifier.name == CHANNEL and notifier.slot == "handoff.notifier"
    assert isinstance(notifier, Notifier)
    assert notifier.notify(db, handoff, pending_question="q", context_summary="c") is None
    assert db.exec(select(ChannelDelivery)).all() == []


def test_provider_handoff_notifier_dingtalk_never_breaks_the_core(module, db, guard, handoff, dingtalk_binding):
    """Contract: a notifier may skip, but must not raise into HandoffCore.notify."""

    notifier = module(MODULE_ID).provider
    handoff.metadata_json = {"assignee_notify_channel": CHANNEL}
    core = HandoffCore(db=db, guard=guard("handoff"), assignment=None, notifiers={CHANNEL: notifier}, resolvers={})
    core.notify(handoff, pending_question="q", context_summary="c")
    assert db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_notify_failed")).all() == []
    assert db.get(HumanHandoffRequest, "h1").notify_message_id is None


@pytest.mark.xfail(reason="钉钉暂不支持主动私聊通知（service_outbox.HANDOFF_NOTIFY_CHANNELS）；模块已注册但默认停用，manifest 如实说明", strict=True)
def test_provider_handoff_notifier_dingtalk_enqueues_private_notice(module, db, handoff, dingtalk_binding):
    notifier = module(MODULE_ID).provider
    notifier.notify(db, handoff, pending_question="需要人工确认", context_summary="用户申请退款")
    rows = db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")).all()
    assert len(rows) == 1 and rows[0].binding_id == dingtalk_binding.id


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_notifier_dingtalk(module, guard, security_ctx):
    assert module(MODULE_ID).manifest.policy_actions == ()
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t2"))
    assert g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t1")).allowed
