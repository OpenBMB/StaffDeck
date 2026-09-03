"""Module tests: ``handoff.reply.channel_command`` (渠道内回复) — ChannelCommandReplyResolver."""

from __future__ import annotations

import re

import pytest

from app.db.models import HumanHandoffRequest
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.handoff.core import ChannelCommandReplyResolver, HandoffCore, ReplyResolver
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "handoff.reply.channel_command"
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
    row = HumanHandoffRequest(id="h1", tenant_id="t1", session_id="s1", agent_id="a1", requester_user_id="u1", assignee_user_id="admin", pending_question="需要人工确认", context_summary="用户申请退款", status="assigned", notify_message_id="om_notice_1")
    db.add(row)
    # a same-id notice in another tenant must never be correlated
    db.add(HumanHandoffRequest(id="h2", tenant_id="t2", session_id="s2", status="assigned", notify_message_id="om_notice_2"))
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_reply_channel_command(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_REPLY_ENDPOINT and m.attaches_to == (SlotName.HANDOFF_REPLY_ENDPOINT,)
    assert m.provides_operations == ("handoff.reply/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("handoff.reply/v1",)
    assert m.hooks == ()
    assert isinstance(item.provider, ChannelCommandReplyResolver)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.reply_endpoint" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "渠道内回复"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.reply/v1"] and d["requires"] == [] and d["hooks"] == []
    assert d["policy_actions"] == ["handoff.reply/v1"]
    assert d["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_reply_channel_command(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.notification", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_reply_channel_command():
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
    assert names == {"web"}


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_reply_channel_command_quoted_reply(module, db, handoff):
    resolver = module(MODULE_ID).provider
    assert resolver.name == "channel_command" and resolver.slot == "handoff.reply_endpoint"
    assert isinstance(resolver, ReplyResolver)
    # quoting the notice message correlates through notify_message_id
    assert resolver.resolve(db, "t1", {"quoted_message_id": "om_notice_1", "text": " 已处理，可以退款 "}) == ("h1", "已处理，可以退款")
    # quoted notice of another tenant is invisible
    assert resolver.resolve(db, "t1", {"quoted_message_id": "om_notice_2", "text": "x"}) is None
    # unknown quote and no command → nothing
    assert resolver.resolve(db, "t1", {"quoted_message_id": "om_unknown", "text": "x"}) is None
    # quoted but empty text → nothing
    assert resolver.resolve(db, "t1", {"quoted_message_id": "om_notice_1", "text": "  "}) is None


def test_provider_handoff_reply_channel_command_slash_command(module, db, handoff):
    resolver = module(MODULE_ID).provider
    assert resolver.resolve(db, "t1", {"text": "/回复反馈 h1 已处理，可以退款"}) == ("h1", "已处理，可以退款")
    assert resolver.resolve(db, "t1", {"text": "  /回复反馈 h1   多个空格  "}) == ("h1", "多个空格")
    # english alias understood by the legacy command parser
    assert resolver.resolve(db, "t1", {"text": "/handoff_reply h1 done"}) == ("h1", "done")
    # id without body, body without id, other commands, plain chat → None
    assert resolver.resolve(db, "t1", {"text": "/回复反馈 h1"}) is None
    assert resolver.resolve(db, "t1", {"text": "/回复反馈"}) is None
    assert resolver.resolve(db, "t1", {"text": "/员工"}) is None
    assert resolver.resolve(db, "t1", {"text": "你好"}) is None
    assert resolver.resolve(db, "t1", {}) is None
    # the resolver only parses; it does not verify the id exists (the Core does)
    assert resolver.resolve(db, "t1", {"text": "/回复反馈 nope hi"}) == ("nope", "hi")


def test_provider_handoff_reply_channel_command_drives_core_reply(module, db, guard, security_ctx, handoff):
    applied: list[tuple[str, str, str | None, str]] = []

    def fake_apply(dbx, row, text, *, answered_by_user_id, source):
        row.status = "answered"
        row.human_reply = text
        dbx.add(row)
        dbx.commit()
        applied.append((row.id, text, answered_by_user_id, source))

    core = HandoffCore(db=db, guard=guard("handoff"), assignment=None, notifiers={}, resolvers={"channel_command": module(MODULE_ID).provider}, apply_reply=fake_apply)
    admin = security_ctx(principal_id="admin", tenant_role="admin", channel="feishu", provider="channel")
    # unknown handoff id parsed from the command → None (row missing)
    assert core.reply(admin, "channel_command", {"text": "/回复反馈 nope hi"}, source="feishu") is None
    with pytest.raises(PermissionDenied):
        core.reply(security_ctx(principal_id="stranger"), "channel_command", {"quoted_message_id": "om_notice_1", "text": "x"}, source="feishu")
    out = core.reply(admin, "channel_command", {"quoted_message_id": "om_notice_1", "text": "已处理"}, source="feishu")
    assert out is not None and out.id == "h1" and out.status == "answered"
    assert applied == [("h1", "已处理", "admin", "feishu")]


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_reply_channel_command(module, guard, security_ctx):
    m = module(MODULE_ID).manifest
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in m.policy_actions:
        assert mapper.map(op) == ("edit", "handoff")
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "handoff.reply/v1", ResourceRef(type="handoff", id="h1", tenant_id="t2", attributes={"assignee_user_id": "u1"}))
    assert exc.value.details["profile"] == "OSS_LOCAL" and exc.value.details["operation"] == "handoff.reply/v1"
    # channel principals are still users: assignee allowed, stranger denied
    ctx = security_ctx(provider="channel", channel="feishu")
    assert g.require(ctx, "handoff.reply/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1", attributes={"assignee_user_id": "u1"})).allowed
    with pytest.raises(PermissionDenied):
        g.require(ctx, "handoff.reply/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1", attributes={"assignee_user_id": "someone"}))
