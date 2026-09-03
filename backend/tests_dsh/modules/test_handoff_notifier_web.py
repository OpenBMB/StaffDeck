"""Module tests: ``handoff.notifier.web`` (站内通知) — the web inbox Notifier."""

from __future__ import annotations

import re

import pytest
from sqlmodel import select

from app.db.models import AgentEvent, HumanHandoffRequest
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.contracts.security import ResourceRef
from staffdeck_dsh.handoff.core import HandoffCore, Notifier, WebInboxNotifier
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "handoff.notifier.web"
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


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_notifier_web(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_NOTIFIER and m.attaches_to == (SlotName.HANDOFF_NOTIFIER,)
    assert m.provides_operations == ("handoff.request/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ()
    assert m.hooks == ()
    assert isinstance(item.provider, WebInboxNotifier)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.notifier" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "站内通知"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.request/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    # a module that declares policy actions must sit under a guarded slot; this one declares none
    # but its host (HandoffCore) is still PEP-bound, so the slot is guarded either way.
    assert d["guarded"] is True
    assert d["guarded"] or not d["policy_actions"]


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_notifier_web(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.notification", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_notifier_web():
    class Disabled(FakeSettings):
        dsh_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _describe(reg)["enabled"] is False
    names = {getattr(i.provider, "name", None) for i in reg.providers(SlotName.HANDOFF_NOTIFIER)}
    assert "web" not in names and {"feishu", "wecom"} <= names   # dingtalk/wechat are registered but disabled (no proactive DM support)


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_notifier_web_records_inbox_event(module, db, handoff):
    notifier = module(MODULE_ID).provider
    assert notifier.name == "web"
    assert notifier.slot == "handoff.notifier"
    assert isinstance(notifier, Notifier)

    mid = notifier.notify(db, handoff, pending_question="需要人工确认", context_summary="用户申请退款")
    db.commit()
    # the web inbox has no external message id
    assert mid is None
    events = db.exec(select(AgentEvent).where(AgentEvent.session_id == "s1", AgentEvent.event_type == "human_handoff_notified")).all()
    assert len(events) == 1
    assert events[0].tenant_id == "t1"
    assert events[0].payload_json == {"handoff_id": "h1", "assignee_user_id": "admin", "channel": "web"}


def test_provider_handoff_notifier_web_is_the_core_fallback(module, db, guard, handoff):
    """HandoffCore always notifies the web inbox, even when a channel is preferred; no message id is written."""

    notifier = module(MODULE_ID).provider

    class Boom:
        name = "feishu"

        def notify(self, *a, **kw):
            raise RuntimeError("channel down")

    handoff.metadata_json = {"assignee_notify_channel": "feishu"}
    core = HandoffCore(db=db, guard=guard("handoff"), assignment=None, notifiers={"web": notifier, "feishu": Boom()}, resolvers={})
    core.notify(handoff, pending_question="q", context_summary="c")
    kinds = [e.event_type for e in db.exec(select(AgentEvent).where(AgentEvent.session_id == "s1")).all()]
    assert "human_handoff_notify_failed" in kinds and "human_handoff_notified" in kinds
    assert db.get(HumanHandoffRequest, "h1").notify_message_id is None


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_notifier_web(module, guard, security_ctx):
    """The notifier is not itself guarded (no policy_actions); the operation it provides is, via the Core."""

    assert module(MODULE_ID).manifest.policy_actions == ()
    g = guard(MODULE_ID)
    ref = ResourceRef(type="handoff", id="new", tenant_id="t2", attributes={"requester_user_id": "u1"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "handoff.request/v1", ref)
    assert exc.value.details["operation"] == "handoff.request/v1"
    # same tenant: any member may request a handoff
    assert g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t1")).allowed
