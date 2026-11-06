"""Per-module tests for ``tool.local`` (业务工具调用, kind A, slot staff.capability).

One provider serves ``tool.invoke/v1`` / ``mcp.invoke/v1`` / ``a2a.invoke/v1``; the
facade picks the PEP operation from the Tool row's ``tool_type`` and delegates
execution to the legacy ``ToolExecutor``. Network execution is replaced by a
recording fake so the tests stay offline; the real ``ToolExecutor`` is still used
for the deterministic pre-network failure (unsupported tool type).
"""

from __future__ import annotations

from tests_harness.modules.conftest import invoke_provider

import re

import pytest

from app.agents.branching import ensure_private_resource_binding, get_agent
from app.db.models import MCPServer, Tool
from app.tools.tool_schema import ToolResult
from staffdeck_harness.capabilities import facade as facade_mod
from staffdeck_harness.capabilities.facade import FacadeDeps, ToolFacade
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.builtin import ToolProvider
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "tool.local"
OPERATIONS = ["tool.invoke/v1", "mcp.invoke/v1", "a2a.invoke/v1"]
CJK = re.compile(r"[一-鿿]")


# --------------------------------------------------------------------------- helpers

def _describe(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


class _Slot:
    active_node_id = None

    def __init__(self, allowed: dict[str, set[str]], active_sop_id: str | None = None):
        self._allowed = allowed
        self.active_sop_id = active_sop_id

    def allowed(self) -> dict[str, set[str]]:
        return {k: set(v) for k, v in self._allowed.items()}

    def remaining_seconds(self):
        return 12.5


class _RecordingGuard:
    """Wraps the real Guard and records every (operation, resource type, resource id) it was asked about."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, str, str]] = []

    def require(self, ctx, operation, resource):
        self.calls.append((operation, resource.type, resource.id))
        return self.inner.require(ctx, operation, resource)

    def decide(self, ctx, operation, resource):
        return self.inner.decide(ctx, operation, resource)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _FakeHost:
    """The subset of ``CapabilityHost`` a capability provider touches."""

    def __init__(self, db, guard, security_context, *, allowed: dict[str, set[str]], workspace, active_sop_id=None):
        self.db = db
        self.guard = _RecordingGuard(guard)
        self.security_context = security_context
        self.slot = _Slot(allowed, active_sop_id)
        self.model_config = None
        self._agent_row = get_agent(db, security_context.tenant_id, "a1")
        self._workspace = workspace

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(db=self.db, guard=self.guard, security_context=self.security_context, model_config=None, agent_row=self._agent_row, trace=None, remaining_seconds=self.slot.remaining_seconds)

    def _workspace_root(self, ctx):
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


class _FakeExecutor:
    """Stand-in for the legacy ToolExecutor: records the call and answers without network."""

    calls: list[dict] = []
    outcome: ToolResult | None = None

    def __init__(self, db):
        self.db = db

    def execute(self, tenant_id, tool_call, active_skill_id=None, agent_id=None, session_id=None, invocation_id=None, timeout_seconds_override=None):
        _FakeExecutor.calls.append({"tenant_id": tenant_id, "name": tool_call.name, "arguments": dict(tool_call.arguments), "active_skill_id": active_skill_id, "agent_id": agent_id, "session_id": session_id, "invocation_id": invocation_id, "timeout": timeout_seconds_override})
        return _FakeExecutor.outcome or ToolResult(tool_name=tool_call.name, success=True, data={"echo": dict(tool_call.arguments)})


@pytest.fixture
def fake_executor(monkeypatch):
    _FakeExecutor.calls = []
    _FakeExecutor.outcome = None
    monkeypatch.setattr(facade_mod, "ToolExecutor", _FakeExecutor)
    return _FakeExecutor


@pytest.fixture
def host(db, guard, security_ctx, tmp_path):
    def _make(allowed: dict[str, set[str]] | None = None, *, active_sop_id: str | None = None, **ctx_kw):
        return _FakeHost(db, guard(MODULE_ID), security_ctx(**ctx_kw), allowed=allowed or {}, workspace=tmp_path / "ws", active_sop_id=active_sop_id)

    return _make


def _tool(db, tool_id: str, *, tool_type: str = "http", method: str = "POST", enabled: bool = True, mcp_server_id: str | None = None) -> Tool:
    row = Tool(
        id=tool_id, tenant_id="t1", name=f"name_{tool_id}", display_name="订单查询", description="按订单号查询", tool_type=tool_type, method=method,
        url="http://tools.invalid/query", input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}}, enabled=enabled,
        mcp_server_id=mcp_server_id,
    )
    db.add(row)
    db.commit()
    return row


def _bind(db, tool_id: str) -> None:
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool_id)
    db.commit()


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_tool_local(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.CODE and m.kind.value == "A"
    assert item.slot is SlotName.STAFF_CAPABILITY
    assert SlotName.SOP_SLOT_ACTION in m.attaches_to
    assert item.enabled is True
    assert isinstance(item.provider, ToolProvider)

    d = _describe(registry)
    assert d["kind"] == "A"
    assert d["slot"] == "staff.capability"
    assert d["provides"] == OPERATIONS
    assert d["requires"] == []
    assert d["policy_actions"] == OPERATIONS
    assert d["hooks"] == []
    assert d["name"] == "业务工具调用"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+", d["version"])
    assert d["contract_version"] == "v1"
    assert d["guarded"] is bool(d["policy_actions"]) is True
    # the same provider object answers all three operations
    for op in OPERATIONS:
        assert registry.for_operation(op) is item


# --------------------------------------------------------------------------- 2. placement

def test_placement_tool_local(registry):
    big = next(b for b in tree(registry.describe()) if b["id"] == "capability")
    sub = next(s for s in big["subs"] if s["id"] == "capability.tool")
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "capability", "sub_id": "capability.tool", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True
    everywhere = [(b["id"], s["id"]) for b in tree(registry.describe()) for s in b["subs"] for x in s["modules"] if x["module_id"] == MODULE_ID]
    assert everywhere == [("capability", "capability.tool")]


# --------------------------------------------------------------------------- 3. disable

def test_disable_tool_local():
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
    for op in OPERATIONS:
        assert reg.for_operation(op) is None
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.STAFF_CAPABILITY)]


# --------------------------------------------------------------------------- 4. provider

def test_provider_tool_local_nothing_bound(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = invoke_provider(provider, host(), invocation("tool.invoke/v1", arguments={"order_id": "A1"}))
    assert isinstance(res, ModuleResult)
    assert res.success is False and res.error["code"] == "TOOL_NOT_AVAILABLE"
    res = invoke_provider(provider, host(), invocation("tool.invoke/v1", arguments={"tool_id": "nope", "order_id": "A1"}, binding_id="nope"))
    assert res.success is False and res.error["code"] == "TOOL_NOT_AVAILABLE"


def test_provider_tool_local_disabled_tool_is_unavailable(module, host, invocation, db):
    row = _tool(db, "tool_off", enabled=False)
    _bind(db, row.id)
    provider = module(MODULE_ID).provider
    res = invoke_provider(provider, host({"tool": {row.id}}), invocation("tool.invoke/v1", arguments={"tool_id": row.id, "order_id": "A1"}, binding_id=row.id))
    assert res.success is False and res.error["code"] == "TOOL_NOT_AVAILABLE"


def test_provider_tool_local_unbound_tool_is_denied_by_pep(module, host, invocation, db, fake_executor):
    row = _tool(db, "tool_unbound")
    provider = module(MODULE_ID).provider
    with pytest.raises(PermissionDenied) as exc:
        invoke_provider(provider, host({"tool": {row.id}}), invocation("tool.invoke/v1", arguments={"tool_id": row.id, "order_id": "A1"}, binding_id=row.id))
    assert exc.value.details == {"operation": "tool.invoke/v1", "resource_type": "tool", "resource": row.id, "profile": "OSS_LOCAL"}
    assert fake_executor.calls == [], "denied calls never reach the executor"


def test_provider_tool_local_bound_http_tool_executes(module, host, invocation, db, fake_executor):
    row = _tool(db, "tool_http")
    _bind(db, row.id)
    provider = module(MODULE_ID).provider
    h = host({"tool": {row.id}}, active_sop_id="sop_9")
    res = invoke_provider(provider, h, invocation("tool.invoke/v1", arguments={"tool_id": row.id, "order_id": "A1"}, binding_id=row.id))
    assert res.success is True, res.error
    assert res.data == {"echo": {"order_id": "A1"}}
    assert res.artifacts == ()
    assert h.guard.calls == [("tool.invoke/v1", "tool", row.id)]
    call = fake_executor.calls[0]
    assert call["name"] == row.name and call["arguments"] == {"order_id": "A1"}, "tool_id is stripped before execution"
    assert call["tenant_id"] == "t1" and call["agent_id"] == "a1" and call["session_id"] == "s1"
    assert call["invocation_id"] == "inv1"
    assert call["active_skill_id"] == "sop_9", "the active SOP is passed through for allowed_skills checks"
    assert call["timeout"] == 12.5, "remaining step time bounds the tool timeout"


def test_provider_tool_local_executor_error_becomes_module_failure(module, host, invocation, db, fake_executor):
    from app.tools.tool_schema import ToolError

    row = _tool(db, "tool_err")
    _bind(db, row.id)
    fake_executor.outcome = ToolResult(tool_name=row.name, success=False, error=ToolError(code="HTTP_ERROR", message="工具返回异常状态码：502"))
    provider = module(MODULE_ID).provider
    res = invoke_provider(provider, host({"tool": {row.id}}), invocation("tool.invoke/v1", arguments={"tool_id": row.id}, binding_id=row.id))
    assert res.success is False
    assert res.error == {"code": "HTTP_ERROR", "message": "工具返回异常状态码：502"}
    assert res.extensions["raw"]["success"] is False


def test_provider_tool_local_real_executor_rejects_unknown_tool_type_offline(module, host, invocation, db):
    """Real ToolExecutor path that settles before any network I/O."""

    row = _tool(db, "tool_grpc", tool_type="grpc")
    _bind(db, row.id)
    provider = module(MODULE_ID).provider
    h = host({"tool": {row.id}})
    res = invoke_provider(provider, h, invocation("tool.invoke/v1", arguments={"tool_id": row.id}, binding_id=row.id))
    assert res.success is False and res.error["code"] == "UNSUPPORTED_TOOL_TYPE"
    # unknown tool types are guarded with the generic tool.invoke action
    assert h.guard.calls == [("tool.invoke/v1", "tool", row.id)]


def test_provider_tool_local_mcp_tool_checks_tool_and_server(module, host, invocation, db, fake_executor):
    db.add(MCPServer(id="srv1", tenant_id="t1", name="crm", transport="streamable_http", url="http://mcp.invalid/mcp"))
    db.commit()
    row = _tool(db, "tool_mcp", tool_type="mcp", mcp_server_id="srv1")
    _bind(db, row.id)
    provider = module(MODULE_ID).provider
    h = host({"tool": {row.id}}, principal_id="admin", tenant_role="admin")
    res = invoke_provider(provider, h, invocation("mcp.invoke/v1", arguments={"tool_id": row.id, "q": 1}, binding_id=row.id, user_id="admin"))
    assert res.success is True, res.error
    assert h.guard.calls == [("mcp.invoke/v1", "tool", row.id), ("mcp.invoke/v1", "mcp_server", "srv1")]


def test_provider_tool_local_a2a_tool_uses_a2a_action(module, host, invocation, db, fake_executor):
    row = _tool(db, "tool_a2a", tool_type="a2a")
    _bind(db, row.id)
    provider = module(MODULE_ID).provider
    h = host({"tool": {row.id}})
    res = invoke_provider(provider, h, invocation("a2a.invoke/v1", arguments={"tool_id": row.id, "task": "x"}, binding_id=row.id))
    assert res.success is True, res.error
    assert h.guard.calls == [("a2a.invoke/v1", "tool", row.id)]


def test_provider_tool_local_snapshot_digest_guard(host, invocation, db, fake_executor):
    """The provider passes the live digest; a stale digest from an older activation is refused by the facade."""

    row = _tool(db, "tool_digest")
    _bind(db, row.id)
    h = host({"tool": {row.id}})
    res = ToolFacade(h._deps()).invoke(invocation("tool.invoke/v1", arguments={"tool_id": row.id}, binding_id=row.id), expected_digest="stale")
    assert res.success is False and res.error["code"] == "CAPABILITY_SNAPSHOT_CHANGED"
    assert fake_executor.calls == []


# --------------------------------------------------------------------------- 5. PEP

def test_pep_tool_local_denies_cross_tenant(registry, guard, security_ctx):
    actions = _describe(registry)["policy_actions"]
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    assert actions == OPERATIONS
    for op in actions:
        assert mapper.map(op) == ("use", "tool")
    g = guard(MODULE_ID)
    for op in actions:
        foreign = ResourceRef(type="tool", id="tool_x", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
        with pytest.raises(PermissionDenied) as exc:
            g.require(security_ctx(), op, foreign)
        assert "tenant" in exc.value.message
        assert exc.value.details["profile"] == "OSS_LOCAL"
        own = ResourceRef(type="tool", id="tool_x", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
        assert g.require(security_ctx(), op, own).allowed is True
