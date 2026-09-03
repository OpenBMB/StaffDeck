"""Module tests: ``team.provider`` (团队协作) — TeamProviderModule over the legacy team planner context."""

from __future__ import annotations

import re

import pytest

from app.db.models import AgentProfile, Team, TeamMember
from app.session.session_schema import TeamPlannerContext
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_dsh.modules.kernel import TeamProviderModule
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "team.provider"
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
def team(db) -> Team:
    row = Team(id="team1", tenant_id="t1", name="Support", owner_user_id="u1")
    db.add(row)
    db.add(AgentProfile(id="a2", tenant_id="t1", name="Researcher", status="active", description="查资料", metadata_json={"capabilities": ["search", " ", "search", "summarize"]}))
    db.add(AgentProfile(id="a3", tenant_id="t1", name="Retired", status="archived", metadata_json={}))
    db.add(AgentProfile(id="a_t2", tenant_id="t2", name="Foreign", status="active", metadata_json={}))
    db.add(TeamMember(team_id="team1", agent_id="a1", role="leader"))
    db.add(TeamMember(team_id="team1", agent_id="a2", role="member"))
    db.add(TeamMember(team_id="team1", agent_id="a3", role="member"))
    db.add(TeamMember(team_id="team1", agent_id="a_t2", role="member"))
    db.add(TeamMember(team_id="team1", agent_id="missing", role="member"))
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_team_provider(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE
    assert item.slot is SlotName.STAFF_TEAM and m.attaches_to == (SlotName.STAFF_TEAM,)
    assert m.provides_operations == ("team.delegate/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("team.delegate/v1",)
    assert m.hooks == ()
    assert isinstance(item.provider, TeamProviderModule)

    d = _describe(registry)
    assert d["kind"] == "A" and d["slot"] == "staff.team" and d["enabled"] is True and d["source"] == "builtin"
    assert d["name"] == "团队协作"
    assert d["summary"] and CJK.search(d["summary"])
    assert SEMVER.match(d["version"])
    assert d["provides"] == ["team.delegate/v1"] and d["requires"] == [] and d["hooks"] == []
    assert d["policy_actions"] == ["team.delegate/v1"]
    assert d["guarded"] is True
    assert registry.for_operation("team.delegate/v1").manifest.module_id == MODULE_ID


# --------------------------------------------------------------------------- 2. placement

def test_placement_team_provider(registry):
    m = _placed(registry)
    assert m["placement"] == {"big_id": "interaction", "sub_id": "interaction.team", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True


# --------------------------------------------------------------------------- 3. disable

def test_disable_team_provider():
    class Disabled(FakeSettings):
        dsh_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is False
    assert _describe(reg)["enabled"] is False
    assert reg.providers(SlotName.STAFF_TEAM) == []
    assert reg.for_operation("team.delegate/v1") is None


# --------------------------------------------------------------------------- 4. provider

def test_provider_team_provider_planner_context(module, db, team):
    provider = module(MODULE_ID).provider
    assert callable(getattr(provider, "planner_context", None))
    from app.teams.wakeup import build_team_planner_context  # the legacy function it delegates to

    assert callable(build_team_planner_context)
    ctx = provider.planner_context(db, team)
    assert isinstance(ctx, TeamPlannerContext)
    assert ctx.team_id == "team1" and ctx.leader_agent_id == "a1"
    # archived, foreign-tenant and dangling members are dropped; capabilities are trimmed, de-duplicated, description first
    assert [(m.agent_id, m.name, m.role) for m in ctx.members] == [("a1", "Agent One", "leader"), ("a2", "Researcher", "member")]
    assert ctx.members[0].capabilities == []
    assert ctx.members[1].capabilities == ["查资料", "search", "summarize"]
    assert ctx.model_dump()["members"][1]["agent_id"] == "a2"


def test_provider_team_provider_planner_context_without_leader(module, db):
    provider = module(MODULE_ID).provider
    row = Team(id="team2", tenant_id="t1", name="Empty", owner_user_id="u1")
    db.add(row)
    db.commit()
    ctx = provider.planner_context(db, row)
    assert ctx.team_id == "team2" and ctx.leader_agent_id == "" and ctx.members == []


# --------------------------------------------------------------------------- 5. events / pep

def test_events_or_pep_team_provider(module, guard, security_ctx):
    m = module(MODULE_ID).manifest
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in m.policy_actions:
        assert mapper.map(op) == ("delegate", "team")
    g = guard(MODULE_ID)
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), "team.delegate/v1", ResourceRef(type="team", id="team1", tenant_id="t2", attributes={"owner_user_id": "u1"}))
    assert exc.value.details == {"operation": "team.delegate/v1", "resource_type": "team", "resource": "team1", "profile": "OSS_LOCAL"}
    # delegate is not a manage action: any same-tenant member may delegate to the team
    assert g.require(security_ctx(), "team.delegate/v1", ResourceRef(type="team", id="team1", tenant_id="t1")).allowed
    assert g.require(security_ctx(tenant_role="admin"), "team.delegate/v1", ResourceRef(type="team", id="team1", tenant_id="t1")).allowed
    with pytest.raises(KeyError):
        mapper.map("team.delegate/v2")
