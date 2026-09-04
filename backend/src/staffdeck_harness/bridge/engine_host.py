"""EngineHost: selects the execution engine for a turn without touching legacy code.

``HarnessV3Engine`` *is* a ``HarnessV2Engine`` — it inherits the whole outer turn
machinery (claim, planner, TaskFrame store, leases, SOP CAS, handoff, memory,
response generation) and swaps exactly one collaborator: ``self.task_agent``.
``_run_frame`` calls ``self.task_agent.run(requirement, model_config,
invoker.invoke, ...)`` and that call now lands on ``HarnessV3TaskAgent``.

The per-frame turn context the Harness v3 agent needs (snapshot, security context,
ids) is captured by wrapping ``_run_frame``: we compile the
``CompositionSnapshot`` once per turn (bindings take effect next turn), build
the ``SecurityContext`` from the acting user, and stash them on the engine
before delegating to the inherited implementation.

``EngineHost.open(loop, request)`` returns the engine for this turn:

    settings.harness_v3_enabled is False        → HarnessV2Engine (legacy, default)
    settings.harness_v3_enabled is True         → HarnessV3Engine, unless the Staff is
                                            excluded by ``harness_v3_staff_allowlist``
    settings.harness_v3_staff_allowlist set     → only listed agent ids run on Harness v3

That gives the "single Staff canary" rollout from the plan.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from sqlmodel import Session


from app.core.harness_v2_engine import HarnessV2Engine
from app.db.models import ChatSession, HarnessTaskFrameRecord, Skill, User
from app.session.session_schema import ChatTurnRequest
from staffdeck_harness.bridge.task_agent import HarnessV3Runtime, HarnessV3TaskAgent, HarnessV3TurnContext
from staffdeck_harness.bridge.worker import HarnessV3WorkerConfig
from staffdeck_harness.composition.compiler import CompositionCompiler, CompositionSnapshot
from staffdeck_harness.composition.staff import project_staff
from staffdeck_harness.contracts.errors import EngineUnavailable
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.security.profile import Guard, get_profile

logger = logging.getLogger(__name__)

_runtime_lock = threading.Lock()
_runtime: HarnessV3Runtime | None = None


def _settings_value(settings: Any, name: str, default: Any = None) -> Any:
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    return os.environ.get(name.upper(), default)


def get_runtime(settings: Any) -> HarnessV3Runtime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            root = Path(str(_settings_value(settings, "harness_v3_root", "") or "")).expanduser()
            if not str(root):
                raise EngineUnavailable("harness_v3_root is not configured")
            home = Path(str(_settings_value(settings, "harness_v3_home", "") or "") or (root / ".staffdeck-harness-home")).expanduser()
            cfg = HarnessV3WorkerConfig(
                harness_v3_root=root,
                harness_v3_home=home,
                node_bin=str(_settings_value(settings, "harness_v3_node_bin", "node") or "node"),
                permission_mode=str(_settings_value(settings, "harness_v3_permission_mode", "danger-full-access") or "danger-full-access"),
                initialize_timeout_seconds=float(_settings_value(settings, "harness_v3_initialize_timeout_seconds", 90.0) or 90.0),
                request_timeout_seconds=float(_settings_value(settings, "harness_v3_request_timeout_seconds", 600.0) or 600.0),
            )
            cfg.validate()
            rt = HarnessV3Runtime(worker_config=cfg)
            rt.start()
            _runtime = rt
        return _runtime


def reset_runtime() -> None:
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.stop()
        _runtime = None


def get_settings_safe() -> Any:
    try:
        from app.config import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001
        return None


def _has_image_attachments(request: Any) -> bool:
    for att in getattr(request, "attachments", None) or ():
        kind = getattr(att, "kind", None) if not isinstance(att, dict) else att.get("kind")
        data_url = getattr(att, "data_url", None) if not isinstance(att, dict) else att.get("data_url")
        if kind == "image" and data_url:
            return True
    return False


class HarnessV3Engine(HarnessV2Engine):
    """The v2 turn skeleton with every model stage — planning, each step, the reply — on Harness v3.

    ``HarnessV2Engine`` keeps what is not a model decision: turn claim, leases, TaskFrame
    scheduling, SOP state machine, handoff, memory capture. What *is* a model decision now runs
    on one pooled engine process per turn:

    - ``self.planner``                 → ``EngineTurnPlanner`` (same prompt/contract/normalize as v2)
    - ``self.task_agent`` (per frame)  → ``HarnessV3TaskAgent`` sharing the turn's process
    - ``owner.response_generator``     → ``EngineResponseGenerator`` (only when >1 frame needs
                                         synthesis; a lone frame's finish_task reply is final)

    The process is checked out on the first model stage and returned in ``run()``'s ``finally``;
    the activation token behind it is rebound per phase (see ``bridge/phases``).
    """

    def __init__(self, owner: Any, *, runtime: HarnessV3Runtime, profile: Any = None, compiler: CompositionCompiler | None = None) -> None:
        super().__init__(owner)
        self.runtime = runtime
        self.profile = profile or get_profile(getattr(runtime, "settings", None) or get_settings_safe())
        # HarnessV2Engine already owns ``self.compiler`` (TaskRequestCompiler).
        self.composition_compiler = compiler or CompositionCompiler()
        self.snapshot: CompositionSnapshot | None = None
        self.security_context: SecurityContext | None = None
        self.guard: Guard | None = None
        self._request: ChatTurnRequest | None = None
        self._memory_context: list[dict[str, Any]] = []
        self._pooled: Any = None
        self._v2_planner = self.planner
        self._v2_response_generator: Any = None
        # Lazily-bound phase planner: it needs the pooled process, which needs the model config,
        # which the v2 turn resolves after ``run()`` starts. ``_LazyPlanner`` defers to ``_plan``.
        self.planner = _LazyPlanner(self)  # type: ignore[assignment]

    # -- turn-level capture ------------------------------------------------------------

    def run(self, request: ChatTurnRequest):  # type: ignore[override]
        self._request = request
        owner = self.owner
        self._v2_response_generator = getattr(owner, "response_generator", None)
        # Memory is a registry provider on the v3 path: re-point owner.memory at the facade for
        # this turn. The v2 engine reads ``owner.memory.context_memories(...)`` — that call now
        # goes to the runtime.memory provider (or recalls nothing when memory is disabled).
        v2_memory = getattr(owner, "memory", None)
        # Label the turn v3 before any event fires (memory_recalled happens before the frame
        # loop reaches _ensure_turn_context on the non-stream path).
        if hasattr(self.events, "execution_engine"):
            self.events.execution_engine = "harness_v3"
        try:
            from staffdeck_harness.memory import ProviderMemoryFacade, resolve_memory_provider

            owner.memory = ProviderMemoryFacade(self.db, resolve_memory_provider())
        except Exception:  # pragma: no cover - a broken memory module must not take the turn down
            logger.exception("memory provider could not be resolved; running the turn without memory")
        try:
            return super().run(request)
        finally:
            if v2_memory is not None:
                owner.memory = v2_memory
            if self._v2_response_generator is not None:
                owner.response_generator = self._v2_response_generator
            if self._pooled is not None:
                try:
                    self.runtime.release_process(self._pooled)
                except Exception:  # pragma: no cover
                    logger.exception("releasing the turn's engine process failed")
                self._pooled = None

    # -- the engine process for this turn -----------------------------------------------

    def _turn_process(self, request: ChatTurnRequest, session: ChatSession, model_config: Any) -> Any:
        """Check out (once per turn) the pooled process every phase of this turn runs on."""

        if self._pooled is not None:
            return self._pooled
        self._ensure_turn_context(request, session)
        from staffdeck_harness.bridge.task_agent import _model_thinking
        from staffdeck_harness.bridge.worker import HarnessV3WorkerConfig
        from staffdeck_harness.capabilities.facade import harness_task_workspace_path

        model = str(getattr(model_config, "model", "") or "")
        thinking, effort = _model_thinking(model_config)
        wc = self.runtime.worker_config
        cfg = HarnessV3WorkerConfig(
            harness_v3_root=wc.harness_v3_root, harness_v3_home=wc.harness_v3_home / request.tenant_id, node_bin=wc.node_bin,
            model=model, model_base_url=self.runtime.model_base_url, thinking=thinking, reasoning_effort=effort,
            permission_mode=wc.permission_mode, initialize_timeout_seconds=wc.initialize_timeout_seconds, request_timeout_seconds=wc.request_timeout_seconds,
        )
        cwd = harness_task_workspace_path(tenant_id=request.tenant_id, session_id=session.id, task_frame_id=f"turn-{self.user_message_id or session.id}", db=self.db)
        cwd.mkdir(parents=True, exist_ok=True)
        import time as _time

        t0 = _time.monotonic()
        self._pooled = self.runtime.acquire_process(cfg, request.tenant_id, cwd=cwd)
        pooled = self._pooled
        self.events.record(request.tenant_id, session.id, "harness_v3_process_started", {
            "model": model, "model_config_id": getattr(model_config, "id", None), "base_url": str(getattr(model_config, "base_url", "") or ""),
            "via": "staffdeck-model-gateway", "thinking": thinking or "provider_default", "reasoning_effort": effort or "provider_default",
            "workspace": str(cwd), "boot_ms": int((_time.monotonic() - t0) * 1000), "pooled": pooled.uses > 1, "process_uses": pooled.uses, "execution_engine": "harness_v3",
        })
        return self._pooled

    def _phase_runner(self, request: ChatTurnRequest, session: ChatSession, model_config: Any) -> Any:
        from staffdeck_harness.bridge.phases import EnginePhaseRunner

        pooled = self._turn_process(request, session, model_config)

        def trace(event: str, payload: dict[str, Any]) -> None:
            self.events.record(request.tenant_id, session.id, event, {**payload, "execution_engine": "harness_v3"})

        return EnginePhaseRunner(self.runtime, pooled, tenant_id=request.tenant_id, session_id=session.id, trace=trace, cancelled=lambda: self._is_cancelled(request, session))

    def _plan(self, message: str, session: ChatSession, available_skills: list[Any], model_config: Any, *args: Any, **kwargs: Any) -> Any:
        """Turn planning on the engine. Falls back to the v2 planner if the engine cannot be reached
        *before* any model work happened (so a dead engine degrades, not crashes, the turn)."""

        request = self._request
        assert request is not None
        try:
            runner = self._phase_runner(request, session, model_config)
        except EngineUnavailable as exc:
            fallback = str(_settings_value(self.runtime_settings(), "harness_v3_fallback_to_v2", "true")).lower() in {"1", "true", "yes", "on"}
            if not fallback:
                raise
            _note_fallback(self.owner, request, "engine_unavailable", detail=str(exc))
            return self._v2_planner.plan(message, session, available_skills, model_config, *args, **kwargs)
        from staffdeck_harness.bridge.phases import EngineResponseGenerator, EngineTurnPlanner

        engine_session = f"sd-{session.id}-{self.user_message_id or 'turn'}"
        # Reply synthesis (if the turn needs it) runs on the same process.
        if self._v2_response_generator is not None:
            self.owner.response_generator = EngineResponseGenerator(self._v2_response_generator, runner, engine_session=engine_session)
        return EngineTurnPlanner(runner, engine_session=engine_session).plan(message, session, available_skills, model_config, *args, **kwargs)

    def runtime_settings(self) -> Any:
        return getattr(self.runtime, "settings", None) or get_settings_safe()

    def _ensure_turn_context(self, request: ChatTurnRequest, session: ChatSession) -> None:
        if self.snapshot is not None:
            return
        staff = project_staff(self.db, request.tenant_id, session.agent_id)
        self.snapshot = self.composition_compiler.compile(staff, generation=0, metadata={"turn_id": self.user_message_id, "engine": "harness_v3"})
        user = self.db.get(User, request.user_id) if request.user_id else None
        if user is not None:
            self.security_context = self.profile.identity.from_user(user, channel=request.channel)
        else:
            self.security_context = self.profile.identity.from_service(f"channel:{request.channel}", request.tenant_id)
        self.guard = Guard("staffdeck.runtime", self.profile)
        # Every event of this turn (also the ones legacy code stamps "harness_v2") belongs to Harness v3.
        if hasattr(self.events, "execution_engine"):
            self.events.execution_engine = "harness_v3"
        # Staff-level PEP: may this principal use this staff at all?
        from staffdeck_harness.contracts.security import ResourceRef

        self.guard.require(self.security_context, "staff.use/v1", ResourceRef(type="agent", id=staff.staff_id, tenant_id=staff.tenant_id, attributes=dict(staff.ref.attributes)))
        self.events.record(
            request.tenant_id,
            session.id,
            "composition_snapshot_compiled",
            {
                "snapshot_id": self.snapshot.snapshot_id,
                "staff_id": self.snapshot.staff_id,
                "grants": len(self.snapshot.grants),
                "sops": [s.skill_id for s in self.snapshot.sops],
                "security_profile": self.profile.name,
                "execution_engine": "harness_v3",
            },
        )

    # -- the seam -----------------------------------------------------------------------

    def _run_frame(  # type: ignore[override]
        self,
        request: ChatTurnRequest,
        session: ChatSession,
        row: HarnessTaskFrameRecord,
        frame: Any,
        active_skill: Skill | None,
        model_config: Any,
        memory_context: list[dict[str, object]],
        prior_frame_results: list[dict[str, Any]],
        max_actions: int,
    ):
        self._ensure_turn_context(request, session)
        assert self.snapshot is not None and self.security_context is not None and self.guard is not None
        turn = HarnessV3TurnContext(
            db=self.db,
            snapshot=self.snapshot,
            security_context=self.security_context,
            guard=self.guard,
            tenant_id=request.tenant_id,
            agent_id=session.agent_id or self.snapshot.staff_id,
            user_id=request.user_id or "",
            session_id=session.id,
            turn_id=self.user_message_id or request.client_turn_id or session.id,
            channel=request.channel,
            run_id="",
            task_frame_id=row.task_id,
            memory_context=[dict(m) for m in memory_context],
            session_slots=dict(session.slots_json or {}),
            generation=0,
            attachments_text="",
            client_turn_id=request.client_turn_id,
            run_id_provider=lambda: self.active_run_id or "",
        )
        pooled = self._turn_process(request, session, model_config)
        self.task_agent = HarnessV3TaskAgent(self.runtime, turn, pooled=pooled)  # type: ignore[assignment]
        return super()._run_frame(request, session, row, frame, active_skill, model_config, memory_context, prior_frame_results, max_actions)


class _LazyPlanner:
    """``HarnessV2Engine.run`` calls ``self.planner.plan(...)``; route it to the engine-backed planner."""

    def __init__(self, engine: HarnessV3Engine) -> None:
        self._engine = engine

    def plan(self, *args: Any, **kwargs: Any) -> Any:
        return self._engine._plan(*args, **kwargs)


class EngineHost:
    """Startup-level engine selection. Not a per-Staff plugin."""

    def __init__(self, settings: Any):
        self.settings = settings

    @property
    def harness_v3_enabled(self) -> bool:
        value = _settings_value(self.settings, "harness_v3_enabled", False)
        return str(value).lower() in {"1", "true", "yes", "on"} if not isinstance(value, bool) else value

    def allowlist(self) -> set[str]:
        raw = str(_settings_value(self.settings, "harness_v3_staff_allowlist", "") or "")
        return {x.strip() for x in raw.split(",") if x.strip()}

    def selects_harness_v3(self, request: ChatTurnRequest, agent_id: str | None, *, db: Session | None = None) -> bool:
        """Per-staff persisted choice > allowlist > deployment default (see api.admin.effective_engine_for).

        Turns carrying image attachments always run on Harness v2: the Harness v3 engine's model
        adapter cannot carry image content through the bridge's gateway, so the turn would fail
        instead of seeing the picture. Harness v2's multimodal path is intact. This is decided up
        front so the very first events of the turn are labelled with the engine that runs it.
        """

        if _has_image_attachments(request):
            return False
        return self._staff_wants_harness_v3(agent_id, db=db)

    def _staff_wants_harness_v3(self, agent_id: str | None, *, db: Session | None = None) -> bool:
        from staffdeck_harness.api.admin import effective_engine_for

        row = None
        if db is not None and agent_id and hasattr(db, "get"):
            from app.db.models import AgentProfile

            row = db.get(AgentProfile, agent_id)
        return effective_engine_for(self.settings, row) == "harness_v3"

    def engine_fallback_reason(self, request: ChatTurnRequest, agent_id: str | None, *, db: Session | None = None) -> str | None:
        """Why a turn that *would* select Harness v3 is routed to v2 anyway (None when it is not)."""

        if _has_image_attachments(request) and self._staff_wants_harness_v3(agent_id, db=db):
            return "image_attachments"
        return None

    def open(self, loop: Any, request: ChatTurnRequest, agent_id: str | None) -> HarnessV2Engine:
        db = getattr(loop, "db", None)
        if not self.selects_harness_v3(request, agent_id, db=db):
            reason = self.engine_fallback_reason(request, agent_id, db=db)
            if reason:
                _note_fallback(loop, request, reason, detail="本轮带图片附件，Harness v3 引擎不读取图片，已改用 Harness v2")
            return HarnessV2Engine(loop)
        from staffdeck_harness.modules.registry import get_registry

        # The staff (or the deployment default) chose Harness v3. Resolve the *v3* engine module by
        # id rather than "the active RUNTIME_ENGINE provider": when the deployment default is v2 the
        # registry's active provider is engine.harness_v2, yet a per-staff canary must still run v3.
        installed = get_registry(self.settings).get("engine.harness_v3")
        try:
            if installed is not None and callable(getattr(installed.provider, "open", None)):
                return installed.provider.open(loop, request, agent_id)
            return HarnessV3Engine(loop, runtime=get_runtime(self.settings))
        except EngineUnavailable as exc:
            fallback = str(_settings_value(self.settings, "harness_v3_fallback_to_v2", "true")).lower() in {"1", "true", "yes", "on"}
            if fallback:
                logger.warning("Harness v3 unavailable (%s); falling back to Harness v2", exc)
                # Not silent: the turn gets an event the execution log shows, and the admin
                # console's runtime status carries the count + last reason.
                _note_fallback(loop, request, "engine_unavailable", detail=exc.message if hasattr(exc, "message") else str(exc))
                return HarnessV2Engine(loop)
            raise


# --------------------------------------------------------------------------- fallback visibility

_fallback_lock = threading.Lock()
_fallback_count = 0
_last_fallback: dict[str, Any] | None = None


def _note_fallback(loop: Any, request: Any, reason: str, *, detail: str) -> None:
    """Record that a turn which wanted Harness v3 ran on Harness v2, and why."""

    global _fallback_count, _last_fallback
    from datetime import datetime, timezone

    entry = {"reason": reason, "detail": str(detail)[:500], "at": datetime.now(timezone.utc).isoformat(), "session_id": getattr(request, "session_id", None), "agent_id": getattr(request, "agent_id", None)}
    with _fallback_lock:
        _fallback_count += 1
        _last_fallback = entry
    events = getattr(loop, "events", None)
    if events is not None and hasattr(events, "execution_engine"):
        events.execution_engine = None  # the turn really runs on v2; do not label it v3
    record = getattr(events, "record", None)
    tenant_id, session_id = getattr(request, "tenant_id", None), getattr(request, "session_id", None)
    if callable(record) and tenant_id and session_id:
        try:
            record(tenant_id, session_id, "harness_v3_fallback", {"reason": reason, "detail": entry["detail"], "execution_engine": "harness_v2"})
        except Exception:  # pragma: no cover - visibility must never break a turn
            logger.exception("could not record harness_v3_fallback event")


def fallback_state() -> dict[str, Any]:
    with _fallback_lock:
        return {"fallback_count": _fallback_count, "last_fallback": dict(_last_fallback) if _last_fallback else None}


def reset_fallback_state() -> None:
    global _fallback_count, _last_fallback
    with _fallback_lock:
        _fallback_count, _last_fallback = 0, None
