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
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_session
from app.db.models import AgentEvent, AgentProfile, HarnessInvocationRecord, User, utc_now
from app.security.auth import get_current_user
from app.security.permissions import ensure_tenant_admin, ensure_current_user_tenant
from staffdeck_dsh.capabilities.ledger import InvocationLedger
from staffdeck_dsh.composition.compiler import CompositionCompiler
from staffdeck_dsh.composition.staff import project_staff
from staffdeck_dsh.contracts.errors import EngineUnavailable, ModuleSdkError
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


class ModuleRead(BaseModel):
    module_id: str
    name: str
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
    )


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
