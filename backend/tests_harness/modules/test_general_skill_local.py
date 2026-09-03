"""Per-module tests for ``general_skill.local`` (通用技能, kind A, slot staff.capability).

The provider materializes a published GeneralSkill package into the TaskFrame
workspace (a tmp dir here) after the PEP check; no model or network is involved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.branching import ensure_private_resource_binding, get_agent
from app.db.models import GeneralSkill
from staffdeck_harness.capabilities.facade import FacadeDeps, GeneralSkillFacade
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.builtin import GeneralSkillProvider
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "general_skill.local"
OPERATION = "general_skill.consume/v1"
CJK = re.compile(r"[一-鿿]")
SKILL_MD = "# 周报助手\n\n按部门汇总本周进展。\n"


# --------------------------------------------------------------------------- helpers

def _describe(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


class _Slot:
    active_sop_id = None
    active_node_id = None

    def __init__(self, allowed: dict[str, set[str]]):
        self._allowed = allowed

    def allowed(self) -> dict[str, set[str]]:
        return {k: set(v) for k, v in self._allowed.items()}

    def remaining_seconds(self):
        return None


class _FakeHost:
    """The subset of ``CapabilityHost`` a capability provider touches."""

    def __init__(self, db, guard, security_context, *, allowed: dict[str, set[str]], workspace: Path):
        self.db = db
        self.guard = guard
        self.security_context = security_context
        self.slot = _Slot(allowed)
        self.model_config = None
        self._agent_row = get_agent(db, security_context.tenant_id, "a1")
        self._workspace = workspace
        self.workspace_calls = 0

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(db=self.db, guard=self.guard, security_context=self.security_context, model_config=None, agent_row=self._agent_row, trace=None, remaining_seconds=self.slot.remaining_seconds)

    def _workspace_root(self, ctx) -> Path:
        self.workspace_calls += 1
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


@pytest.fixture
def host(db, guard, security_ctx, tmp_path):
    def _make(allowed: dict[str, set[str]] | None = None, **ctx_kw):
        return _FakeHost(db, guard(MODULE_ID), security_ctx(**ctx_kw), allowed=allowed or {}, workspace=tmp_path / "ws")

    return _make


@pytest.fixture
def skill(db):
    row = GeneralSkill(
        id="gs1", tenant_id="t1", slug="weekly-report", name="周报助手", description="生成周报", skill_markdown=SKILL_MD,
        skill_files_json=[{"path": "SKILL.md", "content": SKILL_MD}, {"path": "templates/report.md", "content": "# 模板\n"}],
        status="published", metadata_json={"scope": "agent_private"},
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_general_skill_local(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE and m.kind.value == "A"
    assert item.slot is SlotName.STAFF_CAPABILITY
    assert SlotName.SOP_SLOT_SKILL in m.attaches_to
    assert item.enabled is True
    assert isinstance(item.provider, GeneralSkillProvider)

    d = _describe(registry)
    assert d["kind"] == "A"
    assert d["slot"] == "staff.capability"
    assert d["provides"] == [OPERATION]
    assert d["requires"] == []
    assert d["policy_actions"] == [OPERATION]
    assert d["hooks"] == []
    assert d["name"] == "通用技能"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+", d["version"])
    assert d["contract_version"] == "v1"
    assert d["guarded"] is bool(d["policy_actions"]) is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_general_skill_local(registry):
    big = next(b for b in tree(registry.describe()) if b["id"] == "capability")
    sub = next(s for s in big["subs"] if s["id"] == "capability.skill")
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "capability", "sub_id": "capability.skill", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    everywhere = [(b["id"], s["id"]) for b in tree(registry.describe()) for s in b["subs"] for x in s["modules"] if x["module_id"] == MODULE_ID]
    assert everywhere == [("capability", "capability.skill")]


# --------------------------------------------------------------------------- 3. disable

def test_disable_general_skill_local():
    class _Disabled(FakeSettings):
        harness_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), _Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None, "disabling keeps the module installed"
    assert item.enabled is False
    assert _describe(reg)["enabled"] is False
    assert reg.for_operation(OPERATION) is None
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.STAFF_CAPABILITY)]


# --------------------------------------------------------------------------- 4. provider

def test_provider_general_skill_local_nothing_bound(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host(), invocation(OPERATION, arguments={"query": "写周报"}))
    assert isinstance(res, ModuleResult)
    assert res.success is False and res.error["code"] == "SKILL_NOT_AVAILABLE"
    # an unknown binding id is the same failure, not an exception
    res = provider.invoke(host(), invocation(OPERATION, arguments={"skill_id": "nope", "query": "写周报"}, binding_id="nope"))
    assert res.success is False and res.error["code"] == "SKILL_NOT_AVAILABLE"


def test_provider_general_skill_local_draft_is_unavailable(module, host, invocation, skill, db):
    skill.status = "draft"
    db.add(skill)
    db.commit()
    ensure_private_resource_binding(db, "t1", "a1", "general_skill", skill.id)
    db.commit()
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"general_skill": {skill.id}}), invocation(OPERATION, arguments={"skill_id": skill.id, "query": "写周报"}, binding_id=skill.id))
    assert res.success is False and res.error["code"] == "SKILL_NOT_AVAILABLE"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: projection.bound_resource_ref emits ResourceRef(type='general_skill'), which is outside the "
        "ResourceType vocabulary; LocalPep has no _authorize_general_skill handler so it falls through to "
        "_authorize_tenant_member and ALLOWS any same-tenant member to consume an unbound published skill. "
        "knowledge_base/tool refs are denied in the same situation. Only the activation fence (not a PEP) "
        "keeps this closed in production."
    ),
)
def test_provider_general_skill_local_unbound_is_denied_by_pep(module, host, invocation, skill):
    """The skill exists in the tenant and is in the activation set, but is not bound to a1: PEP must refuse."""

    provider = module(MODULE_ID).provider
    with pytest.raises(PermissionDenied) as exc:
        provider.invoke(host({"general_skill": {skill.id}}), invocation(OPERATION, arguments={"skill_id": skill.id, "query": "写周报"}, binding_id=skill.id))
    assert exc.value.details["operation"] == OPERATION
    assert exc.value.details["resource"] == skill.id


def test_provider_general_skill_local_pep_sees_general_skill_ref_type(host, invocation, skill):
    """Evidence for the xfail above: the live ref carries type 'general_skill', for which OSS_LOCAL has no rule."""

    from staffdeck_harness.composition import projection

    h = host()
    ref = projection.live_resource_ref(h.db, "t1", "general_skill", skill, agent=h._agent_row)
    assert ref.type == "general_skill"
    assert ref.attributes["binding_status"] is None, "no binding to a1 exists"
    # the same attributes on a ref typed 'skill' (the DEFAULT_ACTION_MAP resource type) are denied,
    # which is the behaviour the provider path should get as well
    as_skill = ResourceRef(type="skill", id=ref.id, tenant_id=ref.tenant_id, attributes=ref.attributes)
    assert h.guard.decide(h.security_context, OPERATION, as_skill).allowed is False


def test_provider_general_skill_local_bound_skill_is_materialized(module, host, invocation, skill, db, tmp_path):
    ensure_private_resource_binding(db, "t1", "a1", "general_skill", skill.id)
    db.commit()
    provider = module(MODULE_ID).provider
    h = host({"general_skill": {skill.id}})
    res = provider.invoke(h, invocation(OPERATION, arguments={"skill_id": skill.id, "query": "写周报"}, binding_id=skill.id))
    assert res.success is True, res.error
    data = res.data
    assert data["kind"] == "general_skill"
    assert data["slug"] == "weekly-report"
    assert data["operation"] == "read"
    assert data["query"] == "写周报"
    assert data["skill_markdown"] == SKILL_MD
    assert data["entrypoint_path"].endswith("/SKILL.md")
    assert data["sandbox_entrypoint_path"].startswith("/") and data["sandbox_entrypoint_path"].endswith("/SKILL.md")
    assert any(p.endswith("templates/report.md") for p in data["file_paths"])
    assert data["package"]["package_id"] == skill.id
    # the package really landed in the host workspace
    assert h.workspace_calls == 1
    written = tmp_path / "ws" / data["entrypoint_path"]
    assert written.is_file() and written.read_text(encoding="utf-8") == SKILL_MD


def test_provider_general_skill_local_execute_downgrades_to_read(module, host, invocation, skill, db):
    ensure_private_resource_binding(db, "t1", "a1", "general_skill", skill.id)
    db.commit()
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"general_skill": {skill.id}}), invocation(OPERATION, arguments={"skill_id": skill.id, "query": "q", "operation": "execute"}, binding_id=skill.id))
    assert res.success is True
    assert res.data["operation"] == "read"
    assert res.data["requested_operation"] == "execute"
    assert "compatibility_notice" in res.data
    res = provider.invoke(host({"general_skill": {skill.id}}), invocation(OPERATION, arguments={"skill_id": skill.id, "query": "q", "operation": "delete"}, binding_id=skill.id))
    assert res.success is False and res.error["code"] == "INVALID_ARGUMENTS"


def test_provider_general_skill_local_snapshot_digest_guard(host, invocation, skill, db, tmp_path):
    """The provider passes the live digest; a stale digest from an older activation is refused by the facade."""

    ensure_private_resource_binding(db, "t1", "a1", "general_skill", skill.id)
    db.commit()
    h = host({"general_skill": {skill.id}})
    facade = GeneralSkillFacade(h._deps(), tmp_path / "ws")
    res = facade.consume(invocation(OPERATION, arguments={"skill_id": skill.id, "query": "q"}, binding_id=skill.id), expected_digest="sha256:stale")
    assert res.success is False and res.error["code"] == "CAPABILITY_SNAPSHOT_CHANGED"


# --------------------------------------------------------------------------- 5. PEP

def test_pep_general_skill_local_denies_cross_tenant(registry, guard, security_ctx):
    actions = _describe(registry)["policy_actions"]
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in actions:
        assert mapper.map(op) == ("use", "skill")
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="skill", id="gs_x", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), OPERATION, foreign)
    assert "tenant" in exc.value.message
    assert exc.value.details["profile"] == "OSS_LOCAL"
    own = ResourceRef(type="skill", id="gs_x", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    assert g.require(security_ctx(), OPERATION, own).allowed is True
