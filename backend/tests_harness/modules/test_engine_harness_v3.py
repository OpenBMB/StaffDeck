"""Module test: ``engine.harness_v3`` (Harness v3 引擎, trusted, slot runtime.engine).

Provider ``HarnessV3BridgeEngine.open(loop, request, agent_id)`` returns a
``HarnessV3Engine`` (a ``HarnessV2Engine`` whose step executor is the external Harness v3 engine
process). Everything here stays offline: the Harness v3 runtime is never started —
``get_runtime`` is either short-circuited by an empty ``harness_v3_root`` (→
``EngineUnavailable``) or replaced by a stub.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import staffdeck_harness.bridge.engine_host as engine_host
from staffdeck_harness.contracts.errors import EngineUnavailable, PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import DEFAULT_ACTION_MAP, ResourceRef
from staffdeck_harness.modules.builtin import HarnessV3BridgeEngine
from staffdeck_harness.modules.registry import ModuleRegistry, SlotConflict, discover_and_install
from staffdeck_harness.modules.taxonomy import FIXED_SLOTS, tree

MODULE_ID = "engine.harness_v3"
CJK = re.compile(r"[一-鿿]")


def _described(registry, module_id: str = MODULE_ID) -> dict:
    hit = [m for m in registry.describe() if m["module_id"] == module_id]
    assert len(hit) == 1
    return hit[0]


def _placed(registry, module_id: str = MODULE_ID) -> tuple[dict, dict, dict]:
    for big in tree(registry.describe()):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == module_id:
                    return big, sub, m
    raise AssertionError(f"{module_id} not placed")


def _guarded_registry(settings) -> ModuleRegistry:
    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


@pytest.fixture(autouse=True)
def _no_live_runtime(monkeypatch):
    """Never touch a cached Harness v3 runtime from another test/process, and never leave one behind."""

    monkeypatch.setattr(engine_host, "_runtime", None)
    monkeypatch.delenv("HARNESS_V3_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_V3_HOME", raising=False)
    yield
    monkeypatch.setattr(engine_host, "_runtime", None)


@pytest.fixture
def fake_loop(db):
    recorded: list[tuple] = []
    events = SimpleNamespace(record=lambda *a, **k: recorded.append(a), recorded=recorded)
    return SimpleNamespace(db=db, events=events)


@pytest.fixture
def empty_root_settings(monkeypatch, settings):
    """``app.config.get_settings`` → FakeSettings with harness_v3_root='' (what HarnessV3BridgeEngine.open reads)."""

    import app.config as app_config

    settings.harness_v3_root = ""
    settings.harness_v3_home = ""
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    return settings


# --------------------------------------------------------------------------- 1. manifest


def test_manifest_engine_harness_v3(registry, module):
    item = module(MODULE_ID)
    m = item.manifest
    assert m.module_id == MODULE_ID
    assert m.kind is ModuleKind.TRUSTED
    assert item.slot is SlotName.RUNTIME_ENGINE
    assert m.attaches_to == (SlotName.RUNTIME_ENGINE,)
    assert m.provides_operations == ("runtime.turn/v1",)
    assert m.requires_operations == ()
    assert m.policy_actions == ("staff.use/v1",)
    assert m.hooks == ()
    assert isinstance(item.provider, HarnessV3BridgeEngine)
    assert item.provider.module_id == MODULE_ID
    assert item.enabled is False, "harness_v3_enabled=False → Harness v3 installed but inactive"

    d = _described(registry)
    assert d["kind"] == "T"
    assert d["slot"] == "runtime.engine"
    assert d["name"] == "Harness v3 引擎"
    assert d["summary"] and CJK.search(d["summary"])
    assert re.match(r"^\d+\.\d+\.\d+$", d["version"])
    assert d["provides"] == ["runtime.turn/v1"]
    assert d["policy_actions"] == ["staff.use/v1"]
    assert d["requires"] == [] and d["hooks"] == []
    assert d["guarded"] is True, "declares policy actions → must sit under a PEP-bound slot"
    assert d["enabled"] is False


def test_manifest_engine_harness_v3_policy_actions_need_guarded_slot(settings):
    """seal() refuses an *active* engine.harness_v3 whose slot has no PEP-bound host."""

    from staffdeck_harness.contracts.errors import PepBindingMissing

    settings.harness_v3_enabled = True
    reg = discover_and_install(ModuleRegistry(), settings)
    # builtin.register marks runtime.engine guarded; simulate a host that forgot to
    reg._guarded_slots.discard(SlotName.RUNTIME_ENGINE)
    with pytest.raises(PepBindingMissing) as exc:
        reg.seal()
    assert exc.value.details == {"slot": "runtime.engine"}


# --------------------------------------------------------------------------- 2. placement


def test_placement_engine_harness_v3(registry):
    big, sub, m = _placed(registry)
    assert big["id"] == "runtime"
    assert sub["id"] == "runtime.bridge"
    assert sub["kind"] == "T"
    assert m["placement"] == {"big_id": "runtime", "sub_id": "runtime.bridge", "source": "slot"}
    assert m["switchable"] is False
    assert m["movable"] is False
    assert m["slot"] in FIXED_SLOTS


def test_placement_engine_harness_v3_override_ignored(registry):
    for big in tree(registry.describe(), placements={MODULE_ID: "runtime.agentloop"}):
        for sub in big["subs"]:
            for m in sub["modules"]:
                if m["module_id"] == MODULE_ID:
                    assert (big["id"], sub["id"]) == ("runtime", "runtime.bridge")
                    assert m["placement"]["source"] == "slot"
                    return
    raise AssertionError("module vanished from the tree")


# --------------------------------------------------------------------------- 3. disable


def test_disable_engine_harness_v3_not_switchable(registry):
    d = _described(registry)
    assert d["kind"] == "T"
    assert d["switchable"] is False
    assert d["slot"] == "runtime.engine", "admin: 通过选择引擎切换，不能单独停用"


def test_disable_engine_harness_v3_activated_by_harness_v3_enabled(settings):
    settings.harness_v3_enabled = True
    reg = _guarded_registry(settings)
    item = reg.get(MODULE_ID)
    assert item is not None and item.enabled is True
    assert reg.provider(SlotName.RUNTIME_ENGINE).manifest.module_id == MODULE_ID
    assert reg.get("engine.harness_v2").enabled is False


def test_disable_engine_harness_v3_two_active_engines_conflict(settings):
    settings.harness_v3_enabled = True
    reg = discover_and_install(ModuleRegistry(), settings)
    reg.set_enabled("engine.harness_v2", True)
    with pytest.raises(SlotConflict) as exc:
        reg.seal()
    assert "runtime.engine" in str(exc.value)


# --------------------------------------------------------------------------- 4. provider


def test_provider_engine_harness_v3_open_raises_engine_unavailable_without_root(module, fake_loop, empty_root_settings):
    with pytest.raises(EngineUnavailable) as exc:
        module(MODULE_ID).provider.open(fake_loop, None, "a1")
    assert exc.value.code == "ENGINE_UNAVAILABLE"
    # nothing was cached: a later call with a configured root must be allowed to start fresh
    assert engine_host._runtime is None


def test_provider_engine_harness_v3_open_raises_when_root_has_no_build(module, fake_loop, monkeypatch, settings, tmp_path):
    import app.config as app_config

    settings.harness_v3_root = str(tmp_path / "engine-root")
    (tmp_path / "engine-root").mkdir()
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    with pytest.raises(EngineUnavailable) as exc:
        module(MODULE_ID).provider.open(fake_loop, None, "a1")
    assert "bin.js" in str(exc.value)
    assert engine_host._runtime is None


def test_provider_engine_harness_v3_open_returns_harness_v3_engine_with_stub_runtime(module, fake_loop, db, monkeypatch, settings, profile):
    """With the runtime factory stubbed the provider returns a HarnessV3Engine that is still a HarnessV2Engine."""

    import app.config as app_config
    from app.core.harness_v2_engine import HarnessV2Engine
    from staffdeck_harness.bridge.engine_host import HarnessV3Engine
    from staffdeck_harness.security.profile import install_profile

    install_profile(profile)
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    stub_runtime = SimpleNamespace(name="stub-harness-runtime")
    seen: list = []

    def fake_get_runtime(s):
        seen.append(s)
        return stub_runtime

    monkeypatch.setattr(engine_host, "get_runtime", fake_get_runtime)

    engine = module(MODULE_ID).provider.open(fake_loop, None, "a1")
    assert isinstance(engine, HarnessV3Engine)
    assert isinstance(engine, HarnessV2Engine)
    assert engine.runtime is stub_runtime
    assert seen == [settings], "runtime is resolved from app settings, not from the loop"
    assert engine.owner is fake_loop and engine.db is db
    assert engine.profile.name == "OSS_LOCAL"
    # turn context is captured lazily, at the first frame
    assert engine.snapshot is None and engine.security_context is None and engine.guard is None
    assert engine.composition_compiler is not None


def test_provider_engine_harness_v3_engine_host_falls_back_to_legacy_when_unavailable(module, fake_loop, monkeypatch, settings, empty_root_settings):
    """EngineHost resolves engine.harness_v3 from the registry and falls back to Harness v2 on EngineUnavailable."""

    from app.core.harness_v2_engine import HarnessV2Engine
    from app.session.session_schema import ChatTurnRequest
    from staffdeck_harness.bridge.engine_host import HarnessV3Engine, EngineHost
    from staffdeck_harness.modules import registry as registry_mod

    settings.harness_v3_enabled = True
    reg = _guarded_registry(settings)
    monkeypatch.setattr(registry_mod, "_active", reg)
    request = ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi")

    settings.harness_v3_fallback_to_v2 = True
    engine = EngineHost(settings).open(fake_loop, request, "a1")
    assert type(engine) is HarnessV2Engine and not isinstance(engine, HarnessV3Engine)

    settings.harness_v3_fallback_to_v2 = False
    with pytest.raises(EngineUnavailable):
        EngineHost(settings).open(fake_loop, request, "a1")


# --------------------------------------------------------------------------- 5. events / pep


def test_events_or_pep_engine_harness_v3_staff_use_denies_cross_tenant(registry, guard, security_ctx):
    d = _described(registry)
    assert d["policy_actions"] == ["staff.use/v1"]
    for op in d["policy_actions"]:
        assert op in DEFAULT_ACTION_MAP
    assert DEFAULT_ACTION_MAP["staff.use/v1"] == ("use", "agent")

    g = guard(MODULE_ID)
    foreign = ResourceRef(type="agent", id="a1", tenant_id="t2", attributes={"owner_user_id": "u1"})
    with pytest.raises(PermissionDenied) as exc:
        g.require(security_ctx(tenant_id="t1"), "staff.use/v1", foreign)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details["operation"] == "staff.use/v1"
    assert exc.value.details["resource_type"] == "agent"
    assert exc.value.details["profile"] == "OSS_LOCAL"

    own = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "u1"})
    assert g.require(security_ctx(tenant_id="t1"), "staff.use/v1", own).allowed


def test_events_or_pep_engine_harness_v3_turn_context_enforces_staff_use(module, fake_loop, db, monkeypatch, settings, profile):
    """``HarnessV3Engine._ensure_turn_context`` runs the staff.use/v1 check: a foreign-tenant request is denied before any engine call."""

    from app.db.models import ChatSession
    from app.session.session_schema import ChatTurnRequest
    from staffdeck_harness.bridge.engine_host import HarnessV3Engine

    monkeypatch.setattr(engine_host, "get_runtime", lambda s: SimpleNamespace())
    import app.config as app_config

    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    engine = module(MODULE_ID).provider.open(fake_loop, None, "a1")
    assert isinstance(engine, HarnessV3Engine)
    engine.profile = profile

    # a1 belongs to t1 and is owned by u1; user u1 acting inside t1 is allowed …
    session = ChatSession(id="s1", tenant_id="t1", agent_id="a1")
    engine._ensure_turn_context(ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi"), session)
    assert engine.snapshot is not None and engine.snapshot.staff_id == "a1"
    assert engine.security_context.tenant_id == "t1"
    assert fake_loop.events.recorded and fake_loop.events.recorded[0][2] == "composition_snapshot_compiled"
    assert fake_loop.events.recorded[0][3]["execution_engine"] == "harness_v3"

    # … while a principal from another tenant is denied by the same guard
    engine.snapshot = None
    foreign = SimpleNamespace(tenant_id="t2", principal_id="u9", tenant_role="member")
    engine.profile = SimpleNamespace(
        name=profile.name,
        pep=profile.pep,
        identity=SimpleNamespace(from_user=lambda user, channel=None: security_ctx_for(foreign), from_service=profile.identity.from_service),
    )
    with pytest.raises(PermissionDenied):
        engine._ensure_turn_context(ChatTurnRequest(tenant_id="t1", user_id="u1", agent_id="a1", message="hi"), session)


def security_ctx_for(ns):
    from staffdeck_harness.contracts.security import SecurityContext

    return SecurityContext(principal_id=ns.principal_id, tenant_id=ns.tenant_id, principal_type="user", tenant_role=ns.tenant_role)


def test_harness_v3_process_follows_model_config_thinking_policy():
    """A ModelConfig with thinking disabled must not let the engine default to reasoningEffort=high
    (OpenAI-compatible gateways such as Qwen reject it), and the policy must reach the profile patch."""

    from types import SimpleNamespace

    from staffdeck_harness.bridge.task_agent import _model_thinking
    from staffdeck_harness.bridge.worker import render_patch

    disabled = SimpleNamespace(legacy_extra_body={"thinking": {"type": "disabled"}}, protocol_options={})
    assert _model_thinking(disabled) == ("disabled", "off")
    enabled = SimpleNamespace(legacy_extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "medium"}, protocol_options={})
    assert _model_thinking(enabled) == ("enabled", "high")
    via_protocol = SimpleNamespace(legacy_extra_body={}, protocol_options={"thinking": {"type": "disabled"}})
    assert _model_thinking(via_protocol) == ("disabled", "off")
    unset = SimpleNamespace(legacy_extra_body={}, protocol_options={})
    assert _model_thinking(unset) == ("", "")
    patch = render_patch(mcp_url="http://127.0.0.1:1/mcp")
    assert "thinking: !!js process.env.STAFFDECK_MODEL_THINKING" in patch
    assert "reasoningEffort: !!js process.env.STAFFDECK_MODEL_REASONING_EFFORT" in patch


def test_event_log_labels_whole_turn_with_engine():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.observability.event_log import EventLog

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        log = EventLog(db)
        assert log.record("t1", "s1", "user_message_received", {"execution_engine": "harness_v2"}).payload_json["execution_engine"] == "harness_v2"
        log.execution_engine = "harness_v3"
        assert log.record("t1", "s1", "user_message_received", {"execution_engine": "harness_v2"}).payload_json["execution_engine"] == "harness_v3"
        assert log.record("t1", "s1", "stream_status", {}).payload_json["execution_engine"] == "harness_v3"
        # an explicit non-legacy label is left alone
        assert log.record("t1", "s1", "x", {"execution_engine": "custom"}).payload_json["execution_engine"] == "custom"


def test_harness_v3_process_uses_bridge_gateway_not_provider_credentials(module, fake_loop, db, monkeypatch, settings, profile):
    """The subprocess is pointed at the bridge's model gateway with the activation token as its key;
    the provider base_url / api key stay in StaffDeck."""

    from types import SimpleNamespace

    from staffdeck_harness.bridge import task_agent as ta

    captured = {}

    class _Proc:
        def __init__(self, cfg, *, activation_token, mcp_url, cwd):
            captured["cfg"] = cfg
            captured["token"] = activation_token
            raise RuntimeError("stop here")  # we only need the config

    monkeypatch.setattr(ta, "HarnessV3Process", _Proc)
    runtime = SimpleNamespace(
        worker_config=SimpleNamespace(harness_v3_root="/x", harness_v3_home=__import__("pathlib").Path("/tmp/harness-home-test"), node_bin="node", permission_mode="danger-full-access", initialize_timeout_seconds=1, request_timeout_seconds=1),
        registry=__import__("staffdeck_harness.bridge.capability_mcp", fromlist=["ActivationRegistry"]).ActivationRegistry(),
        mcp_url="http://127.0.0.1:1/mcp", model_base_url="http://127.0.0.1:1/v1",
    )
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.composition.staff import project_staff
    from staffdeck_harness.contracts.security import SecurityContext
    from staffdeck_harness.security.profile import Guard

    staff = project_staff(db, "t1", "a1")
    snap = CompositionCompiler().compile(staff, generation=1)
    ctx = SecurityContext(principal_id="u1", tenant_id="t1", principal_type="user", tenant_role="member")
    turn = ta.HarnessV3TurnContext(db=db, snapshot=snap, security_context=ctx, guard=Guard("t", profile), tenant_id="t1", agent_id="a1", user_id="u1", session_id="s1", turn_id="turn1", channel="web", run_id="run1", task_frame_id="tf1")
    agent = ta.HarnessV3TaskAgent(runtime, turn)
    model_config = SimpleNamespace(id="m1", model="qwen-x", base_url="https://real-provider/v1", api_key_encrypted="enc", legacy_extra_body={"thinking": {"type": "disabled"}}, protocol_options={}, max_output_tokens=512, temperature=0.1)
    from app.core.task_request_compiler import TaskRequirement

    requirement = TaskRequirement(task_frame_id="tf1", kind="conversation", goal="回答问题")
    result = agent.run(requirement, model_config, lambda *a, **k: None, max_actions=3, trace_sink=lambda e, p: None, is_cancelled=lambda: False)
    assert result.status == "failed"
    cfg = captured["cfg"]
    assert cfg.model_base_url == "http://127.0.0.1:1/v1" and cfg.model_api_key == captured["token"]
    assert cfg.model_api_key != "enc" and "real-provider" not in str(cfg.model_base_url)
    assert cfg.model == "qwen-x" and cfg.thinking == "disabled" and cfg.reasoning_effort == "off"
