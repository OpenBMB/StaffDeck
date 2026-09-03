"""Module tests: ``handoff.reply.web`` (网页回复) — WebReplyResolver."""

from __future__ import annotations

import re

import pytest

from app.db.models import HumanHandoffRequest
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.handoff.core import HandoffCore, HandoffTransitionError, ReplyResolver, WebReplyResolver
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "handoff.reply.web"
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


def _core(db, guard, resolver) -> HandoffCore:
    applied: list[tuple[str, str, str | None, str]] = []

    def fake_apply(dbx, row, text, *, answered_by_user_id, source):
        row.status = "answered"
        row.human_reply = text
        dbx.add(row)
        dbx.commit()
        applied.append((row.id, text, answered_by_user_id, source))

    core = HandoffCore(db=db, guard=guard("handoff"), assignment=None, notifiers={}, resolvers={"web": resolver}, apply_reply=fake_apply)
    core._applied = applied  # type: ignore[attr-defined]
    return core


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_reply_web(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_REPLY_ENDPOINT and m.attaches_to == (SlotName.HANDOFF_REPLY_ENDPOINT,)
    assert m.provides_operations == ("handoff.reply/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("handoff.reply/v1",)
    assert m.hooks == ()
    assert isinstance(item.provider, WebReplyResolver)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.reply_endpoint" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "网页回复"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.reply/v1"] and d["requires"] == [] and d["hooks"] == []
    assert d["policy_actions"] == ["handoff.reply/v1"]
    # declares policy actions → must be under a guarded slot (seal() enforces PepBindingMissing otherwise)
    assert d["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_reply_web(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.notification", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_reply_web():
    class Disabled(FakeSettings):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _describe(reg)["enabled"] is False
    names = {getattr(i.provider, "name", None) for i in reg.providers(SlotName.HANDOFF_REPLY_ENDPOINT)}
    assert names == {"channel_command"}
    # the operation is still provided by the remaining endpoint, so seal() stays happy
    assert reg.for_operation("handoff.reply/v1") is not None


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_reply_web_resolves_inbound_payload(module, db):
    resolver = module(MODULE_ID).provider
    assert resolver.name == "web" and resolver.slot == "handoff.reply_endpoint"
    assert isinstance(resolver, ReplyResolver)
    assert resolver.resolve(db, "t1", {"handoff_id": "h1", "reply": "  已处理，可以退款  "}) == ("h1", "已处理，可以退款")
    # both parts are mandatory; blank reply or missing id yields nothing to act on
    assert resolver.resolve(db, "t1", {"handoff_id": "h1", "reply": "   "}) is None
    assert resolver.resolve(db, "t1", {"reply": "x"}) is None
    assert resolver.resolve(db, "t1", {}) is None
    assert resolver.resolve(db, "t1", {"handoff_id": None, "reply": None}) is None


def test_provider_handoff_reply_web_drives_core_reply(module, db, guard, security_ctx, handoff):
    core = _core(db, guard, module(MODULE_ID).provider)
    # unknown resolver name → nothing happens
    assert core.reply(security_ctx(principal_id="admin", tenant_role="admin"), "sms", {"handoff_id": "h1", "reply": "x"}, source="web") is None
    # unresolvable payload → None, no PEP call
    assert core.reply(security_ctx(principal_id="admin", tenant_role="admin"), "web", {"handoff_id": "h1"}, source="web") is None
    # foreign tenant cannot see the row at all
    assert core.reply(security_ctx(principal_id="admin", tenant_id="t2", tenant_role="admin"), "web", {"handoff_id": "h1", "reply": "x"}, source="web") is None
    # non-participant member is denied by the PEP (u1 owns agent a1, so it is a participant)
    with pytest.raises(PermissionDenied):
        core.reply(security_ctx(principal_id="stranger"), "web", {"handoff_id": "h1", "reply": "x"}, source="web")
    # assignee replies → legacy apply path runs with the replier and source
    out = core.reply(security_ctx(principal_id="admin", tenant_role="admin"), "web", {"handoff_id": "h1", "reply": "已处理"}, source="web")
    assert out is not None and out.status == "answered" and out.human_reply == "已处理"
    assert core._applied == [("h1", "已处理", "admin", "web")]  # type: ignore[attr-defined]
    with pytest.raises(HandoffTransitionError):
        core.reply(security_ctx(principal_id="admin", tenant_role="admin"), "web", {"handoff_id": "h1", "reply": "again"}, source="web")


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_reply_web(module, guard, security_ctx):
    m = module(MODULE_ID).manifest
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in m.policy_actions:
        assert mapper.map(op) == ("edit", "handoff")
    g = guard(MODULE_ID)
    ref = ResourceRef(type="handoff", id="h1", tenant_id="t2", attributes={"assignee_user_id": "u1"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "handoff.reply/v1", ref)
    assert exc.value.details == {"operation": "handoff.reply/v1", "resource_type": "handoff", "resource": "h1", "profile": "OSS_LOCAL"}
    # same tenant assignee may edit
    assert g.require(security_ctx(), "handoff.reply/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1", attributes={"assignee_user_id": "u1"})).allowed
    with pytest.raises(KeyError):
        mapper.map("handoff.reply/v2")
