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
    settings.harness_v3_staff_allowlist set     → only listed agent ids run on DSH

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


class HarnessV3Engine(HarnessV2Engine):
    """HarnessV2Engine with the Harness v3 engine as the step executor."""

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

    # -- turn-level capture ------------------------------------------------------------

    def run(self, request: ChatTurnRequest):  # type: ignore[override]
        self._request = request
        return super().run(request)

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
        self.task_agent = HarnessV3TaskAgent(self.runtime, turn)  # type: ignore[assignment]
        return super()._run_frame(request, session, row, frame, active_skill, model_config, memory_context, prior_frame_results, max_actions)


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
        """Per-staff persisted choice > allowlist > deployment default (see api.admin.effective_engine_for)."""

        from staffdeck_harness.api.admin import effective_engine_for

        row = None
        if db is not None and agent_id and hasattr(db, "get"):
            from app.db.models import AgentProfile

            row = db.get(AgentProfile, agent_id)
        return effective_engine_for(self.settings, row) == "harness_v3"

    def open(self, loop: Any, request: ChatTurnRequest, agent_id: str | None) -> HarnessV2Engine:
        if not self.selects_harness_v3(request, agent_id, db=getattr(loop, "db", None)):
            return HarnessV2Engine(loop)
        from staffdeck_harness.contracts.manifest import SlotName
        from staffdeck_harness.modules.registry import get_registry

        installed = get_registry(self.settings).provider(SlotName.RUNTIME_ENGINE)
        try:
            if installed is not None and callable(getattr(installed.provider, "open", None)):
                return installed.provider.open(loop, request, agent_id)
            return HarnessV3Engine(loop, runtime=get_runtime(self.settings))
        except EngineUnavailable as exc:
            fallback = str(_settings_value(self.settings, "harness_v3_fallback_to_v2", "true")).lower() in {"1", "true", "yes", "on"}
            if fallback:
                logger.warning("Harness v3 unavailable (%s); falling back to Harness v2", exc)
                return HarnessV2Engine(loop)
            raise
