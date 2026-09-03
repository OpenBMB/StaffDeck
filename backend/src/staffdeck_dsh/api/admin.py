"""Admin API for the DSH pluggable runtime.

Mounted under ``/api/enterprise/dsh``; every route requires a tenant admin.
Lives in the parallel package and is attached to the FastAPI app by
``staffdeck_dsh.runtime.assembly.mount_admin_api`` only when ``dsh_enabled``
or ``dsh_admin_api_enabled`` is set, so a legacy deployment exposes nothing.

Endpoints

    GET  /status                     runtime health, security profile, engine, module counts
    GET  /modules                    the sealed Module Registry (every slot, enabled/guarded)
    GET  /snapshot?agent_id=         compile a CompositionSnapshot for a staff (what the model will see)
    GET  /ledger/unknown             outcome_unknown invocations awaiting reconciliation
    POST /ledger/{id}/reconcile      settle one as completed|failed
    GET  /staff/{agent_id}/engine    which engine this staff runs on
    PUT  /staff/{agent_id}/engine    set legacy|dsh|default for this staff (persisted on AgentProfile.metadata_json)
    GET  /events/recent?session_id=  DSH-related AgentEvents for one session (trace projection)
    GET  /config                     saved vs applied assembly (engine / profile / disabled / extra modules)
    PUT  /config                     save a new assembly (takes effect after /restart)
    POST /restart                    rebuild registry + profile + DSH runtime from the saved assembly
    GET  /sessions/recent            recent chat sessions (for the log picker)
    GET  /log?session_id=            one session's execution log (events + invocation ledger, merged)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.db.models import AgentEvent, AgentProfile, ChatSession, HarnessInvocationRecord, User, utc_now
from app.security.auth import get_current_user
from app.security.permissions import ensure_tenant_admin, ensure_current_user_tenant
from staffdeck_dsh.capabilities.ledger import InvocationLedger
from staffdeck_dsh.composition.compiler import CompositionCompiler
from staffdeck_dsh.composition.staff import project_staff
from staffdeck_dsh.contracts.errors import EngineUnavailable, ModuleSdkError
from staffdeck_dsh.modules.config import ENGINES, SECURITY_PROFILES, RuntimeOverrides, load_overrides, save_overrides
from staffdeck_dsh.modules.registry import get_registry
from staffdeck_dsh.security.profile import get_profile

router = APIRouter(prefix="/api/enterprise/dsh", tags=["enterprise:dsh"], dependencies=[Depends(get_current_user)])

EngineChoice = Literal["default", "legacy", "dsh"]
ENGINE_METADATA_KEY = "execution_engine"


class StatusRead(BaseModel):
    dsh_enabled: bool
    security_profile: str
    default_engine: EngineChoice
    staff_allowlist: list[str]
    fallback_to_legacy: bool
    dsh_root: str
    dsh_home: str
    runtime_ok: bool
    runtime_error: dict[str, Any] | None = None
    mcp_url: str | None = None
    live_activations: int = 0
    modules_total: int
    modules_enabled: int
    registry_generation: int
    started_at: str | None = None
    restart_count: int = 0
    config_pending: bool = False
    engine_version: str | None = None


class ModuleRead(BaseModel):
    module_id: str
    name: str
    summary: str = ""
    version: str
    kind: str
    contract_version: str
    slot: str
    enabled: bool
    source: str
    provides: list[str]
    requires: list[str]
    hooks: list[str]
    policy_actions: list[str]
    guarded: bool


class SnapshotRead(BaseModel):
    snapshot_id: str
    staff_id: str
    persona_preview: str | None
    model_route: dict[str, str]
    session_policy: dict[str, Any]
    grants: list[dict[str, Any]]
    sops: list[dict[str, Any]]
    hooks: dict[str, list[str]]
    channels: list[str]
    team_id: str | None
    proxy_tools: list[str]


class LedgerRowRead(BaseModel):
    id: str
    session_id: str
    task_id: str
    run_id: str
    tool_name: str
    status: str
    side_effect_key: str | None
    arguments: dict[str, Any]
    error: dict[str, Any] | None
    started_at: str
    finished_at: str | None
    engine: str | None


class ReconcileRequest(BaseModel):
    tenant_id: str
    status: Literal["completed", "failed"]
    result: dict[str, Any] | None = None


class StaffEngineRead(BaseModel):
    agent_id: str
    engine: EngineChoice
    effective_engine: Literal["legacy", "dsh"]


class StaffEngineUpdate(BaseModel):
    tenant_id: str
    engine: EngineChoice


def _admin(tenant_id: str, user: User) -> User:
    return ensure_tenant_admin(tenant_id, user)


def _settings_engine(settings: Any) -> EngineChoice:
    return "dsh" if bool(getattr(settings, "dsh_enabled", False)) else "legacy"


def staff_engine_choice(row: AgentProfile) -> EngineChoice:
    value = str((row.metadata_json or {}).get(ENGINE_METADATA_KEY) or "default")
    return value if value in {"default", "legacy", "dsh"} else "default"  # type: ignore[return-value]


def effective_engine_for(settings: Any, row: AgentProfile | None) -> Literal["legacy", "dsh"]:
    """Per-staff override > allowlist > deployment default. Used by EngineHost."""

    if row is not None:
        choice = staff_engine_choice(row)
        if choice != "default":
            return choice
    if not bool(getattr(settings, "dsh_enabled", False)):
        return "legacy"
    allow = {x.strip() for x in str(getattr(settings, "dsh_staff_allowlist", "") or "").split(",") if x.strip()}
    if allow and (row is None or row.id not in allow):
        return "legacy"
    return "dsh"


@router.get("/status", response_model=StatusRead)
def dsh_status(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> StatusRead:
    ensure_current_user_tenant(tenant_id, user)
    settings = get_settings()
    reg = get_registry(settings)
    modules = reg.describe()
    from staffdeck_dsh.runtime.assembly import assembly_state

    state = assembly_state(settings)
    runtime_ok, runtime_error, mcp_url, live = True, None, None, 0
    if bool(getattr(settings, "dsh_enabled", False)):
        try:
            from staffdeck_dsh.bridge.engine_host import get_runtime

            rt = get_runtime(settings)
            mcp_url, live = rt.mcp_url, len(rt.registry)
        except EngineUnavailable as exc:
            runtime_ok, runtime_error = False, exc.to_dict()
    else:
        runtime_ok = False
        runtime_error = {"code": "DSH_DISABLED", "message": "dsh_enabled is false; turns run on the legacy engine"}
    return StatusRead(
        dsh_enabled=bool(getattr(settings, "dsh_enabled", False)),
        security_profile=get_profile(settings).name,
        default_engine=_settings_engine(settings),
        staff_allowlist=[x.strip() for x in str(getattr(settings, "dsh_staff_allowlist", "") or "").split(",") if x.strip()],
        fallback_to_legacy=bool(getattr(settings, "dsh_fallback_to_legacy", True)),
        dsh_root=str(getattr(settings, "dsh_root", "") or ""),
        dsh_home=str(getattr(settings, "dsh_home", "") or ""),
        runtime_ok=runtime_ok,
        runtime_error=runtime_error,
        mcp_url=mcp_url,
        live_activations=live,
        modules_total=len(modules),
        modules_enabled=sum(1 for m in modules if m["enabled"]),
        registry_generation=reg.generation,
        started_at=state.get("started_at"),
        restart_count=int(state.get("restart_count") or 0),
        config_pending=bool(state.get("pending")),
        engine_version=_engine_version(reg),
    )


def _engine_version(reg: Any) -> str | None:
    item = reg.get("dsh.core")
    if item is None or not callable(getattr(item.provider, "version", None)):
        return None
    try:
        return str(item.provider.version())
    except Exception:  # noqa: BLE001
        return None


@router.get("/modules", response_model=list[ModuleRead])
def dsh_modules(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> list[ModuleRead]:
    ensure_current_user_tenant(tenant_id, user)
    return [ModuleRead(**m) for m in get_registry(get_settings()).describe()]


@router.get("/modules/tree")
def dsh_modules_tree(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    """Big-module → sub-module → plugin tree (the product view of the flat registry)."""

    ensure_current_user_tenant(tenant_id, user)
    from staffdeck_dsh.modules.taxonomy import tree

    return tree(get_registry(get_settings()).describe())


@router.get("/snapshot", response_model=SnapshotRead)
def dsh_snapshot(tenant_id: str = Query(...), agent_id: str | None = Query(None), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> SnapshotRead:
    ensure_current_user_tenant(tenant_id, user)
    try:
        staff = project_staff(db, tenant_id, agent_id)
        snap = CompositionCompiler().compile(staff)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModuleSdkError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    from staffdeck_dsh.capabilities.host import PROXY_TOOLS

    allowed = snap.allowed_resource_ids()
    proxy = [n for n, spec in PROXY_TOOLS.items() if not (
        (spec["operation"] == "knowledge.search/v1" and not allowed.get("knowledge_base"))
        or (spec["operation"] == "general_skill.consume/v1" and not allowed.get("general_skill"))
        or (spec["operation"] == "tool.invoke/v1" and not allowed.get("tool"))
    )]
    return SnapshotRead(
        snapshot_id=snap.snapshot_id,
        staff_id=snap.staff_id,
        persona_preview=(snap.persona or "")[:400] or None,
        model_route=dict(snap.model_route),
        session_policy=dict(snap.session_policy),
        grants=[{"operation": g.operation, "resource_type": g.resource_type, "resource_id": g.resource_id, "name": g.name, "scope": g.scope, "sop_id": g.sop_id, "node_id": g.node_id, "slot_name": g.slot_name, "required": g.required} for g in snap.grants],
        sops=[{"skill_id": s.skill_id, "version": s.version, "name": s.name, "resolved_slots": [{"slot": r.declaration.name, "operation": r.declaration.operation, "resource_type": r.resource_type, "resource_id": r.resource_id, "required": r.declaration.required, "node_id": r.declaration.node_id} for r in s.resolved_slots], "sub_sop_ids": list(s.sub_sop_ids)} for s in snap.sops],
        hooks={k: [c.handler for c in v] for k, v in snap.hooks.order.items()},
        channels=list(snap.channels),
        team_id=snap.team_id,
        proxy_tools=proxy,
    )


def _ledger_read(r: HarnessInvocationRecord) -> LedgerRowRead:
    err = (r.result_json or {}).get("error") if isinstance(r.result_json, dict) else None
    return LedgerRowRead(
        id=r.id, session_id=r.session_id, task_id=r.task_id, run_id=r.run_id, tool_name=r.tool_name, status=r.status,
        side_effect_key=r.logical_action_key, arguments=dict(r.arguments_json or {}), error=err if isinstance(err, dict) else None,
        started_at=r.started_at.isoformat(), finished_at=r.finished_at.isoformat() if r.finished_at else None,
        engine=(r.approval_json or {}).get("engine") if isinstance(r.approval_json, dict) else None,
    )


@router.get("/ledger/unknown", response_model=list[LedgerRowRead])
def dsh_ledger_unknown(tenant_id: str = Query(...), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[LedgerRowRead]:
    _admin(tenant_id, user)
    return [_ledger_read(r) for r in InvocationLedger(db).unknown_outcomes(tenant_id)]


@router.get("/ledger/recent", response_model=list[LedgerRowRead])
def dsh_ledger_recent(tenant_id: str = Query(...), session_id: str | None = Query(None), limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[LedgerRowRead]:
    ensure_current_user_tenant(tenant_id, user)
    stmt = select(HarnessInvocationRecord).where(HarnessInvocationRecord.tenant_id == tenant_id)
    if session_id:
        stmt = stmt.where(HarnessInvocationRecord.session_id == session_id)
    rows = db.exec(stmt.order_by(HarnessInvocationRecord.started_at.desc()).limit(limit)).all()
    return [_ledger_read(r) for r in rows]


@router.post("/ledger/{invocation_id}/reconcile", response_model=LedgerRowRead)
def dsh_ledger_reconcile(invocation_id: str, request: ReconcileRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> LedgerRowRead:
    _admin(request.tenant_id, user)
    row = db.get(HarnessInvocationRecord, invocation_id)
    if row is None or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="invocation not found")
    try:
        InvocationLedger(db).reconcile(row, status=request.status, result=request.result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(AgentEvent(tenant_id=row.tenant_id, session_id=row.session_id, event_type="invocation_reconciled", payload_json={"invocation_id": row.id, "status": request.status, "by": user.id}))
    db.commit()
    db.refresh(row)
    return _ledger_read(row)


@router.get("/staff/{agent_id}/engine", response_model=StaffEngineRead)
def dsh_staff_engine(agent_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StaffEngineRead:
    ensure_current_user_tenant(tenant_id, user)
    row = db.get(AgentProfile, agent_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return StaffEngineRead(agent_id=agent_id, engine=staff_engine_choice(row), effective_engine=effective_engine_for(get_settings(), row))


@router.put("/staff/{agent_id}/engine", response_model=StaffEngineRead)
def dsh_set_staff_engine(agent_id: str, request: StaffEngineUpdate, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StaffEngineRead:
    _admin(request.tenant_id, user)
    row = db.get(AgentProfile, agent_id)
    if row is None or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    meta = dict(row.metadata_json or {})
    if request.engine == "default":
        meta.pop(ENGINE_METADATA_KEY, None)
    else:
        meta[ENGINE_METADATA_KEY] = request.engine
    row.metadata_json = meta
    row.updated_at = utc_now()
    db.add(row)
    db.add(AgentEvent(tenant_id=row.tenant_id, session_id=f"agent:{row.id}", event_type="staff_engine_changed", payload_json={"agent_id": row.id, "engine": request.engine, "by": user.id}))
    db.commit()
    db.refresh(row)
    return StaffEngineRead(agent_id=agent_id, engine=staff_engine_choice(row), effective_engine=effective_engine_for(get_settings(), row))


DSH_EVENT_TYPES = (
    "composition_snapshot_compiled", "dsh_process_started", "dsh_turn_started", "dsh_step_started", "dsh_task_finished",
    "dsh_turn_steered", "dsh_turn_failed", "dsh_turn_ended", "capability_provider_selected", "capability_invoked",
    "capability_denied", "hook_decision", "hook_failed", "invocation_reconciled", "staff_engine_changed",
    "human_handoff_created", "human_handoff_assigned", "human_handoff_notified",
)


@router.get("/events/recent")
def dsh_events_recent(tenant_id: str = Query(...), session_id: str = Query(...), limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    ensure_current_user_tenant(tenant_id, user)
    rows = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id, AgentEvent.event_type.in_(DSH_EVENT_TYPES)).order_by(AgentEvent.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    ).all()
    return [{"id": r.id, "event_type": r.event_type, "payload": r.payload_json, "created_at": r.created_at.isoformat()} for r in reversed(rows)]


# -- assembly (dynamic module configuration) ----------------------------------------


class AssemblyRead(BaseModel):
    engine: str
    security_profile: str
    disabled_modules: list[str]
    extra_modules: list[str]
    updated_at: str | None = None
    updated_by: str | None = None


class AssemblyStateRead(BaseModel):
    saved: AssemblyRead
    applied: AssemblyRead | None
    pending: bool
    started_at: str | None
    restart_count: int
    last_restart_error: str | None
    config_path: str


class AssemblyUpdate(BaseModel):
    tenant_id: str
    engine: str | None = None
    security_profile: str | None = None
    disabled_modules: list[str] | None = None
    extra_modules: list[str] | None = None


class RestartRequest(BaseModel):
    tenant_id: str


def _assembly_state_read(settings: Any) -> AssemblyStateRead:
    from staffdeck_dsh.modules.config import config_path
    from staffdeck_dsh.runtime.assembly import assembly_state

    state = assembly_state(settings)
    return AssemblyStateRead(
        saved=AssemblyRead(**state["saved"]),
        applied=AssemblyRead(**state["applied"]) if state["applied"] else None,
        pending=bool(state["pending"]),
        started_at=state["started_at"],
        restart_count=int(state["restart_count"]),
        last_restart_error=state["last_restart_error"],
        config_path=str(config_path(settings)),
    )


@router.get("/config", response_model=AssemblyStateRead)
def dsh_config(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> AssemblyStateRead:
    _admin(tenant_id, user)
    return _assembly_state_read(get_settings())


@router.put("/config", response_model=AssemblyStateRead)
def dsh_set_config(request: AssemblyUpdate, user: User = Depends(get_current_user)) -> AssemblyStateRead:
    """Save a new assembly. Nothing changes until ``POST /restart``."""

    _admin(request.tenant_id, user)
    settings = get_settings()
    current = load_overrides(settings)
    if request.engine is not None and request.engine not in ENGINES:
        raise HTTPException(status_code=400, detail=f"engine must be one of {list(ENGINES)}")
    if request.security_profile is not None and request.security_profile not in SECURITY_PROFILES:
        raise HTTPException(status_code=400, detail=f"security_profile must be one of {list(SECURITY_PROFILES)}")
    disabled = current.disabled_modules if request.disabled_modules is None else list(request.disabled_modules)
    if request.disabled_modules is not None:
        reg = get_registry(settings)
        for mid in disabled:
            item = reg.get(mid)
            if item is not None and item.manifest.kind.value == "K":
                raise HTTPException(status_code=400, detail=f"core module cannot be disabled: {mid}")
            if item is not None and item.slot.value in {"runtime.engine", "security.pep"}:
                raise HTTPException(status_code=400, detail=f"choose engine / security_profile instead of disabling {mid}")
    for spec in (request.extra_modules or []):
        if ":" not in spec and "." not in spec:
            raise HTTPException(status_code=400, detail=f"module spec must look like package.module:register — got {spec!r}")
    new = RuntimeOverrides(
        engine=request.engine or current.engine,
        security_profile=request.security_profile or current.security_profile,
        disabled_modules=disabled,
        extra_modules=current.extra_modules if request.extra_modules is None else list(request.extra_modules),
    )
    save_overrides(settings, new, by=user.id)
    return _assembly_state_read(settings)


@router.post("/restart")
def dsh_restart(request: RestartRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Rebuild the module registry, security profile and DSH runtime from the saved assembly."""

    _admin(request.tenant_id, user)
    from staffdeck_dsh.runtime.assembly import AssemblyFailed, restart_dsh_runtime

    settings = get_settings()
    try:
        info = restart_dsh_runtime(settings)
    except AssemblyFailed as exc:
        db.add(AgentEvent(tenant_id=request.tenant_id, session_id="runtime", event_type="runtime_restart_failed", payload_json={"error": str(exc), "by": user.id}))
        db.commit()
        raise HTTPException(status_code=409, detail=f"重启失败，已恢复原有配置：{exc}") from exc
    db.add(AgentEvent(tenant_id=request.tenant_id, session_id="runtime", event_type="runtime_restarted", payload_json={**{k: v for k, v in info.items() if k != "runtime_error"}, "by": user.id}))
    db.commit()
    return {**info, "state": _assembly_state_read(settings).model_dump()}


# -- execution log ----------------------------------------------------------------


class SessionSummary(BaseModel):
    session_id: str
    title: str | None
    agent_id: str | None
    agent_name: str | None
    channel: str | None
    status: str
    updated_at: str


@router.get("/sessions/recent", response_model=list[SessionSummary])
def dsh_sessions_recent(tenant_id: str = Query(...), limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[SessionSummary]:
    ensure_current_user_tenant(tenant_id, user)
    rows = db.exec(select(ChatSession).where(ChatSession.tenant_id == tenant_id).order_by(ChatSession.updated_at.desc()).limit(limit)).all()  # type: ignore[attr-defined]
    agent_ids = {r.agent_id for r in rows if r.agent_id}
    names: dict[str, str] = {}
    if agent_ids:
        for a in db.exec(select(AgentProfile).where(AgentProfile.id.in_(agent_ids))).all():  # type: ignore[attr-defined]
            names[a.id] = a.name
    return [
        SessionSummary(session_id=r.id, title=r.title, agent_id=r.agent_id, agent_name=names.get(r.agent_id or ""), channel=r.channel, status=r.status, updated_at=r.updated_at.isoformat())
        for r in rows
    ]


# AgentEvent types that become log lines, and the DSH-style tag they get.
LOG_EVENT_TYPES: dict[str, str] = {
    "session_created": "session/start",
    "user_message_received": "user/message",
    "turn_plan_created": "turn/plan",
    "composition_snapshot_compiled": "snapshot/compiled",
    "dsh_process_started": "engine/start",
    "dsh_turn_started": "engine/turn",
    "dsh_step_started": "engine/step",
    "dsh_turn_steered": "engine/steer",
    "dsh_task_finished": "engine/finish",
    "dsh_turn_failed": "turn/failed",
    "dsh_turn_ended": "turn/end",
    "task_frame_started": "task/start",
    "task_frame_finished": "task/end",
    "capability_provider_selected": "tool/route",
    "capability_denied": "tool/denied",
    "hook_decision": "hook/result",
    "hook_failed": "hook/failed",
    "llm_call_finished": "llm/call",
    "llm_call_failed": "llm/failed",
    "knowledge_query_finished": "knowledge/query",
    "memory_recalled": "memory/recall",
    "assistant_message_created": "assistant/message",
    "agent_loop_completed": "turn/complete",
    "stream_cancelled": "turn/cancelled",
    "error_occurred": "error",
    "human_handoff_created": "handoff/created",
    "human_handoff_assigned": "handoff/assigned",
    "human_handoff_notified": "handoff/notified",
    "invocation_reconciled": "ledger/reconciled",
}

# Payload keys worth carrying into the log line; everything else stays behind the "details" toggle.
_LOG_PICK: dict[str, tuple[str, ...]] = {
    "user/message": ("message", "channel", "turn_id"),
    "turn/plan": ("decision", "user_intent", "reason", "confidence"),
    "snapshot/compiled": ("snapshot_id", "grants", "sops", "security_profile", "execution_engine"),
    "engine/start": ("model", "boot_ms", "workspace"),
    "engine/step": ("turn", "step"),
    "engine/finish": ("status", "next_step_id"),
    "turn/failed": ("reason", "error"),
    "turn/end": ("turn", "reason"),
    "task/start": ("kind", "skill_name", "step_id", "harness_max_actions"),
    "task/end": ("kind", "skill_name", "status", "action_count", "error"),
    "tool/route": ("operation", "module_id", "module_version"),
    "tool/denied": ("operation", "reason", "resource"),
    "hook/result": ("point", "handler", "decision", "reason"),
    "hook/failed": ("point", "handler", "error"),
    "llm/call": ("operation", "model", "model_name", "duration_ms", "input_tokens", "output_tokens", "status", "finish_reason"),
    "llm/failed": ("operation", "model", "error", "message"),
    "knowledge/query": ("query", "hit_count", "chunks", "results"),
    "memory/recall": ("memories",),
    "assistant/message": ("reply", "message_id"),
    "turn/complete": ("mode", "iteration"),
    "turn/cancelled": ("text",),
    "error": ("code", "message"),
    "handoff/created": ("handoff_id", "reason", "question"),
    "handoff/assigned": ("handoff_id", "assignee_id", "assignee_name"),
    "handoff/notified": ("handoff_id", "channel", "notifier"),
    "ledger/reconciled": ("invocation_id", "status", "by"),
}


def _pick(tag: str, payload: dict[str, Any]) -> dict[str, Any]:
    keys = _LOG_PICK.get(tag)
    if not keys:
        return {k: v for k, v in payload.items() if not isinstance(v, (dict, list)) or k in ("error", "reason")}
    out: dict[str, Any] = {}
    for k in keys:
        if k in payload:
            v = payload[k]
            if k == "memories" and isinstance(v, list):
                out["count"] = len(v)
                continue
            if k in ("chunks", "results") and isinstance(v, list):
                out["hit_count"] = len(v)
                continue
            if isinstance(v, str) and len(v) > 400:
                v = v[:400] + "…"
            out[k] = v
    return out


def _event_ts(e: AgentEvent, payload: dict[str, Any]) -> str:
    """Buffered trace events carry the moment they happened; persist time is only a fallback. Naive UTC like the rest of the DB."""

    occurred = payload.get("occurred_at")
    if isinstance(occurred, str) and occurred:
        try:
            dt = datetime.fromisoformat(occurred)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.isoformat()
        except ValueError:
            pass
    return e.created_at.isoformat()


def _log_entries(db: Session, tenant_id: str, session_id: str, limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    events = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id, AgentEvent.event_type.in_(tuple(LOG_EVENT_TYPES))).order_by(AgentEvent.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    ).all()
    for e in events:
        tag = LOG_EVENT_TYPES[e.event_type]
        payload = e.payload_json if isinstance(e.payload_json, dict) else {}
        entries.append({
            "id": f"evt_{e.id}", "ts": _event_ts(e, payload), "type": tag, "source": "event", "event_type": e.event_type,
            "data": _pick(tag, payload), "engine": payload.get("execution_engine"), "turn_id": payload.get("turn_id") or payload.get("user_message_id"),
        })
    calls = db.exec(
        select(HarnessInvocationRecord).where(HarnessInvocationRecord.tenant_id == tenant_id, HarnessInvocationRecord.session_id == session_id).order_by(HarnessInvocationRecord.started_at.desc()).limit(limit)
    ).all()
    for r in calls:
        engine = (r.approval_json or {}).get("engine") if isinstance(r.approval_json, dict) else None
        err = (r.result_json or {}).get("error") if isinstance(r.result_json, dict) else None
        entries.append({
            "id": f"call_{r.id}", "ts": r.started_at.isoformat(), "type": "tool/call", "source": "ledger", "invocation_id": r.id,
            "data": {"tool": r.tool_name, "arguments": r.arguments_json or {}, "side_effect_key": r.logical_action_key}, "engine": engine, "turn_id": None,
        })
        if r.finished_at is not None or r.status in {"outcome_unknown", "denied", "cancelled", "failed", "completed"}:
            duration = int((r.finished_at - r.started_at).total_seconds() * 1000) if r.finished_at else None
            entries.append({
                "id": f"result_{r.id}", "ts": (r.finished_at or r.started_at).isoformat(), "type": "tool/result", "source": "ledger", "invocation_id": r.id,
                "data": {"tool": r.tool_name, "status": r.status, "duration_ms": duration, "error": err if isinstance(err, dict) else None}, "engine": engine, "turn_id": None,
            })
    entries.sort(key=lambda x: (x["ts"], 0 if x["type"] == "tool/call" else 1))
    return entries[-limit:]


@router.get("/log")
def dsh_log(tenant_id: str = Query(...), session_id: str = Query(...), limit: int = Query(400, ge=1, le=2000), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """One session's execution log: runtime events and capability invocations merged in time order."""

    ensure_current_user_tenant(tenant_id, user)
    row = db.get(ChatSession, session_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="session not found")
    agent = db.get(AgentProfile, row.agent_id) if row.agent_id else None
    return {
        "session": {"session_id": row.id, "title": row.title, "agent_id": row.agent_id, "agent_name": agent.name if agent else None, "channel": row.channel, "status": row.status, "updated_at": row.updated_at.isoformat()},
        "entries": _log_entries(db, tenant_id, session_id, limit),
    }
