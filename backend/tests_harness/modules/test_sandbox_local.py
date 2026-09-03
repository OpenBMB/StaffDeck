"""Per-module tests for ``sandbox.local`` (受控执行环境, kind T, slot staff.capability).

The provider hands the invocation to the host's ``SandboxFacade``: PEP check on a
``capability`` ref (``sandbox:<tool>``), then the legacy ``HarnessExecutor``
registry of file/command tools inside the TaskFrame workspace. Tests use a tmp
workspace with the OS sandbox disabled, so everything is local and deterministic.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.agents.branching import get_agent
from staffdeck_harness.capabilities.facade import FacadeDeps, SandboxFacade
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, PolicyActionMapper, ResourceRef
from staffdeck_harness.modules.builtin import SandboxProvider
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install
from staffdeck_harness.modules.taxonomy import tree

from .conftest import FakeSettings

MODULE_ID = "sandbox.local"
OPERATION = "sandbox.execute/v1"
PROVIDES = ["sandbox.execute/v1", "artifact.publish/v1"]
CJK = re.compile(r"[一-鿿]")


# --------------------------------------------------------------------------- helpers

def _describe(registry, module_id: str = MODULE_ID) -> dict:
    return next(m for m in registry.describe() if m["module_id"] == module_id)


class _Slot:
    active_sop_id = None
    active_node_id = None

    def __init__(self, session_policy: dict | None = None):
        self.session_policy = dict(session_policy or {})

    def allowed(self) -> dict[str, set[str]]:
        return {}

    def remaining_seconds(self):
        return None


class _RecordingGuard:
    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, str, str]] = []

    def require(self, ctx, operation, resource):
        self.calls.append((operation, resource.type, resource.id))
        return self.inner.require(ctx, operation, resource)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _FakeHost:
    """The subset of ``CapabilityHost`` the sandbox provider touches (``sandbox(ctx)``)."""

    def __init__(self, db, guard, security_context, *, workspace: Path, session_policy: dict | None = None):
        self.db = db
        self.guard = _RecordingGuard(guard)
        self.security_context = security_context
        self.slot = _Slot(session_policy)
        self.model_config = None
        self.run_id = None
        self._agent_row = get_agent(db, security_context.tenant_id, "a1")
        self._workspace = workspace
        self._sandbox: SandboxFacade | None = None

    def _deps(self) -> FacadeDeps:
        return FacadeDeps(db=self.db, guard=self.guard, security_context=self.security_context, model_config=None, agent_row=self._agent_row, trace=None, remaining_seconds=self.slot.remaining_seconds)

    def _workspace_root(self, ctx) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def sandbox(self, ctx) -> SandboxFacade:
        # mirrors CapabilityHost.sandbox(): one facade per host, built from the session policy
        if self._sandbox is None:
            policy = self.slot.session_policy
            self._sandbox = SandboxFacade(
                self._deps(),
                workspace_root=self._workspace_root(ctx),
                run_id=self.run_id or ctx.run_id or ctx.turn_id,
                task_frame_id=ctx.task_frame_id or ctx.turn_id,
                sandbox_enabled=bool(policy.get("sandbox_enabled", False)),
                network_mode=str(policy.get("sandbox_network_mode", "all")),
                allowed_domains=tuple(policy.get("sandbox_allowed_domains", ())),
            )
        return self._sandbox


@pytest.fixture
def host(db, guard, security_ctx, tmp_path):
    def _make(**ctx_kw):
        return _FakeHost(db, guard(MODULE_ID), security_ctx(**ctx_kw), workspace=tmp_path / "ws")

    return _make


def _call(invocation, tool: str, **arguments):
    return invocation(OPERATION, module_id="sandbox", arguments={"tool": tool, "arguments": arguments})


# --------------------------------------------------------------------------- 1. manifest

def test_manifest_sandbox_local(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED and m.kind.value == "T"
    assert item.slot is SlotName.STAFF_CAPABILITY
    assert SlotName.SOP_SLOT_ACTION in m.attaches_to
    assert item.enabled is True
    assert isinstance(item.provider, SandboxProvider)
    assert m.metadata.get("switchable") is True

    d = _describe(registry)
    assert d["kind"] == "T"
    assert d["slot"] == "staff.capability"
    assert d["provides"] == PROVIDES
    assert d["requires"] == []
    assert d["policy_actions"] == [OPERATION], "artifact.publish is provided but guarded through the session write path, not a module action"
    assert d["hooks"] == []
    assert d["name"] == "受控执行环境"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+", d["version"])
    assert d["contract_version"] == "v1"
    assert d["guarded"] is bool(d["policy_actions"]) is True
    assert d["switchable"] is True, "T module explicitly marked switchable in metadata"
    assert "switchable" not in d["metadata"], "switchable is lifted to a top-level flag"
    for op in PROVIDES:
        assert registry.for_operation(op) is item


# --------------------------------------------------------------------------- 2. placement

def test_placement_sandbox_local(registry):
    big = next(b for b in tree(registry.describe()) if b["id"] == "capability")
    sub = next(s for s in big["subs"] if s["id"] == "capability.execution")
    assert sub["kind"] == "T"
    mods = [m for m in sub["modules"] if m["module_id"] == MODULE_ID]
    assert len(mods) == 1
    m = mods[0]
    assert m["placement"] == {"big_id": "capability", "sub_id": "capability.execution", "source": "taxonomy"}
    assert m["switchable"] is True
    assert m["movable"] is True, "T modules outside runtime.engine/security.pep may be re-parented by the operator"
    everywhere = [(b["id"], s["id"]) for b in tree(registry.describe()) for s in b["subs"] for x in s["modules"] if x["module_id"] == MODULE_ID]
    assert everywhere == [("capability", "capability.execution")]


# --------------------------------------------------------------------------- 3. disable

def test_disable_sandbox_local():
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
    assert _describe(reg)["switchable"] is True
    for op in PROVIDES:
        assert reg.for_operation(op) is None
    assert MODULE_ID not in [i.manifest.module_id for i in reg.providers(SlotName.STAFF_CAPABILITY)]


# --------------------------------------------------------------------------- 4. provider

def test_provider_sandbox_local_unknown_tool(module, host, invocation):
    provider = module(MODULE_ID).provider
    h = host()
    res = provider.invoke(h, _call(invocation, "format_disk"))
    assert isinstance(res, ModuleResult)
    assert res.success is False and res.error["code"] == "TOOL_NOT_FOUND"
    assert h.guard.calls == [(OPERATION, "capability", "sandbox:format_disk")], "the PEP runs before the tool lookup"


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason=(
        "BUG (minor): SandboxFacade.execute builds HarnessToolCall(name='') when the 'tool' argument is missing, "
        "so the provider raises ValueError instead of returning ModuleResult.fail('INVALID_ARGUMENTS'); "
        "CapabilityHost.invoke only masks this as a generic HARNESS_TOOL_ERROR."
    ),
)
def test_provider_sandbox_local_missing_tool_name_is_invalid_arguments(module, host, invocation):
    provider = module(MODULE_ID).provider
    h = host()
    res = provider.invoke(h, invocation(OPERATION, module_id="sandbox", arguments={}))
    assert isinstance(res, ModuleResult)
    assert res.success is False and res.error["code"] in {"INVALID_ARGUMENTS", "TOOL_NOT_FOUND"}
    assert h.guard.calls[-1] == (OPERATION, "capability", f"sandbox:{OPERATION}")


def test_provider_sandbox_local_write_then_read_in_workspace(module, host, invocation, tmp_path):
    provider = module(MODULE_ID).provider
    h = host()
    res = provider.invoke(h, _call(invocation, "write_file", path="notes/out.txt", content="你好，沙箱", create_parents=True))
    assert res.success is True, res.error
    assert (tmp_path / "ws" / "notes" / "out.txt").read_text(encoding="utf-8") == "你好，沙箱"
    res = provider.invoke(h, _call(invocation, "read_file", path="notes/out.txt"))
    assert res.success is True, res.error
    assert res.data["content"] == "你好，沙箱"
    res = provider.invoke(h, _call(invocation, "list_directory", path="notes"))
    assert res.success is True, res.error
    names = {Path(str(e.get("path") or e.get("name"))).name for e in res.data.get("entries", [])}
    assert "out.txt" in names
    # one facade per host, and the registry exposes the file/command toolset
    assert h.sandbox(None) is h._sandbox
    names = set(h._sandbox.tool_names())
    assert {"read_file", "write_file", "list_directory", "exec_command"} <= names
    assert {s["name"] for s in h._sandbox.schemas()} == names


def test_provider_sandbox_local_invalid_arguments(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host(), _call(invocation, "read_file"))
    assert res.success is False
    assert res.error["code"]
    assert isinstance(res.extensions.get("details"), dict)


def test_provider_sandbox_local_missing_file(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host(), _call(invocation, "read_file", path="does/not/exist.txt"))
    assert res.success is False and res.error["code"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_provider_sandbox_local_nonzero_exit_is_a_failure(module, host, invocation):
    provider = module(MODULE_ID).provider
    res = provider.invoke(host(), _call(invocation, "exec_command", command="exit 3", timeout_seconds=5))
    assert res.success is False and res.error["code"] == "COMMAND_EXIT_NONZERO"
    assert res.extensions["data"]["ok"] is not True


def test_provider_sandbox_local_cross_tenant_context_is_denied(module, host, invocation):
    """Invocation stamped for tenant t2 while the acting principal is in t1: the PEP refuses before executing."""

    provider = module(MODULE_ID).provider
    with pytest.raises(PermissionDenied) as exc:
        provider.invoke(host(), invocation(OPERATION, module_id="sandbox", arguments={"tool": "write_file", "arguments": {"path": "x", "content": "y"}}, tenant_id="t2"))
    assert exc.value.details == {"operation": OPERATION, "resource_type": "capability", "resource": "sandbox:write_file", "profile": "OSS_LOCAL"}


def test_provider_sandbox_local_discover_artifacts(module, host, invocation, tmp_path):
    provider = module(MODULE_ID).provider
    h = host()
    ctx = _call(invocation, "write_file", path="report.md", content="# 报告\n").context
    assert provider.invoke(h, _call(invocation, "write_file", path="report.md", content="# 报告\n")).success is True
    found = h._sandbox.discover_artifacts(ctx.task_frame_id or ctx.turn_id)
    assert isinstance(found, list)
    if found:  # artifact discovery is a legacy heuristic; when it reports, the shape is stable
        item = found[0]
        assert item["source"] == "harness_v3.workspace_discovery"
        assert item["sandbox_path"].startswith("/") and item["display_name"]


# --------------------------------------------------------------------------- 5. PEP

def test_pep_sandbox_local_denies_cross_tenant(registry, guard, security_ctx):
    actions = _describe(registry)["policy_actions"]
    mapper = PolicyActionMapper(DEFAULT_ACTION_MAP)
    assert actions == [OPERATION]
    assert mapper.map(OPERATION) == ("execute", "capability")
    assert mapper.map("artifact.publish/v1") == ("write", "session"), "the second provided operation is also mapped"
    g = guard(MODULE_ID)
    foreign = ResourceRef(type="capability", id="sandbox:exec_command", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(), OPERATION, foreign)
    assert "tenant" in exc.value.message
    assert exc.value.details["profile"] == "OSS_LOCAL"
    own = ResourceRef(type="capability", id="sandbox:exec_command", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    assert g.require(security_ctx(), OPERATION, own).allowed is True
    # a service principal (scheduler) may execute but never manage
    svc = security_ctx(principal_id="svc", principal_type="service", tenant_role="service")
    assert g.require(svc, OPERATION, own).allowed is True
