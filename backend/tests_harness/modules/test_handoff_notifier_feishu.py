"""Module tests: ``handoff.notifier.feishu`` (飞书通知) — ChannelNotifier("feishu")."""

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

MODULE_ID = "handoff.notifier.feishu"
CHANNEL = "feishu"
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


def _bind(db, *, tenant_id: str = "t1", status: str = "active", team_id: str | None = None, identity: bool = True) -> ChannelBinding:
    binding = ChannelBinding(id=f"bind-{CHANNEL}-{tenant_id}-{status}-{team_id or 'staff'}", tenant_id=tenant_id, agent_id="a1", channel=CHANNEL, status=status, team_id=team_id, config_json={"app_id": "cli_x"})
    db.add(binding)
    exists = db.exec(select(ChannelIdentity).where(ChannelIdentity.tenant_id == tenant_id, ChannelIdentity.channel == CHANNEL)).first()
    if identity and exists is None:
        # feishu identities live in the empty scope (see scope_from_config)
        db.add(ChannelIdentity(tenant_id=tenant_id, channel=CHANNEL, external_account_scope="", external_user_id="ou_admin", staffdeck_user_id="admin"))
    db.commit()
    return binding


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_notifier_feishu(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_NOTIFIER and m.attaches_to == (SlotName.HANDOFF_NOTIFIER,)
    assert m.provides_operations == ("handoff.request/v1",)
    assert m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, ChannelNotifier)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.notifier" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "飞书通知"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.request/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["guarded"] is True
    assert d["guarded"] or not d["policy_actions"]


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_notifier_feishu(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.notification", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_notifier_feishu():
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

def test_provider_handoff_notifier_feishu_without_binding_is_noop(module, db, handoff):
    notifier = module(MODULE_ID).provider
    assert notifier.name == CHANNEL and notifier.slot == "handoff.notifier"
    assert isinstance(notifier, Notifier)
    assert notifier.notify(db, handoff, pending_question="q", context_summary="c") is None
    assert db.exec(select(ChannelDelivery)).all() == []


def test_provider_handoff_notifier_feishu_ignores_inactive_foreign_and_team_bindings(module, db, handoff):
    notifier = module(MODULE_ID).provider
    _bind(db, status="disabled")
    _bind(db, tenant_id="t2")
    _bind(db, team_id="team1")
    assert notifier.notify(db, handoff, pending_question="q", context_summary="c") is None
    assert db.exec(select(ChannelDelivery)).all() == []


def test_provider_handoff_notifier_feishu_enqueues_private_notice(module, db, handoff):
    notifier = module(MODULE_ID).provider
    binding = _bind(db)
    mid = notifier.notify(db, handoff, pending_question="需要人工确认", context_summary="用户申请退款")
    # the message id is written back by the outbox worker after delivery, so nothing yet
    assert mid is None
    rows = db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")).all()
    assert len(rows) == 1
    d = rows[0]
    assert d.tenant_id == "t1" and d.binding_id == binding.id and d.session_id == "handoff:h1" and d.status == "pending"
    assert d.target_json == {"receive_id_type": "open_id", "receive_id": "ou_admin", "handoff_id": "h1"}
    assert "【人工介入转接】" in d.text and "需要人工确认" in d.text and "用户申请退款" in d.text
    # feishu replies correlate through the quoted message, so the notice explains that
    assert "引用" in d.text
    # duplicate notify while the first notice is still pending is suppressed
    assert notifier.notify(db, handoff, pending_question="需要人工确认", context_summary="用户申请退款") is None
    assert len(db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")).all()) == 1


def test_provider_handoff_notifier_feishu_skips_assignee_without_identity(module, db, handoff):
    notifier = module(MODULE_ID).provider
    _bind(db, identity=False)
    assert notifier.notify(db, handoff, pending_question="q", context_summary="c") is None
    assert db.exec(select(ChannelDelivery)).all() == []


def test_provider_handoff_notifier_feishu_is_used_when_preferred_by_core(module, db, guard, handoff):
    notifier = module(MODULE_ID).provider
    _bind(db)
    handoff.metadata_json = {"assignee_notify_channel": CHANNEL}
    core = HandoffCore(db=db, guard=guard("handoff"), assignment=None, notifiers={CHANNEL: notifier}, resolvers={})
    core.notify(handoff, pending_question="q", context_summary="c")
    assert len(db.exec(select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_notice")).all()) == 1
    assert [e.event_type for e in db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_notify_failed")).all()] == []


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_notifier_feishu(module, guard, security_ctx):
    assert module(MODULE_ID).manifest.policy_actions == ()
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t2"))
    assert g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t1")).allowed
