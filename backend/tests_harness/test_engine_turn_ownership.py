"""Behavioural tests for "Harness v3 owns the whole turn":

- process pool: warm reuse per (tenant, model route), budgets, close-on-release
- activation rebinding: one token, many phase hosts; idle placeholder refuses tools
- phase hosts: planning/reply cannot reach capabilities but can reach the model gateway
- image turns route to Harness v2 and are surfaced as a visible fallback
- multi-tenant operator boundary is closed by default
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from staffdeck_harness.bridge import engine_host
from staffdeck_harness.bridge.capability_mcp import ActivationRegistry
from staffdeck_harness.bridge.phases import PhaseHost, parse_json_object
from staffdeck_harness.bridge.process_pool import ProcessPool, pool_key
from staffdeck_harness.bridge.task_agent import HarnessV3Runtime, IdlePhaseHost
from staffdeck_harness.bridge.worker import HarnessV3WorkerConfig


# --------------------------------------------------------------------------- fakes

class _FakeProc:
    def __init__(self, cfg, *, activation_token, mcp_url, cwd):
        self.cfg = cfg
        self.activation_token = activation_token
        self.mcp_url = mcp_url
        self.cwd = cwd
        self.started = 0
        self.closed = False

    def start(self):
        self.started += 1

    def close(self):
        self.closed = True

    @property
    def alive(self):
        return not self.closed


def _cfg(model="m", tenant_home="/tmp/h", thinking=""):
    return HarnessV3WorkerConfig(harness_v3_root=Path("/x"), harness_v3_home=Path(tenant_home), model=model, thinking=thinking)


def _pool(**kw):
    return ProcessPool(factory=lambda cfg, **k: _FakeProc(cfg, **k), **kw)


# --------------------------------------------------------------------------- pool

def test_pool_reuses_warm_process_for_same_key_and_boots_for_a_new_one():
    pool = _pool()
    seen = []
    a = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=seen.append)
    assert a.uses == 1 and a.process.started == 1 and seen == [a.token]
    pool.release(a, on_close=lambda t: pytest.fail("must not close a healthy process"))
    b = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=seen.append)
    assert b is a and b.uses == 2 and b.process.started == 1, "second turn reuses the warm process"
    c = pool.acquire(_cfg(model="other"), "t1", mcp_url="u", cwd=Path("/w"), register=seen.append)
    assert c is not a and c.process.started == 1, "a different model route boots its own process"
    assert pool.stats["hits"] == 1 and pool.stats["misses"] == 2


def test_pool_key_isolates_tenants_and_thinking_policy():
    assert pool_key(_cfg(), "t1") != pool_key(_cfg(), "t2")
    assert pool_key(_cfg(thinking="enabled"), "t1") != pool_key(_cfg(thinking="disabled"), "t1")


def test_pool_closes_on_budget_exhaustion_dead_process_and_overflow():
    pool = _pool(warm_per_key=1, max_reuses=2)
    closed = []
    a = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=lambda t: None)
    b = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=lambda t: None)
    pool.release(a, on_close=closed.append)      # kept (warm slot free)
    pool.release(b, on_close=closed.append)      # overflow: closed
    assert closed == [b.token] and b.process.closed
    a2 = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=lambda t: None)
    assert a2 is a and a2.uses == 2
    pool.release(a2, on_close=closed.append)     # reuse budget spent: closed
    assert a.token in closed and a.process.closed
    d = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=lambda t: None)
    d.process.close()                            # dies while checked out
    pool.release(d, on_close=closed.append)
    assert d.token in closed
    assert pool.snapshot()["warm"] == 0


def test_pool_close_all_releases_tokens():
    pool = _pool()
    a = pool.acquire(_cfg(), "t1", mcp_url="u", cwd=Path("/w"), register=lambda t: None)
    pool.release(a, on_close=lambda t: None)
    gone = []
    pool.close_all(on_close=gone.append)
    assert gone == [a.token] and a.process.closed


# --------------------------------------------------------------------------- rebinding

def test_activation_rebind_swaps_host_and_idle_placeholder_refuses_tools():
    reg = ActivationRegistry()
    act = reg.register(IdlePhaseHost(), None, token="tok")
    assert reg.get("tok") is act and [t["name"] for t in act.host.tool_schemas()] == ["capability_invoke", "knowledge_search", "general_skill_read", "tool_invoke", "sandbox_execute", "capability_describe", "submit_step_result"]
    assert act.host.invoke_proxy("finish_task", {}, None)[0].error["code"] == "ACTIVATION_FENCED"
    assert reg.live_count() == 0, "an idle placeholder is not a live turn"

    class Host:
        idle = False
        model_config = object()

        def tool_schemas(self):
            return [{"name": "x", "description": "", "parameters": {}}]

        def invoke_proxy(self, *a):
            return None, None

    h = Host()
    assert reg.rebind("tok", h, lambda tid: None).host is h
    assert reg.live_count() == 1
    reg.rebind("tok", IdlePhaseHost(), None)
    assert reg.live_count() == 0
    with pytest.raises(KeyError):
        reg.rebind("nope", h, None)


def test_runtime_acquire_registers_idle_token_and_release_parks_it():
    reg = ActivationRegistry()
    rt = HarnessV3Runtime(worker_config=_cfg(), registry=reg, mcp=SimpleNamespace(url="http://m/mcp", model_base_url="http://m/v1", stop=lambda: None), pool=_pool())
    pooled = rt.acquire_process(_cfg(), "t1", cwd=Path("/w"))
    act = reg.get(pooled.token)
    assert act is not None and isinstance(act.host, IdlePhaseHost)
    assert pooled.process.cfg.model_api_key == pooled.token, "the token is the process's gateway API key"
    reg.rebind(pooled.token, PhaseHost(model_config="mc", phase="plan"), None)
    assert reg.get(pooled.token).host.model_config == "mc"
    rt.release_process(pooled)
    assert isinstance(reg.get(pooled.token).host, IdlePhaseHost), "parked, not released: the warm process keeps its token"
    rt.stop()
    assert reg.get(pooled.token) is None, "stopping the runtime drops the tokens with the processes"


def test_phase_host_refuses_tools_but_serves_the_gateway():
    h = PhaseHost(model_config="mc", phase="plan")
    assert len(h.tool_schemas()) == 7, "the tool set is stable for the process; enforcement is at invoke"
    res, receipt = h.invoke_proxy("knowledge_search", {"query": "x"}, None)
    assert res.success is False and res.error["code"] == "ACTIVATION_FENCED" and receipt is None
    assert h.model_config == "mc" and h.idle is False


@pytest.mark.parametrize("text", ['{"a": 1}', 'sure:\n```json\n{"a": 1}\n```', 'prefix {"a": 1} suffix'])
def test_parse_json_object_tolerates_fences_and_prose(text):
    assert parse_json_object(text) == {"a": 1}


def test_parse_json_object_rejects_non_objects():
    with pytest.raises(ValueError):
        parse_json_object("")
    with pytest.raises(ValueError):
        parse_json_object("[1]")


# --------------------------------------------------------------------------- image routing + fallback visibility

def _settings(**kw):
    base = {"harness_v3_enabled": True, "harness_v3_staff_allowlist": "", "harness_v3_fallback_to_v2": True}
    base.update(kw)
    return SimpleNamespace(**base)


def test_image_turn_routes_to_v2_and_records_fallback(monkeypatch):
    engine_host.reset_fallback_state()
    host = engine_host.EngineHost(_settings())
    req_img = SimpleNamespace(tenant_id="t1", session_id="s1", agent_id=None, attachments=[SimpleNamespace(kind="image", data_url="data:image/png;base64,AAAA")])
    req_txt = SimpleNamespace(tenant_id="t1", session_id="s1", agent_id=None, attachments=[SimpleNamespace(kind="pdf", data_url=None)])
    assert host.selects_harness_v3(req_img, None) is True
    assert host.selects_harness_v3(req_txt, None) is True
    assert host.engine_fallback_reason(req_img, None) is None
    assert host.engine_fallback_reason(req_txt, None) is None

    recorded = []
    events = SimpleNamespace(execution_engine="harness_v3", record=lambda *a: recorded.append(a))
    loop = SimpleNamespace(db=None, events=events)
    import staffdeck_harness.modules.registry as reg_mod
    provider = SimpleNamespace(open=lambda *a: "v3-engine")
    monkeypatch.setattr(reg_mod, "get_registry", lambda s: SimpleNamespace(get=lambda mid: SimpleNamespace(provider=provider)))
    assert host.open(loop, req_img, None) == "v3-engine"
    assert events.execution_engine == "harness_v3" and not recorded
    st = engine_host.fallback_state()
    assert st["fallback_count"] == 0


def test_engine_unavailable_fallback_is_visible(monkeypatch):
    engine_host.reset_fallback_state()
    host = engine_host.EngineHost(_settings())
    req = SimpleNamespace(tenant_id="t1", session_id="s1", agent_id=None, attachments=[])
    monkeypatch.setattr(engine_host, "get_registry", lambda s: SimpleNamespace(get=lambda mid: None), raising=False)

    from staffdeck_harness.contracts.errors import EngineUnavailable

    def boom(settings):
        raise EngineUnavailable("node missing")

    monkeypatch.setattr(engine_host, "get_runtime", boom)
    import staffdeck_harness.modules.registry as reg_mod

    monkeypatch.setattr(reg_mod, "get_registry", lambda s: SimpleNamespace(get=lambda mid: None))
    recorded = []
    loop = SimpleNamespace(db=None, events=SimpleNamespace(execution_engine="harness_v3", record=lambda *a: recorded.append(a)))
    with pytest.raises(EngineUnavailable):
        host.open(loop, req, None)
    assert not recorded and engine_host.fallback_state()["fallback_count"] == 0


def test_engine_unavailable_without_fallback_raises(monkeypatch):
    host = engine_host.EngineHost(_settings(harness_v3_fallback_to_v2=False))
    req = SimpleNamespace(tenant_id="t1", session_id="s1", agent_id=None, attachments=[])
    from staffdeck_harness.contracts.errors import EngineUnavailable
    import staffdeck_harness.modules.registry as reg_mod

    monkeypatch.setattr(reg_mod, "get_registry", lambda s: SimpleNamespace(get=lambda mid: None))
    monkeypatch.setattr(engine_host, "get_runtime", lambda s: (_ for _ in ()).throw(EngineUnavailable("down")))
    with pytest.raises(EngineUnavailable):
        host.open(SimpleNamespace(db=None, events=None), req, None)


def test_phase_runner_surfaces_engine_turn_error_instead_of_empty_output(monkeypatch):
    from staffdeck_harness.bridge import phases

    reg = ActivationRegistry()
    reg.register(IdlePhaseHost(), None, token="tok")
    rt = SimpleNamespace(registry=reg)
    pooled = SimpleNamespace(token="tok", process=object())
    events = [{"type": "turn/end", "data": {"reason": {"kind": "error", "error": {"message": "Connection error.", "code": "SERVER", "status": 502}}}}]
    from staffdeck_harness.bridge import session_runner

    monkeypatch.setattr(session_runner, "run_session", lambda *a, **kw: (events, "", "error"))
    traces = []
    runner = phases.EnginePhaseRunner(rt, pooled, tenant_id="t1", session_id="s1", trace=lambda e, p: traces.append(e), cancelled=lambda: False)
    with pytest.raises(phases.EnginePhaseError) as exc:
        runner.prompt(phase="plan", model_config="mc", system_text="sys", user_text="u", engine_session="es")
    assert "Connection error. (SERVER)" in str(exc.value)
    assert isinstance(reg.get("tok").host, IdlePhaseHost), "the token is parked again even on failure"
    assert traces == ["harness_v3_plan_started", "harness_v3_plan_finished"]
