"""Module tests: ``handoff.core`` (转人工处理流程) — the trusted HandoffCore state machine."""

from __future__ import annotations

import re

import pytest
from sqlmodel import select

from app.db.models import AgentEvent, ChatSession, HumanHandoffRequest
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.handoff.core import (
    TRANSITIONS,
    AssignmentStrategy,
    ChannelCommandReplyResolver,
    ChannelNotifier,
    DefaultAssignment,
    HandoffCore,
    HandoffTransitionError,
    WebInboxNotifier,
    WebReplyResolver,
)
from staffdeck_harness.modules.kernel import HandoffCoreModule
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, reset_registry
from staffdeck_harness.modules.taxonomy import FIXED_SLOTS, tree

from .conftest import FakeSettings

MODULE_ID = "handoff.core"
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
def session(db) -> ChatSession:
    s = ChatSession(id="s1", tenant_id="t1", user_id="u1", agent_id="a1", status="active")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _built(module, db, guard) -> HandoffCore:
    core = module(MODULE_ID).provider.build(db, guard("handoff"))
    applied: list[tuple[str, str, str | None, str]] = []

    def fake_apply(dbx, row, text, *, answered_by_user_id, source):
        row.status = "answered"
        row.human_reply = text
        dbx.add(row)
        dbx.commit()
        applied.append((row.id, text, answered_by_user_id, source))

    core.apply_reply = fake_apply
    core._applied = applied  # type: ignore[attr-defined]
    return core


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_core(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED
    assert item.slot is SlotName.HANDOFF_ASSIGNMENT and m.attaches_to == (SlotName.HANDOFF_ASSIGNMENT,)
    assert m.provides_operations == ("handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1")
    assert m.requires_operations == ()
    assert m.policy_actions == ("handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1")
    assert m.hooks == ()
    assert isinstance(item.provider, HandoffCoreModule)

    d = _describe(registry)
    assert d["kind"] == "T" and d["slot"] == "handoff.assignment" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "转人工处理流程"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"] and d["requires"] == [] and d["hooks"] == []
    assert d["policy_actions"] == ["handoff.request/v1", "handoff.assign/v1", "handoff.reply/v1"]
    # declares policy actions → must sit under a guarded slot
    assert d["guarded"] is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_core(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.resume", "source": "taxonomy"}
    assert m["switchable"] is False
    # T modules outside engine/PEP slots may be re-parented for display, never swapped
    assert m["movable"] is True and m["slot"] not in FIXED_SLOTS


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_core(registry):
    """Not switchable: the admin API refuses to stop it (``是平台核心组成部分，不能停用``)."""

    d = _describe(registry)
    assert d["kind"] == "T" and d["switchable"] is False
    assert d["slot"] not in {"runtime.engine", "security.pep"}
    # Direct configuration cannot silently disable the shared platform state machine.
    class Disabled(FakeSettings):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    assert reg.get(MODULE_ID) is not None and reg.get(MODULE_ID).enabled is True
    assert _describe(reg)["switchable"] is False


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_core_build_assembles_from_registry(module, db, guard):
    core = module(MODULE_ID).provider.build(db, guard("handoff"))
    assert isinstance(core, HandoffCore)
    assert core.db is db and core.guard.module_id == "handoff" and core.guard.name == "OSS_LOCAL"
    assert isinstance(core.assignment, DefaultAssignment)
    assert set(core.notifiers) == {"web", "feishu", "dingtalk", "wecom", "wechat"}
    assert isinstance(core.notifiers["web"], WebInboxNotifier)
    assert all(isinstance(core.notifiers[ch], ChannelNotifier) and core.notifiers[ch].name == ch for ch in ("feishu", "dingtalk", "wecom", "wechat"))
    assert set(core.resolvers) == {"web", "channel_command"}
    assert isinstance(core.resolvers["web"], WebReplyResolver) and isinstance(core.resolvers["channel_command"], ChannelCommandReplyResolver)
    assert core.apply_reply is None


def test_provider_handoff_core_state_machine_happy_path(module, db, guard, security_ctx, session):
    core = _built(module, db, guard)
    alice = security_ctx()
    row = core.create(alice, session, pending_question="需要人工确认退款", context_summary="用户申请退款", trigger_skill_id="sk1", trigger_step_id="st1", metadata={"step": {"name": "审批"}})
    assert row.status == "assigned" and row.assignee_user_id == "u1"  # a1 is owned by u1
    assert row.trigger_skill_id == "sk1" and row.trigger_step_id == "st1" and row.metadata_json["step"] == {"name": "审批"}
    assert db.get(ChatSession, "s1").status == "handoff"
    assert db.get(ChatSession, "s1").awaiting_input_json == {"type": "human_handoff", "handoff_id": row.id, "pending_question": "需要人工确认退款"}
    kinds = [e.event_type for e in db.exec(select(AgentEvent).where(AgentEvent.session_id == "s1")).all()]
    assert kinds == ["human_handoff_created", "human_handoff_assigned", "human_handoff_notified"]
    # idempotent while open, merging metadata
    again = core.create(alice, session, pending_question="q2", context_summary="c", metadata={"extra": 1})
    assert again.id == row.id and again.metadata_json["extra"] == 1 and again.metadata_json["step"] == {"name": "审批"}
    # reply by the assignee (owner) → answered via the injected legacy apply path
    out = core.reply(security_ctx(principal_id="u1"), "web", {"handoff_id": row.id, "reply": "已处理"}, source="web")
    assert out is not None and out.status == "answered"
    assert core._applied == [(row.id, "已处理", "u1", "web")]  # type: ignore[attr-defined]
    core.mark_resumed(row)
    assert row.status == "resumed"
    core.close(security_ctx(principal_id="admin", tenant_role="admin"), row, reason="done")
    assert row.status == "closed"
    assert "human_handoff_closed" in [e.event_type for e in db.exec(select(AgentEvent).where(AgentEvent.session_id == "s1")).all()]
    with pytest.raises(HandoffTransitionError):
        core.mark_resumed(row)


def test_provider_handoff_core_cancel_and_transition_rules(module, db, guard, security_ctx, session):
    core = _built(module, db, guard)
    row = core.create(security_ctx(), session, pending_question="q", context_summary="c", notify_channel="feishu")
    assert row.metadata_json["assignee_notify_channel"] == "feishu"
    # stranger cannot cancel
    with pytest.raises(PermissionDenied):
        core.cancel(security_ctx(principal_id="stranger"), row)
    core.cancel(security_ctx(principal_id="admin", tenant_role="admin"), row, reason="user left")
    assert row.status == "cancelled"
    s = db.get(ChatSession, "s1")
    assert s.status == "active" and s.awaiting_input_json is None
    # terminal: nothing else is allowed
    for to in ("assigned", "answered", "resumed", "closed", "cancelled"):
        with pytest.raises(HandoffTransitionError):
            core._transition(row, to)
    with pytest.raises(HandoffTransitionError):
        core.reply(security_ctx(principal_id="u1"), "web", {"handoff_id": row.id, "reply": "late"}, source="web")
    # after cancellation a new handoff can be opened for the same session
    row2 = core.create(security_ctx(), s, pending_question="q2", context_summary="c")
    assert row2.id != row.id and row2.status == "assigned"
    # the table is the documented graph
    assert TRANSITIONS["pending"] == {"assigned", "answered", "cancelled"}
    assert TRANSITIONS["answered"] == {"resumed", "cancelled", "closed"}
    assert TRANSITIONS["closed"] == set() and TRANSITIONS["cancelled"] == set()


def test_provider_handoff_core_create_runs_pep(module, db, guard, security_ctx, session):
    core = _built(module, db, guard)
    with pytest.raises(PermissionDenied):
        core.create(security_ctx(tenant_id="t2"), session, pending_question="q", context_summary="c")
    assert db.exec(select(HumanHandoffRequest)).all() == []
    assert db.get(ChatSession, "s1").status == "active"


def test_provider_handoff_core_build_falls_back_when_default_assignment_disabled(module, db, guard, monkeypatch):
    monkeypatch.setenv("STAFFDECK_HARNESS_DISABLED_MODULES", "handoff.assignment.default")
    reset_registry()
    core = module(MODULE_ID).provider.build(db, guard("handoff"))
    assert isinstance(core.assignment, AssignmentStrategy)
    assert callable(getattr(core.assignment, "propose", None))


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_core(module, guard, security_ctx):
    m = module(MODULE_ID).manifest
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    assert [mapper.map(op) for op in m.policy_actions] == [("create", "handoff"), ("manage", "handoff"), ("edit", "handoff")]
    g = guard(MODULE_ID)
    for op in m.policy_actions:
        with pytest.raises(PermissionDenied) as exc:
            g.require(security_ctx(), op, ResourceRef(type="handoff", id="h1", tenant_id="t2", attributes={"assignee_user_id": "u1", "requester_user_id": "u1"}))
        assert exc.value.details["operation"] == op and "tenant boundary" in str(exc.value)
    # create is open to any tenant member; manage/edit need participation
    assert g.require(security_ctx(), "handoff.request/v1", ResourceRef(type="handoff", id="new", tenant_id="t1")).allowed
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "handoff.assign/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1"))
    assert g.require(security_ctx(tenant_role="admin"), "handoff.assign/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1")).allowed
