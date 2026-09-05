"""Invocation Ledger over the existing ``harness_invocations`` table.

The legacy ``HarnessCapabilityInvoker`` already implements the important
semantics — start record, ``completed`` / ``failed`` / ``outcome_unknown`` /
``cancelled``, claim release for pre-side-effect failures, replay-or-block on a
logical action key — but only for ``kind == "tool"``. This ledger generalizes
the same rows and the same state machine to *every* ``ModuleInvocation`` so
knowledge, skills, sandbox and artifacts get receipts too, and Harness v3-originated
calls and legacy calls share one audit trail.

State machine (unchanged from legacy):

    started ──ok──────────────► completed        (claim kept; replayable)
            ──not sent────────► failed           (claim released; retryable)
            ──maybe sent──────► outcome_unknown  (claim kept; blocks replay)
            ──cancel──────────► cancelled        (claim released)
            ──pep deny────────► denied           (claim released)

``side_effect_key`` is the logical action key. Read-only operations have no
key and are never deduplicated; they still get a receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import HarnessInvocationRecord, utc_now
from staffdeck_harness.contracts.errors import OutcomeUnknown
from staffdeck_harness.contracts.invocation import ModuleInvocation, ModuleResult, Receipt

# Error codes the legacy invoker treats as "the provider never saw the request".
NOT_SENT_CODES = frozenset(
    {
        "NOT_FOUND", "DISABLED", "NOT_ALLOWED", "UNSUPPORTED_TOOL_TYPE", "TOOL_NOT_AVAILABLE",
        "CAPABILITY_AUTHORIZATION_REVOKED", "CAPABILITY_SNAPSHOT_CHANGED", "CAPABILITY_NOT_ACTIVATED",
        "CAPABILITY_NOT_AVAILABLE", "INVALID_ARGUMENTS", "PERMISSION_DENIED", "ACTIVATION_FENCED",
        "REQUIRED_SLOT_MISSING", "SLOT_NOT_BOUND", "AUTHORIZATION_UNAVAILABLE",
        # host-side refusals: the provider never ran, so nothing can have been sent
        "UNSUPPORTED_CAPABILITY", "PROVIDER_INVALID", "PROVIDER_UNAUTHORIZED", "ENGINE_UNAVAILABLE",
        "PROVIDER_UNAVAILABLE", "PROVIDER_VERSION_CHANGED", "PROVIDER_CONFLICT",
    }
)

_AUDIT_MAX = 4_000


def _audit(value: Any) -> Any:
    try:
        import json

        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= _AUDIT_MAX:
        return value
    return {"_truncated": True, "preview": text[:_AUDIT_MAX]}


@dataclass
class LedgerEntry:
    row: HarnessInvocationRecord
    invocation: ModuleInvocation

    @property
    def receipt(self) -> Receipt:
        r = self.row
        error = r.result_json.get("error") if isinstance(r.result_json, dict) else None
        return Receipt(
            invocation_id=self.invocation.invocation_id,
            status=r.status,  # type: ignore[arg-type]
            request_digest=r.request_digest,
            side_effect_key=r.logical_action_key,
            replayed_from=r.replayed_from_invocation_id,
            ledger_id=r.id,
            started_at=r.started_at,
            finished_at=r.finished_at,
            error=error if isinstance(error, dict) else None,
        )


class InvocationLedger:
    def __init__(self, db: Session):
        self.db = db

    # -- replay ---------------------------------------------------------------

    def find_prior(self, side_effect_key: str) -> HarnessInvocationRecord | None:
        return self.db.exec(
            select(HarnessInvocationRecord).where(HarnessInvocationRecord.logical_action_key == side_effect_key)
        ).first()

    def replay_or_block(self, invocation: ModuleInvocation) -> tuple[ModuleResult, Receipt] | None:
        key = invocation.side_effect_key()
        if not key:
            return None
        prior = self.find_prior(key)
        if prior is None:
            return None
        if prior.status == "completed" and "success" in (prior.response_cache_json or {}):
            cached = dict(prior.response_cache_json or {})
            data = cached.get("data")
            replay_meta = {"idempotent_replay": True, "replayed_from_invocation_id": prior.id}
            if isinstance(data, dict):
                data = {**data, **replay_meta}
            result = ModuleResult(
                success=bool(cached["success"]),
                error=cached.get("error"),
                data=data,
                citations=tuple(cached.get("citations") or ()),
                artifacts=tuple(cached.get("artifacts") or ()),
                extensions={"replay": replay_meta},
            )
            receipt = Receipt(
                invocation_id=invocation.invocation_id,
                status="completed",
                request_digest=prior.request_digest,
                side_effect_key=key,
                replayed_from=prior.id,
                ledger_id=prior.id,
                started_at=prior.started_at,
                finished_at=prior.finished_at,
            )
            return result, receipt
        raise OutcomeUnknown(
            "相同副作用调用已有未完成的持久化记录；为避免重复提交，不会自动重试，请先核对外部系统状态。",
            details={"side_effect_key": key, "prior_invocation_id": prior.id, "prior_status": prior.status},
        )

    # -- lifecycle ------------------------------------------------------------

    def cache_projection(self, receipt: Receipt, result: ModuleResult) -> None:
        row = self.db.get(HarnessInvocationRecord, receipt.ledger_id)
        if row is None or row.status != "completed":
            return
        row.response_cache_json = {
            "success": result.success, "data": result.data, "error": result.error,
            "citations": list(result.citations), "artifacts": list(result.artifacts),
        }
        self.db.add(row)
        self.db.commit()

    def start(self, invocation: ModuleInvocation) -> LedgerEntry:
        ctx = invocation.context
        row = HarnessInvocationRecord(
            tenant_id=ctx.tenant_id,
            session_id=ctx.session_id,
            task_id=ctx.task_frame_id or ctx.turn_id,
            run_id=ctx.run_id or ctx.turn_id,
            call_id=invocation.invocation_id,
            tool_name=f"{invocation.module_id}:{invocation.operation}",
            request_digest=invocation.request_digest(),
            logical_action_key=invocation.side_effect_key(),
            status="started",
            arguments_json=_audit(dict(invocation.arguments)),
            approval_json={"engine": invocation.metadata.get("execution_engine", "harness_v3"), "snapshot_id": ctx.snapshot_id, "binding_id": invocation.binding_id},
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            # Lost the race on the side-effect claim; let the caller replay.
            replay = self.replay_or_block(invocation)
            if replay is not None:
                raise _Replayed(replay)
            raise
        self.db.refresh(row)
        return LedgerEntry(row=row, invocation=invocation)

    def finish(self, entry: LedgerEntry, result: ModuleResult, *, cache_result: bool = True) -> Receipt:
        row = entry.row
        payload: dict[str, Any] = {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "citations": list(result.citations),
            "artifacts": list(result.artifacts),
        }
        if result.success:
            row.status = "completed"
        else:
            code = str((result.error or {}).get("code") or "")
            if code in NOT_SENT_CODES or not entry.invocation.side_effecting:
                row.status = "failed"
                row.logical_action_key = None
            else:
                row.status = "outcome_unknown"
        row.result_json = _audit(payload)
        row.response_cache_json = payload if row.status == "completed" and cache_result else {}
        row.finished_at = utc_now()
        row.updated_at = utc_now()
        self.db.add(row)
        self.db.commit()
        return entry.receipt

    def deny(self, entry: LedgerEntry, reason: Mapping[str, Any]) -> Receipt:
        row = entry.row
        row.status = "denied"
        row.logical_action_key = None
        row.result_json = {"success": False, "error": dict(reason)}
        row.finished_at = utc_now()
        row.updated_at = utc_now()
        self.db.add(row)
        self.db.commit()
        return entry.receipt

    def cancel(self, entry: LedgerEntry) -> Receipt:
        row = entry.row
        row.status = "cancelled"
        row.logical_action_key = None
        row.finished_at = utc_now()
        row.updated_at = utc_now()
        self.db.add(row)
        self.db.commit()
        return entry.receipt

    # -- reconciliation -------------------------------------------------------

    def unknown_outcomes(self, tenant_id: str, *, since: datetime | None = None) -> list[HarnessInvocationRecord]:
        stmt = select(HarnessInvocationRecord).where(
            HarnessInvocationRecord.tenant_id == tenant_id,
            HarnessInvocationRecord.status == "outcome_unknown",
        )
        if since is not None:
            stmt = stmt.where(HarnessInvocationRecord.started_at >= since)
        return list(self.db.exec(stmt.order_by(HarnessInvocationRecord.started_at)).all())

    def reconcile(self, row: HarnessInvocationRecord, *, status: str, result: Mapping[str, Any] | None = None) -> None:
        """Operator/automation resolves an ``outcome_unknown`` after checking the external system."""

        if row.status != "outcome_unknown":
            raise ValueError(f"invocation {row.id} is {row.status}, not outcome_unknown")
        if status not in {"completed", "failed"}:
            raise ValueError("reconcile status must be completed or failed")
        row.status = status
        if status == "failed":
            row.logical_action_key = None
        if result is not None:
            row.result_json = _audit(dict(result))
            if status == "completed":
                row.response_cache_json = dict(result)
        row.updated_at = utc_now()
        self.db.add(row)
        self.db.commit()


class _Replayed(Exception):
    def __init__(self, replay: tuple[ModuleResult, Receipt]):
        super().__init__("replayed")
        self.replay = replay
