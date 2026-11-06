"""Module tests: ``handoff.assignment.default`` (处理人指派（默认）) — DefaultAssignment strategy."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlmodel import select

from app.db.models import AgentEvent, AgentProfile, ChatSession, HumanHandoffRequest, Tenant, User, utc_now
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.handoff.core import AssignmentStrategy, DefaultAssignment, HandoffCore, HandoffTransitionError
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "handoff.assignment.default"
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
def people(db):
    """Extra users: a second admin (older), a channel-only user, and a foreign-tenant user."""

    db.add(Tenant(id="t2", name="T2"))
    db.add(User(id="admin0", tenant_id="t1", username="admin0", role="admin", password_hash="x", created_at=utc_now() - timedelta(days=1)))
    db.add(User(id="bot_user", tenant_id="t1", username="bot", role="member", password_hash="x", source="feishu"))
    db.add(User(id="outsider", tenant_id="t2", username="outsider", role="admin", password_hash="x"))
    db.add(AgentProfile(id="a_noowner", tenant_id="t1", name="No Owner", status="active", metadata_json={}))
    db.commit()


def _handoff(db, *, agent_id: str | None = "a1", metadata: dict | None = None, status: str = "pending") -> HumanHandoffRequest:
    row = HumanHandoffRequest(tenant_id="t1", session_id="s1", agent_id=agent_id, requester_user_id="u1", status=status, metadata_json=dict(metadata or {}))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_handoff_assignment_default(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.HANDOFF_ASSIGNMENT and m.attaches_to == (SlotName.HANDOFF_ASSIGNMENT,)
    assert m.provides_operations == ("handoff.assign/v1",)
    assert m.requires_operations == () and m.policy_actions == () and m.hooks == ()
    assert isinstance(item.provider, DefaultAssignment)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "handoff.assignment" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "处理人指派（默认）"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["handoff.assign/v1"] and d["requires"] == [] and d["hooks"] == [] and d["policy_actions"] == []
    assert d["guarded"] is True
    assert d["guarded"] or not d["policy_actions"]
    # it is the first (i.e. the resolved) provider of the assignment slot
    assert registry.provider(SlotName.HANDOFF_ASSIGNMENT).manifest.module_id == MODULE_ID


# --------------------------------------------------------------------------- 2. placement

def test_placement_handoff_assignment_default(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.human_task", "source": "slot"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_handoff_assignment_default():
    class Disabled(FakeSettings):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _describe(reg)["enabled"] is False
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.HANDOFF_ASSIGNMENT)]


# --------------------------------------------------------------------------- 4. provider

def test_provider_handoff_assignment_default_prefers_step_then_binding_then_owner_then_admin(module, db, people):
    strategy = module(MODULE_ID).provider
    assert strategy.slot == "handoff.assignment"
    assert isinstance(strategy, AssignmentStrategy)
    agent = db.get(AgentProfile, "a1")

    # 1. SOP step assignee wins
    assert strategy.propose(db, _handoff(db, metadata={"step_assignee_user_id": "admin", "binding_default_assignee_user_id": "u1"}), agent=agent) == "admin"
    # 2. channel binding default assignee
    assert strategy.propose(db, _handoff(db, metadata={"binding_default_assignee_user_id": "admin"}), agent=agent) == "admin"
    # 3. agent owner (conftest: a1 is owned by u1)
    assert strategy.propose(db, _handoff(db), agent=agent) == "u1"
    # 4. oldest tenant admin when there is no owner
    assert strategy.propose(db, _handoff(db, agent_id="a_noowner"), agent=db.get(AgentProfile, "a_noowner")) == "admin0"
    assert strategy.propose(db, _handoff(db, agent_id=None), agent=None) == "admin0"


def test_provider_handoff_assignment_default_skips_external_and_foreign_users(module, db, people):
    strategy = module(MODULE_ID).provider
    agent = db.get(AgentProfile, "a1")
    # channel-only (non-web) users and users of another tenant are not valid internal assignees → fall through to owner
    assert strategy.propose(db, _handoff(db, metadata={"step_assignee_user_id": "bot_user"}), agent=agent) == "u1"
    assert strategy.propose(db, _handoff(db, metadata={"step_assignee_user_id": "outsider"}), agent=agent) == "u1"
    assert strategy.propose(db, _handoff(db, metadata={"step_assignee_user_id": "ghost"}), agent=agent) == "u1"
    # an owner from another tenant is ignored too
    foreign_owner = AgentProfile(id="a_foreign", tenant_id="t1", name="F", status="active", metadata_json={"owner_user_id": "outsider"})
    assert strategy.propose(db, _handoff(db, agent_id="a_foreign"), agent=foreign_owner) == "admin0"


def test_provider_handoff_assignment_default_no_admin_returns_none(module, db):
    strategy = module(MODULE_ID).provider
    for u in db.exec(select(User).where(User.role == "admin")).all():
        db.delete(u)
    db.commit()
    assert strategy.propose(db, _handoff(db, agent_id=None), agent=None) is None


def test_provider_handoff_assignment_default_through_core_assign(module, db, guard, security_ctx, people):
    """HandoffCore validates the proposal against the tenant and moves pending → assigned."""

    strategy = module(MODULE_ID).provider
    db.add(ChatSession(id="s1", tenant_id="t1", user_id="u1", agent_id="a1", status="active"))
    db.commit()
    core = HandoffCore(db=db, guard=guard("handoff"), assignment=strategy, notifiers={}, resolvers={})
    row = _handoff(db)
    out = core.assign(security_ctx(), row, actor_is_system=True)
    assert out.status == "assigned" and out.assignee_user_id == "u1"
    events = db.exec(select(AgentEvent).where(AgentEvent.event_type == "human_handoff_assigned")).all()
    assert len(events) == 1 and events[0].payload_json == {"handoff_id": row.id, "assignee_user_id": "u1"}
    # explicit reassignment by a participant stays assigned; a foreign user is rejected by the Core
    core.assign(security_ctx(principal_id="admin", tenant_role="admin"), row, assignee_user_id="admin0")
    assert row.status == "assigned" and row.assignee_user_id == "admin0"
    with pytest.raises(HandoffTransitionError):
        core.assign(security_ctx(principal_id="admin", tenant_role="admin"), row, assignee_user_id="outsider")
    # a stranger cannot reassign
    with pytest.raises(PermissionDenied):
        core.assign(security_ctx(principal_id="stranger"), row, assignee_user_id="admin")
    # when nothing is proposed the row stays pending
    for u in db.exec(select(User).where(User.role == "admin")).all():
        db.delete(u)
    db.commit()
    pending = _handoff(db, agent_id=None)
    core.assign(security_ctx(), pending, actor_is_system=True)
    assert pending.status == "pending" and pending.assignee_user_id is None


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_handoff_assignment_default(module, guard, security_ctx):
    """No policy_actions of its own; the operation it provides (handoff.assign/v1) is enforced by the Core."""

    assert module(MODULE_ID).manifest.policy_actions == ()
    assert PolicyActionMapper(DEFAULT_ACTION_MAP).map("handoff.assign/v1") == ("manage", "handoff")
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "handoff.assign/v1", ResourceRef(type="handoff", id="h1", tenant_id="t2", attributes={"assignee_user_id": "u1"}))
    # same tenant: assignee / agent owner / admin may manage, others not
    assert g.require(security_ctx(), "handoff.assign/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1", attributes={"agent_owner_user_id": "u1"})).allowed
    with pytest.raises(PermissionDenied):
        g.require(security_ctx(), "handoff.assign/v1", ResourceRef(type="handoff", id="h1", tenant_id="t1", attributes={"assignee_user_id": "someone"}))
