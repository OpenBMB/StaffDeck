"""Admin API for the Harness v3 pluggable runtime.

Mounted under ``/api/enterprise/harness``; every route requires a tenant admin.
Lives in the parallel package and is attached to the FastAPI app by
``staffdeck_harness.runtime.assembly.mount_admin_api`` only when ``harness_v3_enabled``
or ``harness_admin_api_enabled`` is set, so a legacy deployment exposes nothing.

Endpoints

    GET  /status                     runtime health, security profile, engine, module counts
    GET  /modules                    the sealed Module Registry (every slot, enabled/guarded)
    GET  /snapshot?agent_id=         compile a CompositionSnapshot for a staff (what the model will see)
    GET  /ledger/unknown             outcome_unknown invocations awaiting reconciliation
    POST /ledger/{id}/reconcile      settle one as completed|failed
    GET  /staff/{agent_id}/engine    which engine this staff runs on
    PUT  /staff/{agent_id}/engine    set harness_v2|harness_v3|default for this staff (persisted on AgentProfile.metadata_json)
    GET  /events/recent?session_id=  DSH-related AgentEvents for one session (trace projection)
    GET  /config                     saved vs applied assembly (engine / profile / disabled / extra modules)
    PUT  /config                     save a new assembly (takes effect after /restart)
    POST /restart                    rebuild registry + profile + Harness v3 runtime from the saved assembly
    GET  /sessions/recent            recent chat sessions (for the log picker)
    GET  /log?session_id=            one session's execution log (events + invocation ledger, merged)
    POST /base/test                  connection test against the enterprise permission centre (no side effects)
    PUT  /modules/{id}/placement     move a module under a taxonomy sub-module (display only, immediate)
    POST /modules/inspect            dry-run an external module spec against a throwaway registry
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
from app.security.permissions import ensure_current_user_tenant, ensure_tenant_admin, is_admin_user
from staffdeck_harness.capabilities.ledger import InvocationLedger
from staffdeck_harness.composition.compiler import CompositionCompiler
from staffdeck_harness.composition.staff import project_staff
from staffdeck_harness.contracts.errors import EngineUnavailable, ModuleSdkError
from staffdeck_harness.modules.config import ENGINES, SECURITY_PROFILES, BaseConnection, InvalidConnection, RuntimeOverrides, check_url_policy, load_overrides, save_overrides
from staffdeck_harness.modules.registry import get_registry, validate_spec
from staffdeck_harness.modules.taxonomy import sub_ids, tree as taxonomy_tree, tree_options
from staffdeck_harness.security.profile import get_profile

router = APIRouter(prefix="/api/enterprise/harness", tags=["enterprise:harness"], dependencies=[Depends(get_current_user)])

EngineChoice = Literal["default", "harness_v2", "harness_v3"]
ENGINE_METADATA_KEY = "execution_engine"


class StatusRead(BaseModel):
    harness_v3_enabled: bool
    security_profile: str
    default_engine: EngineChoice
    staff_allowlist: list[str]
    fallback_to_legacy: bool
    harness_v3_root: str
    harness_v3_home: str
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
    last_restart_error: str | None = None
    last_restart_failed: bool = False
    restarting: bool = False
    base_configured: bool = False
    base_last_test_ok: bool | None = None


class ModuleRead(BaseModel):
    module_id: str
    name: str
    summary: str = ""
    category: str = ""
    switchable: bool = True
    metadata: dict[str, Any] = {}
    version: str
    kind: str
    contract_version: str
    slot: str
    enabled: bool
    source: str
    spec: str = "builtin"
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
    effective_engine: Literal["harness_v2", "harness_v3"]


class StaffEngineUpdate(BaseModel):
    tenant_id: str
    engine: EngineChoice


def _admin(tenant_id: str, user: User) -> User:
    return ensure_tenant_admin(tenant_id, user)


def _settings_engine(settings: Any) -> EngineChoice:
    return "harness_v3" if bool(getattr(settings, "harness_v3_enabled", False)) else "harness_v2"


def staff_engine_choice(row: AgentProfile) -> EngineChoice:
    value = str((row.metadata_json or {}).get(ENGINE_METADATA_KEY) or "default")
    return value if value in {"default", "harness_v2", "harness_v3"} else "default"  # type: ignore[return-value]


def effective_engine_for(settings: Any, row: AgentProfile | None) -> Literal["harness_v2", "harness_v3"]:
    """Per-staff override > allowlist > deployment default. Used by EngineHost."""

    if row is not None:
        choice = staff_engine_choice(row)
        if choice != "default":
            return choice
    if not bool(getattr(settings, "harness_v3_enabled", False)):
        return "harness_v2"
    allow = {x.strip() for x in str(getattr(settings, "harness_v3_staff_allowlist", "") or "").split(",") if x.strip()}
    if allow and (row is None or row.id not in allow):
        return "harness_v2"
    return "harness_v3"


@router.get("/status", response_model=StatusRead)
def harness_status(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> StatusRead:
    ensure_current_user_tenant(tenant_id, user)
    settings = get_settings()
    reg = get_registry(settings)
    modules = reg.describe()
    from staffdeck_harness.runtime.assembly import assembly_state

    state = assembly_state(settings)
    runtime_ok, runtime_error, mcp_url, live = True, None, None, 0
    if bool(getattr(settings, "harness_v3_enabled", False)):
        try:
            from staffdeck_harness.bridge.engine_host import get_runtime

            rt = get_runtime(settings)
            mcp_url, live = rt.mcp_url, len(rt.registry)
        except EngineUnavailable as exc:
            runtime_ok, runtime_error = False, exc.to_dict()
    else:
        # Harness v2 runs in-process: nothing to be "down".
        runtime_ok = True
        runtime_error = None
    return StatusRead(
        harness_v3_enabled=bool(getattr(settings, "harness_v3_enabled", False)),
        security_profile=get_profile(settings).name,
        default_engine=_settings_engine(settings),
        staff_allowlist=[x.strip() for x in str(getattr(settings, "harness_v3_staff_allowlist", "") or "").split(",") if x.strip()],
        fallback_to_legacy=bool(getattr(settings, "harness_v3_fallback_to_v2", True)),
        harness_v3_root=str(getattr(settings, "harness_v3_root", "") or "") if is_admin_user(user) else "",
        harness_v3_home=str(getattr(settings, "harness_v3_home", "") or "") if is_admin_user(user) else "",
        runtime_ok=runtime_ok,
        runtime_error=runtime_error,
        mcp_url=mcp_url if is_admin_user(user) else None,
        live_activations=live,
        modules_total=len(modules),
        modules_enabled=sum(1 for m in modules if m["enabled"]),
        registry_generation=reg.generation,
        started_at=state.get("started_at"),
        restart_count=int(state.get("restart_count") or 0),
        config_pending=bool(state.get("pending")),
        engine_version=_engine_version(reg),
        last_restart_error=state.get("last_restart_error") if is_admin_user(user) else None,
        last_restart_failed=bool(state.get("last_restart_error")),
        restarting=bool(state.get("restarting")),
        base_configured=bool(state.get("saved", {}).get("base", {}).get("configured")),
        base_last_test_ok=state.get("saved", {}).get("base", {}).get("last_test_ok"),
    )


def _engine_version(reg: Any) -> str | None:
    item = reg.get("harness_v3.core")
    if item is None or not callable(getattr(item.provider, "version", None)):
        return None
    try:
        return str(item.provider.version())
    except Exception:  # noqa: BLE001
        return None


@router.get("/modules", response_model=list[ModuleRead])
def harness_modules(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> list[ModuleRead]:
    ensure_current_user_tenant(tenant_id, user)
    return [ModuleRead(**m) for m in get_registry(get_settings()).describe()]


@router.get("/modules/tree")
def harness_modules_tree(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    """Big-module → sub-module → plugin tree (the product view of the flat registry), with operator placements applied."""

    ensure_current_user_tenant(tenant_id, user)
    settings = get_settings()
    return taxonomy_tree(get_registry(settings).describe(), load_overrides(settings).placements)


@router.get("/modules/tree/options")
def harness_modules_tree_options(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    ensure_current_user_tenant(tenant_id, user)
    return tree_options()


@router.get("/snapshot", response_model=SnapshotRead)
def harness_snapshot(tenant_id: str = Query(...), agent_id: str | None = Query(None), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> SnapshotRead:
    ensure_current_user_tenant(tenant_id, user)
    try:
        staff = project_staff(db, tenant_id, agent_id)
        snap = CompositionCompiler().compile(staff)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModuleSdkError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    from staffdeck_harness.capabilities.host import PROXY_TOOLS

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
def harness_ledger_unknown(tenant_id: str = Query(...), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[LedgerRowRead]:
    _admin(tenant_id, user)
    return [_ledger_read(r) for r in InvocationLedger(db).unknown_outcomes(tenant_id)]


@router.get("/ledger/recent", response_model=list[LedgerRowRead])
def harness_ledger_recent(tenant_id: str = Query(...), session_id: str | None = Query(None), limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[LedgerRowRead]:
    ensure_current_user_tenant(tenant_id, user)
    stmt = select(HarnessInvocationRecord).where(HarnessInvocationRecord.tenant_id == tenant_id)
    if session_id:
        stmt = stmt.where(HarnessInvocationRecord.session_id == session_id)
    rows = db.exec(stmt.order_by(HarnessInvocationRecord.started_at.desc()).limit(limit)).all()
    return [_ledger_read(r) for r in rows]


@router.post("/ledger/{invocation_id}/reconcile", response_model=LedgerRowRead)
def harness_ledger_reconcile(invocation_id: str, request: ReconcileRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> LedgerRowRead:
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
def harness_staff_engine(agent_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StaffEngineRead:
    ensure_current_user_tenant(tenant_id, user)
    row = db.get(AgentProfile, agent_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return StaffEngineRead(agent_id=agent_id, engine=staff_engine_choice(row), effective_engine=effective_engine_for(get_settings(), row))


@router.put("/staff/{agent_id}/engine", response_model=StaffEngineRead)
def harness_set_staff_engine(agent_id: str, request: StaffEngineUpdate, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> StaffEngineRead:
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


HARNESS_V3_EVENT_TYPES = (
    "composition_snapshot_compiled", "harness_v3_process_started", "harness_v3_turn_started", "harness_v3_step_started", "harness_v3_task_finished",
    "harness_v3_turn_steered", "harness_v3_turn_failed", "harness_v3_turn_ended", "capability_provider_selected", "capability_invoked",
    "capability_denied", "hook_decision", "hook_failed", "invocation_reconciled", "staff_engine_changed",
    "human_handoff_created", "human_handoff_assigned", "human_handoff_notified",
)


@router.get("/events/recent")
def harness_events_recent(tenant_id: str = Query(...), session_id: str = Query(...), limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    ensure_current_user_tenant(tenant_id, user)
    rows = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id, AgentEvent.event_type.in_(HARNESS_V3_EVENT_TYPES)).order_by(AgentEvent.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    ).all()
    return [{"id": r.id, "event_type": r.event_type, "payload": r.payload_json, "created_at": r.created_at.isoformat()} for r in reversed(rows)]


# -- assembly (dynamic module configuration) ----------------------------------------


class AssemblyRead(BaseModel):
    engine: str
    security_profile: str
    disabled_modules: list[str]
    extra_modules: list[str]
    placements: dict[str, str] = {}
    base: dict[str, Any] = {}
    updated_at: str | None = None
    updated_by: str | None = None


class AssemblyStateRead(BaseModel):
    saved: AssemblyRead
    applied: AssemblyRead | None
    pending: bool
    started_at: str | None
    restart_count: int
    last_restart_error: str | None
    restarting: bool = False
    config_path: str


class AssemblyUpdate(BaseModel):
    tenant_id: str
    engine: str | None = None
    security_profile: str | None = None
    disabled_modules: list[str] | None = None
    extra_modules: list[str] | None = None
    placements: dict[str, str | None] | None = None
    base: dict[str, Any] | None = None


class BaseTestRequest(BaseModel):
    tenant_id: str
    base: dict[str, Any] | None = None


class PlacementUpdate(BaseModel):
    tenant_id: str
    sub_id: str | None = None


class InspectRequest(BaseModel):
    tenant_id: str
    spec: str


class RestartRequest(BaseModel):
    tenant_id: str


def _assembly_read(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k in AssemblyRead.model_fields}


def _assembly_state_read(settings: Any) -> AssemblyStateRead:
    from staffdeck_harness.modules.config import config_path
    from staffdeck_harness.runtime.assembly import assembly_state

    state = assembly_state(settings)
    return AssemblyStateRead(
        saved=AssemblyRead(**_assembly_read(state["saved"])),
        applied=AssemblyRead(**_assembly_read(state["applied"])) if state["applied"] else None,
        pending=bool(state["pending"]),
        started_at=state["started_at"],
        restart_count=int(state["restart_count"]),
        last_restart_error=state["last_restart_error"],
        restarting=bool(state.get("restarting")),
        config_path=str(config_path(settings)),
    )


@router.get("/config", response_model=AssemblyStateRead)
def harness_config(tenant_id: str = Query(...), user: User = Depends(get_current_user)) -> AssemblyStateRead:
    _admin(tenant_id, user)
    return _assembly_state_read(get_settings())


@router.put("/config", response_model=AssemblyStateRead)
def harness_set_config(request: AssemblyUpdate, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> AssemblyStateRead:
    """Save a new assembly. Nothing changes until ``POST /restart`` (placements apply immediately)."""

    _admin(request.tenant_id, user)
    settings = get_settings()
    current = load_overrides(settings)
    reg = get_registry(settings)
    known = {m["module_id"]: m for m in reg.describe()}
    if request.engine is not None and request.engine not in ENGINES:
        raise HTTPException(status_code=400, detail=f"engine must be one of {list(ENGINES)}")
    if request.security_profile is not None and request.security_profile not in SECURITY_PROFILES:
        raise HTTPException(status_code=400, detail=f"security_profile must be one of {list(SECURITY_PROFILES)}")
    disabled = current.disabled_modules if request.disabled_modules is None else list(request.disabled_modules)
    if request.disabled_modules is not None:
        for mid in disabled:
            item = known.get(mid)
            if item is None:
                raise HTTPException(status_code=400, detail=f"未知的模块：{mid}")
            if item["slot"] in {"runtime.engine", "security.pep"}:
                raise HTTPException(status_code=400, detail=f"{item['name']} 通过选择引擎 / 权限模式切换，不能单独停用")
            if not item["switchable"]:
                raise HTTPException(status_code=400, detail=f"{item['name']} 是平台核心组成部分，不能停用")
    extra = current.extra_modules if request.extra_modules is None else list(request.extra_modules)
    for spec in extra:
        try:
            validate_spec(spec)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    placements = dict(current.placements)
    if request.placements is not None:
        valid = set(sub_ids())
        for mid, sub in request.placements.items():
            if sub is None or sub == "":
                placements.pop(mid, None)
                continue
            if sub not in valid:
                raise HTTPException(status_code=400, detail=f"未知的类目：{sub}")
            item = known.get(mid)
            if item is not None and not _movable(item):
                raise HTTPException(status_code=400, detail=f"{item['name']} 的位置由平台决定，不能移动")
            placements[mid] = sub
    try:
        base = current.base if request.base is None else current.base.merge_update(request.base)
        check_url_policy(base.authz_url, settings, field="权限中心地址")
        check_url_policy(base.identity_internal_url, settings, field="身份中心地址")
    except InvalidConnection as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profile = request.security_profile or current.security_profile
    switching_to_business = request.security_profile == "BUSINESS_BASE" and current.security_profile != "BUSINESS_BASE"
    if switching_to_business or (profile == "BUSINESS_BASE" and request.base is not None):
        eff = base.effective(settings)
        if not eff.authz_url or not eff.decision_token:
            raise HTTPException(status_code=400, detail="切换到企业版权限前，请先填写权限中心地址和决策令牌，并通过测试连接")
    if request.base is not None and base.signature() != current.base.signature():
        base.last_test_ok = None
        base.last_test_at = None
    new = RuntimeOverrides(
        engine=request.engine or current.engine,
        security_profile=profile,
        disabled_modules=disabled,
        extra_modules=extra,
        placements=placements,
        base=base,
    )
    save_overrides(settings, new, by=user.id)
    from staffdeck_harness.runtime.assembly import assembly_state, clear_restart_error

    state = assembly_state(settings)
    if not state["pending"]:
        clear_restart_error()   # the operator reverted to what is running; the old refusal is moot
    _audit(db, request.tenant_id, "assembly_saved", {
        "by": user.id,
        "engine": new.engine, "security_profile": new.security_profile,
        "disabled_modules": new.disabled_modules, "extra_modules": new.extra_modules,
        "placements_changed": request.placements is not None,
        "base_fields_changed": sorted(k for k in (request.base or {}).keys()),
        "pending": bool(state["pending"]),
    })
    return _assembly_state_read(settings)


def _movable(m: dict[str, Any]) -> bool:
    from staffdeck_harness.modules.taxonomy import movable

    return movable(m)


def _audit(db: Session, tenant_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Admin-surface audit trail (never contains secret values)."""

    db.add(AgentEvent(tenant_id=tenant_id, session_id="runtime", event_type=event_type, payload_json=payload))
    db.commit()


def _supplied_secret(patch: dict[str, Any], field: str) -> bool:
    from staffdeck_harness.modules.config import SECRET_MASK

    v = patch.get(field)
    return isinstance(v, str) and v != "" and v != SECRET_MASK


@router.post("/base/test")
def harness_base_test(request: BaseTestRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Connection test against the enterprise permission centre. Unsaved field values may be passed in ``base``."""

    _admin(request.tenant_id, user)
    from staffdeck_harness.security.base_preflight import preflight_base

    settings = get_settings()
    current = load_overrides(settings)
    try:
        conn: BaseConnection = current.base if request.base is None else current.base.merge_update(request.base)
        check_url_policy(conn.authz_url, settings, field="权限中心地址")
        check_url_policy(conn.identity_internal_url, settings, field="身份中心地址")
    except InvalidConnection as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Never pair a stored/env secret with a URL the operator just typed: a new address needs its own token in the same request.
    saved_eff = current.base.effective(settings)
    patch = request.base or {}
    if conn.authz_url and conn.authz_url != saved_eff.authz_url and not _supplied_secret(patch, "decision_token"):
        raise HTTPException(status_code=400, detail="更换权限中心地址时，请同时填写该地址对应的决策令牌")
    if conn.identity_internal_url and conn.identity_internal_url != saved_eff.identity_internal_url and not _supplied_secret(patch, "runtime_client_secret"):
        raise HTTPException(status_code=400, detail="更换身份中心地址时，请同时填写该地址对应的运行时客户端密钥")
    try:
        report = preflight_base(conn.effective(settings), tenant_id=request.tenant_id, principal_id=user.id)
    except InvalidConnection as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, request.tenant_id, "base_connection_tested", {"by": user.id, "authz_url": conn.effective(settings).authz_url, "ok": report.ok, "unsaved_values": request.base is not None})
    # Remember the verdict only for the saved values, so a passing test of unsaved edits does not unlock a restart.
    if request.base is None or conn.signature() == current.base.signature():
        current.base.last_test_ok = report.ok
        current.base.last_test_at = report.tested_at
        save_overrides(settings, current, by=user.id)
    return {**report.to_dict(), "saved": request.base is None or conn.signature() == current.base.signature()}


@router.put("/modules/{module_id}/placement")
def harness_set_placement(module_id: str, request: PlacementUpdate, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Move a module under a taxonomy sub-module. Display only — takes effect immediately, no restart, never pending."""

    _admin(request.tenant_id, user)
    from staffdeck_harness.modules.registry import MODULE_ID_RE

    if not MODULE_ID_RE.match(module_id):
        raise HTTPException(status_code=400, detail=f"非法的模块 ID：{module_id}")
    settings = get_settings()
    reg = get_registry(settings)
    item = reg.get(module_id)
    if request.sub_id is not None and request.sub_id not in set(sub_ids()):
        raise HTTPException(status_code=400, detail=f"未知的类目：{request.sub_id}")
    if item is not None:
        if item.manifest.kind.value == "K":
            raise HTTPException(status_code=400, detail="平台核心模块的位置由平台决定，不能移动")
        if item.slot.value in {"runtime.engine", "security.pep"}:
            raise HTTPException(status_code=400, detail="引擎和权限模式固定在各自的类目下")
    current = load_overrides(settings)
    if request.sub_id:
        current.placements[module_id] = request.sub_id
    else:
        current.placements.pop(module_id, None)
    save_overrides(settings, current, by=user.id)
    db.add(AgentEvent(tenant_id=request.tenant_id, session_id="runtime", event_type="module_placed", payload_json={"module_id": module_id, "sub_id": request.sub_id, "by": user.id}))
    db.commit()
    tree = taxonomy_tree(reg.describe(), current.placements)
    placement = next((m["placement"] for big in tree for sub in big["subs"] for m in sub["modules"] if m["module_id"] == module_id), None)
    return {"module_id": module_id, "sub_id": request.sub_id, "installed": item is not None, "placement": placement, "tree": tree}


@router.post("/modules/inspect")
def harness_inspect_module(request: InspectRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Dry-run an external module spec: what it would install and whether the assembly would still seal."""

    _admin(request.tenant_id, user)
    from staffdeck_harness.modules.inspect import inspect_spec

    settings = get_settings()
    live_ids = {m["module_id"] for m in get_registry(settings).describe()}
    result = inspect_spec(settings, request.spec, live_ids=live_ids)
    db.add(AgentEvent(tenant_id=request.tenant_id, session_id="runtime", event_type="module_inspected", payload_json={"spec": request.spec, "ok": result["ok"], "modules": [m["module_id"] for m in result["modules"]], "by": user.id}))
    db.commit()
    return result


@router.post("/restart")
def harness_restart(request: RestartRequest, db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Rebuild the module registry, security profile and Harness v3 runtime from the saved assembly."""

    _admin(request.tenant_id, user)
    from staffdeck_harness.runtime.assembly import AssemblyFailed, restart_harness_runtime

    settings = get_settings()
    _audit(db, request.tenant_id, "runtime_restart_requested", {"by": user.id})
    try:
        info = restart_harness_runtime(settings, tenant_id=request.tenant_id, principal_id=user.id)
    except AssemblyFailed as exc:
        db.add(AgentEvent(tenant_id=request.tenant_id, session_id="runtime", event_type="runtime_restart_failed", payload_json={"error": str(exc), "by": user.id}))
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc) if str(exc).startswith(("无法切换", "装配无法", "已有一次")) else f"重启失败，已恢复原有配置：{exc}") from exc
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
def harness_sessions_recent(tenant_id: str = Query(...), limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> list[SessionSummary]:
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


# AgentEvent types that become log lines, and the Harness v3-style tag they get.
LOG_EVENT_TYPES: dict[str, str] = {
    "session_created": "session/start",
    "user_message_received": "user/message",
    "turn_plan_created": "turn/plan",
    "composition_snapshot_compiled": "snapshot/compiled",
    "harness_v3_process_started": "engine/start",
    "harness_v3_turn_started": "engine/turn",
    "harness_v3_step_started": "engine/step",
    "harness_v3_turn_steered": "engine/steer",
    "harness_v3_task_finished": "engine/finish",
    "harness_v3_turn_failed": "turn/failed",
    "harness_v3_turn_ended": "turn/end",
    "task_frame_started": "task/start",
    "task_frame_finished": "task/end",
    "capability_provider_selected": "tool/route",
    "capability_denied": "tool/denied",
    "hook_decision": "hook/result",
    "hook_failed": "hook/failed",
    "llm_call_finished": "llm/call",
    "harness_v3_model_request": "engine/request",
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
    "assembly_saved": "admin/config",
    "base_connection_tested": "admin/base_test",
    "module_placed": "admin/placement",
    "module_inspected": "admin/inspect",
    "runtime_restart_requested": "admin/restart",
    "runtime_restarted": "admin/restarted",
    "runtime_restart_failed": "admin/restart_failed",
}

# Payload keys worth carrying into the log line; everything else stays behind the "details" toggle.
_LOG_PICK: dict[str, tuple[str, ...]] = {
    "user/message": ("message", "channel", "turn_id"),
    "turn/plan": ("decision", "user_intent", "reason", "confidence"),
    "snapshot/compiled": ("snapshot_id", "grants", "sops", "security_profile", "execution_engine"),
    "engine/start": ("model", "thinking", "reasoning_effort", "boot_ms", "workspace"),
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
    "engine/request": ("provider", "model"),
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
    "admin/config": ("by", "engine", "security_profile", "disabled_modules", "extra_modules", "pending"),
    "admin/base_test": ("by", "authz_url", "ok"),
    "admin/placement": ("by", "module_id", "sub_id"),
    "admin/inspect": ("by", "spec", "ok", "modules"),
    "admin/restart": ("by",),
    "admin/restarted": ("by", "security_profile", "modules", "restart_count"),
    "admin/restart_failed": ("by", "error"),
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


@router.get("/audit")
def harness_audit(tenant_id: str = Query(...), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Who changed the assembly, tested the permission centre, dry-ran or restarted — admin only."""

    _admin(tenant_id, user)
    admin_types = [t for t, tag in LOG_EVENT_TYPES.items() if tag.startswith("admin/")]
    rows = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == "runtime", AgentEvent.event_type.in_(admin_types)).order_by(AgentEvent.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    ).all()
    entries = []
    for e in reversed(rows):
        tag = LOG_EVENT_TYPES[e.event_type]
        payload = e.payload_json if isinstance(e.payload_json, dict) else {}
        entries.append({"id": f"evt_{e.id}", "ts": _event_ts(e, payload), "type": tag, "source": "event", "event_type": e.event_type, "data": _pick(tag, payload), "engine": None, "turn_id": None})
    return {"entries": entries}


@router.get("/log")
def harness_log(tenant_id: str = Query(...), session_id: str = Query(...), limit: int = Query(400, ge=1, le=2000), db: Session = Depends(get_session), user: User = Depends(get_current_user)) -> dict[str, Any]:
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
