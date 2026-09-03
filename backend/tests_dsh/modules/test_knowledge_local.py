"""Per-module tests for ``knowledge.local`` (知识库检索, kind A, slot staff.capability).

Everything runs offline: the provider is driven through a fake CapabilityHost that
exposes the same surface the real ``CapabilityHost`` gives providers (``db``,
``slot.allowed()``, ``slot.active_sop_id``, ``_deps()``, ``_workspace_root()``).
A knowledge base without documents makes ``KnowledgeService.search`` return a
deterministic "no_documents" route without touching any model or network.
"""

from __future__ import annotations

import re

import pytest

from app.agents.branching import ensure_private_resource_binding, get_agent
from app.db.models import KnowledgeBase
from staffdeck_dsh.capabilities.facade import FacadeDeps
from staffdeck_dsh.contracts.errors import PermissionDenied
from staffdeck_dsh.contracts.invocation import ModuleResult
from staffdeck_dsh.contracts.manifest import ModuleKind, SlotName
from staffdeck_dsh.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_dsh.modules.builtin import KnowledgeProvider
from staffdeck_dsh.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_dsh.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "knowledge.local"
OPERATION = "knowledge.search/v1"
CJK = re.compile(r"[一-鿿]")


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

    def __init__(self, db, guard, security_context, *, allowed: dict[str, set[str]], workspace):
        self.db = db
        self.guard = guard
        self.security_context = security_context
        self.slot = _Slot(allowed)
        self.model_config = None
        self._agent_row = get_agent(db, security_context.tenant_id, "a1")
        self._workspace = workspace

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(db=self.db, guard=self.guard, security_context=self.security_context, model_config=None, agent_row=self._agent_row, trace=None, remaining_seconds=self.slot.remaining_seconds)

    def _workspace_root(self, ctx):
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


@pytest.fixture
def host(db, guard, security_ctx, tmp_path):
    def _make(allowed: dict[str, set[str]] | None = None, **ctx_kw):
        return _FakeHost(db, guard(MODULE_ID), security_ctx(**ctx_kw), allowed=allowed or {}, workspace=tmp_path / "ws")

    return _make


@pytest.fixture
def kb(db):
    row = KnowledgeBase(id="kb1", tenant_id="t1", name="售后政策", status="active", metadata_json={"scope": "agent_private"})
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_knowledge_local(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE and m.kind.value == "A"
    assert item.slot is SlotName.STAFF_CAPABILITY
    assert SlotName.SOP_SLOT_KNOWLEDGE in m.attaches_to
    assert item.enabled is True
    assert isinstance(item.provider, KnowledgeProvider)

    d = _describe(registry)
    assert d["kind"] == "A"
    assert d["slot"] == "staff.capability"
    assert d["provides"] == [OPERATION]
    assert d["requires"] == []
    assert d["policy_actions"] == [OPERATION]
    assert d["hooks"] == []
    assert d["name"] == "知识库检索"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+", d["version"])
    assert d["contract_version"] == "v1"
    assert d["guarded"] is bool(d["policy_actions"]) is True


# --------------------------------------------------------------------------- 2. placement

def test_placement_knowledge_local(registry):
    big = next(b for b in tree(registry.describe()) if b["id"] == "capability")
    sub = next(s for s in big["subs"] if s["id"] == "capability.knowledge")
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "capability", "sub_id": "capability.knowledge", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    # it is not listed anywhere else in the tree
    everywhere = [(b["id"], s["id"]) for b in tree(registry.describe()) for s in b["subs"] for x in s["modules"] if x["module_id"] == MODULE_ID]
    assert everywhere == [("capability", "capability.knowledge")]


# --------------------------------------------------------------------------- 3. disable

def test_disable_knowledge_local():
    class _Disabled(FakeSettings):
        dsh_disabled_modules = MODULE_ID

    reg = discover_and_install(ModuleRegistry(), _Disabled())
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    item = reg.get(MODULE_ID)
    assert item is not None, "disabling keeps the module installed"
    assert item.enabled is False
    assert _describe(reg)["enabled"] is False
    assert reg.for_operation(OPERATION) is None, "no other provider serves knowledge.search/v1"
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.STAFF_CAPABILITY)]


# --------------------------------------------------------------------------- 4. provider

def test_provider_knowledge_local_rejects_empty_query(module, host, invocation, kb):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"knowledge_base": {kb.id}}), invocation(OPERATION, arguments={"query": "   "}))
    assert isinstance(res, ModuleResult)
    assert res.success is False and res.error["code"] == "INVALID_ARGUMENTS"


def test_provider_knowledge_local_requested_base_outside_activation(module, host, invocation, kb):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"knowledge_base": {kb.id}}), invocation(OPERATION, arguments={"query": "退货", "knowledge_base_ids": ["kb_other"]}))
    assert res.success is False and res.error["code"] == "KNOWLEDGE_NOT_AVAILABLE"


def test_provider_knowledge_local_missing_row_is_revoked(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"knowledge_base": {"kb_missing"}}), invocation(OPERATION, arguments={"query": "退货"}))
    assert res.success is False and res.error["code"] == "CAPABILITY_AUTHORIZATION_REVOKED"


def test_provider_knowledge_local_unbound_base_is_denied_by_pep(module, host, invocation, kb):
    """The base exists in the tenant and is in the activation set, but is not bound to a1: PEP refuses."""

    provider = module(MODULE_ID).provider
    with pytest.raises(PermissionDenied) as exc:
        provider.invoke(host({"knowledge_base": {kb.id}}), invocation(OPERATION, arguments={"query": "退货"}))
    assert exc.value.details["operation"] == OPERATION
    assert exc.value.details["resource"] == kb.id


def test_provider_knowledge_local_bound_base_searches(module, host, invocation, kb, db):
    ensure_private_resource_binding(db, "t1", "a1", "knowledge_base", kb.id)
    db.commit()
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"knowledge_base": {kb.id}}), invocation(OPERATION, arguments={"query": "退货期限", "max_chunks": 3}))
    assert res.success is True, res.error
    assert isinstance(res.data, dict)
    # an empty base returns a deterministic route without touching any model
    phases = [t.get("phase") for t in res.data.get("route_trace", [])]
    assert phases and phases[-1] in {"no_documents", "no_visible_knowledge"}
    assert res.citations == ()


def test_provider_knowledge_local_admin_can_search_unbound_base(module, host, invocation, kb):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host({"knowledge_base": {kb.id}}, principal_id="admin", tenant_role="admin"), invocation(OPERATION, arguments={"query": "退货"}, user_id="admin"))
    assert res.success is True, res.error


# --------------------------------------------------------------------------- 5. PEP

def test_pep_knowledge_local_denies_cross_tenant(registry, guard, security_ctx):
    actions = _describe(registry)["policy_actions"]
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    for op in actions:
        assert mapper.map(op) == ("use", "knowledge_base")
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="knowledge_base", id="kb_x", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), OPERATION, foreign)
    assert "tenant" in exc.value.message
    assert exc.value.details["profile"] == "OSS_LOCAL"
    # same tenant + active private binding is allowed for a member
    own = ResourceRef(type="knowledge_base", id="kb_x", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    assert g.require(security_ctx(), OPERATION, own).allowed is True
