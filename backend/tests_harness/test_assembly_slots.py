"""Actual replacement and rejection proofs, without private code or external services."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from staffdeck_harness.contracts.errors import ContractIncompatible, ModuleSdkError
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.runtime_services import ExecutionIdentity
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.modules.config import RuntimeOverrides
from staffdeck_harness.modules.parameters import validate_public_parameters
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, manifest
from staffdeck_harness.runtime.services import execution_identity, runtime_services


def install(registry, name, slot, *, features=(), requires=(), conflicts=(), provider=None):
    registry.install(manifest(name, name, kind=ModuleKind.TRUSTED, slots=[slot],
        metadata={"exports_features": features, "requires_features": requires,
                  "conflicts_features": conflicts}),
        provider or SimpleNamespace(build=lambda db: db), slot=slot)


def test_missing_feature_rejected_and_registry_not_sealed():
    reg = ModuleRegistry()
    install(reg, "example.staff", SlotName.STAFF_SOURCE, requires=("identity.base",))
    with pytest.raises(ContractIncompatible, match="identity.base"):
        reg.seal()
    assert not reg.sealed
    install(reg, "example.identity", SlotName.IDENTITY_SOURCE, features=("identity.base",))
    reg.seal()


def test_incompatible_identity_mix_rejected():
    reg = ModuleRegistry()
    install(reg, "example.runtime", SlotName.RUNTIME_SERVICES, features=("identity.local",))
    install(reg, "example.staff", SlotName.STAFF_SOURCE, conflicts=("identity.local",))
    with pytest.raises(ContractIncompatible, match="conflicts"):
        reg.seal()


def test_single_slot_selection_does_not_replace_other_slots(monkeypatch):
    import staffdeck_harness.modules.registry as module
    original = module._load_callable
    def external(reg, ctx):
        install(reg, "example.staff", SlotName.STAFF_SOURCE, features=("staff.ids.oss",))
    monkeypatch.setattr(module, "_load_callable", lambda spec: external if spec == "example:register" else original(spec))
    settings = SimpleNamespace(security_profile="OSS_LOCAL", harness_modules="example:register",
        harness_disabled_modules="", harness_module_selections={"source.staff": "example.staff"})
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.seal()
    assert reg.provider(SlotName.STAFF_SOURCE).manifest.module_id == "example.staff"
    assert reg.provider(SlotName.IDENTITY_SOURCE).manifest.module_id == "source.identity.local"
    assert reg.provider(SlotName.RUNTIME_SERVICES).manifest.module_id == "runtime.services.local"
    assert not reg.get("source.staff.local").enabled


def test_optional_execution_can_be_cleared_without_removing_contract_checks(monkeypatch):
    import staffdeck_harness.modules.registry as module
    from staffdeck_harness.modules.compatibility import validate_compatibility
    def external(reg, ctx):
        reg.install(manifest("business.runtime", "企业运行数据接线", kind=ModuleKind.TRUSTED,
            slots=[SlotName.RUNTIME_SERVICES], metadata={"switchable": True, "exports_features": ["storage.business.runtime"]}),
            SimpleNamespace(build=lambda db: db), slot=SlotName.RUNTIME_SERVICES, enabled=False)
        reg.install(manifest("business.execution", "Base 执行委托", kind=ModuleKind.TRUSTED,
            slots=[SlotName.RUNTIME_EXECUTION], metadata={"switchable": True, "requires_features": ["storage.business.runtime"]}),
            SimpleNamespace(begin=lambda *a: None, end=lambda *a: None), slot=SlotName.RUNTIME_EXECUTION, enabled=False)
    monkeypatch.setattr(module, "_load_callable", lambda spec: external)
    settings = SimpleNamespace(security_profile="OSS_LOCAL", harness_modules="example:register", harness_disabled_modules="",
        harness_module_selections={"runtime.services": "runtime.services.local", "runtime.execution": "business.execution"})
    bad = discover_and_install(ModuleRegistry(), settings)
    with pytest.raises(ContractIncompatible, match="企业运行数据接线"):
        validate_compatibility(bad)
    row = next(row for row in bad.describe() if row["module_id"] == "business.execution")
    assert row["optional_slot"] and row["feature_requires"] == ["storage.business.runtime"]
    settings.harness_module_selections = {"runtime.services": "runtime.services.local"}
    settings.harness_disabled_modules = "business.execution"
    fixed = discover_and_install(ModuleRegistry(), settings)
    fixed.seal()
    assert fixed.provider(SlotName.RUNTIME_EXECUTION) is None
    assert fixed.provider(SlotName.RUNTIME_SERVICES).manifest.module_id == "runtime.services.local"


@pytest.mark.parametrize("value", [
    {"module": {"token": "sensitive"}}, {"module": {"nested": {"password": "sensitive"}}},
    {"module": {"url": "http://user:password@localhost"}},
    {"module": {"url": "https://localhost?key=sensitive"}},
])
def test_credentials_rejected_without_echo(value):
    with pytest.raises(ValueError) as exc:
        validate_public_parameters(value)
    assert "sensitive" not in str(exc.value)


def test_public_references_allowed_and_changes_require_apply():
    configs = {"example.runtime": {"credential_ref": "runtime-test", "namespace": "test"}}
    validate_public_parameters(configs)
    assert not RuntimeOverrides(module_configs=configs).same_assembly(RuntimeOverrides())
    assert not RuntimeOverrides(selections={"source.staff": "example.staff"}).same_assembly(RuntimeOverrides())


def identity():
    return ExecutionIdentity("tenant", "actor", "staff", "session", "outer-run", 3, "outer-trace")


def test_inner_attempt_does_not_rewrite_outer_identity():
    original = SecurityContext("actor", "tenant", execution=identity())
    step = replace(original, run_id="step-run", run_attempt=1)
    assert step.execution.outer_run_id == "outer-run"
    assert step.execution.outer_attempt == 3


def test_trusted_identity_must_match_request_and_session():
    request = SimpleNamespace(tenant_id="other", _trusted_execution=identity())
    with pytest.raises(ModuleSdkError, match="上下文"):
        execution_identity(request, SimpleNamespace(agent_id="staff", id="session"))


def test_selected_runtime_factory_never_reads_caller_bind():
    seen = []
    class Remote:
        @contextmanager
        def session(self, execution, *, purpose):
            seen.append((execution, purpose))
            yield "private multi-bind session"
            seen.append("closed")
    reg = ModuleRegistry()
    install(reg, "example.runtime", SlotName.RUNTIME_SERVICES,
            provider=SimpleNamespace(build=lambda db: Remote()))
    reg.seal()
    services = runtime_services(object(), reg)
    with services.session(identity(), purpose="trace") as db:
        assert db == "private multi-bind session"
    assert seen == [(identity(), "trace"), "closed"]


def test_task_agent_uses_selected_factory_and_closes_on_setup_error():
    from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent
    closed = []
    @contextmanager
    def session(execution, *, purpose):
        assert execution == identity() and purpose == "capability"
        try:
            yield "remote-db"
        finally:
            closed.append(True)
    reg = ModuleRegistry()
    install(reg, "example.runtime", SlotName.RUNTIME_SERVICES,
        provider=SimpleNamespace(build=lambda db: SimpleNamespace(session=session)))
    reg.seal()
    agent = object.__new__(HarnessV3TaskAgent)
    agent.turn = SimpleNamespace(db=object(), module_registry=reg,
        security_context=SecurityContext("actor", "tenant", execution=identity()))
    def failing(*args, **kwargs):
        assert kwargs["_host_db"] == "remote-db"
        raise RuntimeError("setup failed")
    agent._run = failing
    with pytest.raises(RuntimeError, match="setup failed"):
        agent.run(None, None, None)
    assert closed == [True]
