"""One-shot analysis executor for external deterministic gates.

The gate already produced an immutable snapshot; this module never rescans the
source system. It claims at most one event set per policy version and cooldown
bucket, makes exactly one structured LLM call, disables long-term memory capture,
and persists the result. On model failure it stores a fixed template so the raw
alert is never lost.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlmodel import Session

from app.db import engine
from app.db.models import ExternalGateEvent, new_id, utc_now
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation

CHINA_TZ = timezone(timedelta(hours=8))
POLICY_VERSION = "zabbix-patrol-gate-v1"
GATE_TASK_ID = "task-zabbix-patrol-gate"
BESS_AGENT_ID = "agent_6bd6e3403d244193"
TENANT_ID = "tenant_demo"
COOLDOWN_BUCKET_SECONDS = 7200
DEFAULT_MAX_RETRIES = 2
MAX_SUMMARY_LENGTH = 2000

_FALLBACK_ANALYSIS: dict[str, Any] = {
    "summary": "异常快照已持久化；本次异常分析模型调用失败，采用固定模板降级。",
    "likely_causes": [],
    "evidence_interpretation": "模型分析不可用，原始快照字段保持完整，需人工复核。",
    "recommended_checks": ["核对 Zabbix 事件原始快照与过滤原因", "按告警手册人工确认影响范围"],
    "urgency": "unknown",
    "needs_human": True,
    "unknowns": ["模型分析失败原因", "事件根因尚未判定"],
    "next_steps": ["查看 external_gate_events 中原始快照", "人工介入处理告警"],
}


def record_external_gate_event(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist one immutable event snapshot and return the durable record id.

    This is the gate outbox: the snapshot is stored before any LLM analysis and
    can be consumed exactly once by the analysis executor.
    """
    normalized = dict(snapshot)
    event_set_hash = str(normalized.get("event_set_hash") or "").strip()
    event_fingerprint = str(normalized.get("event_fingerprint") or "").strip()
    if not event_set_hash or not event_fingerprint:
        raise ValueError("异常快照缺少 event_set_hash 或 event_fingerprint")
    now = utc_now()
    with Session(engine) as db:
        row = ExternalGateEvent(
            tenant_id=str(normalized.get("tenant_id") or TENANT_ID),
            gate_task_id=str(normalized.get("gate_task_id") or GATE_TASK_ID),
            agent_id=str(normalized.get("agent_id") or BESS_AGENT_ID),
            event_set_hash=event_set_hash,
            policy_version=str(normalized.get("policy_version") or POLICY_VERSION),
            cooldown_bucket=int(normalized.get("cooldown_bucket") or 0),
            event_fingerprint=event_fingerprint,
            snapshot_json=normalized,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            existing = db.exec(
                select(ExternalGateEvent).where(
                    ExternalGateEvent.gate_task_id == row.gate_task_id,
                    ExternalGateEvent.event_set_hash == row.event_set_hash,
                    ExternalGateEvent.policy_version == row.policy_version,
                    ExternalGateEvent.cooldown_bucket == row.cooldown_bucket,
                )
            ).first()
            if existing:
                return _read_row(existing)
            raise
        db.refresh(row)
        return _read_row(row)


def claim_external_gate_event(
    event_set_hash: str,
    policy_version: str = POLICY_VERSION,
    cooldown_bucket: int | None = None,
    *,
    owner: str | None = None,
) -> dict[str, Any] | None:
    """Claim a pending event set exactly once per policy/cooldown bucket."""
    bucket = int(cooldown_bucket if cooldown_bucket is not None else 0)
    with Session(engine) as db:
        row = db.exec(
            select(ExternalGateEvent)
            .where(
                ExternalGateEvent.gate_task_id == GATE_TASK_ID,
                ExternalGateEvent.event_set_hash == event_set_hash,
                ExternalGateEvent.policy_version == policy_version,
                ExternalGateEvent.cooldown_bucket == bucket,
            )
            .order_by(ExternalGateEvent.created_at.asc())
        ).scalar_one_or_none()
        if row is None or row.status != "pending":
            return None
        row.status = "claimed"
        row.claim_owner = owner or f"gate:{GATE_TASK_ID}"
        row.claimed_at = utc_now()
        row.updated_at = utc_now()
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            return None
        db.refresh(row)
        return _read_row(row)


def analyze_external_gate_event(event_id: str) -> dict[str, Any]:
    """Run one structured analysis for a claimed gate event without a chat session."""
    from app.agents.branching import model_for_agent

    with Session(engine) as db:
        row = db.get(ExternalGateEvent, event_id)
        if row is None:
            raise ValueError(f"未找到 gate event: {event_id}")
        if row.status not in {"claimed", "failed"}:
            return _read_row(row)
        snapshot = dict(row.snapshot_json or {})
        model_config = model_for_agent(db, row.tenant_id, row.agent_id)
        if model_config is None:
            result = _fixed_analysis(row, "无可用模型配置")
            _persist_analysis(db, row, result)
            return _read_row(row)

        prompt = _analysis_prompt(snapshot)
        try:
            with llm_operation(
                "external_gate.alert_analysis",
                event_fingerprint=row.event_fingerprint,
                gate_run_id=str(snapshot.get("gate_run_id") or ""),
            ):
                raw = LLMClient(model_config).generate_json(
                    prompt, {"snapshot": json.dumps(snapshot, ensure_ascii=False)}
                )
            result = _normalize_analysis(raw)
            result["summary"] = str(result.get("summary") or "")[:MAX_SUMMARY_LENGTH]
            result["degraded"] = False
        except (LLMError, ValueError, TypeError) as exc:
            result = _fixed_analysis(row, str(exc))
        _persist_analysis(db, row, result)
        return _read_row(row)


def _analysis_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "你是 Zabbix 事件门控的一次性异常分析器。只分析下方不可变异常快照，"
        "不得重新巡检 Zabbix、不得执行任何操作、不得决定告警是否存在。"
        "输出严格 JSON，字段：summary、likely_causes、evidence_interpretation、"
        "recommended_checks、urgency、needs_human、unknowns、next_steps。"
    )


def _normalize_analysis(raw: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "summary",
        "likely_causes",
        "evidence_interpretation",
        "recommended_checks",
        "urgency",
        "needs_human",
        "unknowns",
        "next_steps",
    }
    result = {key: raw.get(key) for key in expected}
    for key in ("likely_causes", "recommended_checks", "unknowns", "next_steps"):
        value = result.get(key)
        result[key] = value if isinstance(value, list) else []
    result["needs_human"] = bool(result.get("needs_human"))
    result["urgency"] = str(result.get("urgency") or "unknown")
    return result


def _fixed_analysis(row: ExternalGateEvent, error: str) -> dict[str, Any]:
    analysis = dict(_FALLBACK_ANALYSIS)
    analysis["summary"] = f"{analysis['summary']} 错误: {error[:300]}"
    analysis["degraded"] = True
    return analysis


def _persist_analysis(db: Session, row: ExternalGateEvent, analysis: dict[str, Any]) -> None:
    row.status = "analyzed"
    row.analysis_json = analysis
    row.summary = str(analysis.get("summary") or "")[:MAX_SUMMARY_LENGTH]
    if analysis.get("degraded"):
        row.error = "模型分析失败，已使用固定模板降级"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)


def _read_row(row: ExternalGateEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "gate_task_id": row.gate_task_id,
        "agent_id": row.agent_id,
        "event_set_hash": row.event_set_hash,
        "policy_version": row.policy_version,
        "cooldown_bucket": row.cooldown_bucket,
        "event_fingerprint": row.event_fingerprint,
        "snapshot": row.snapshot_json or {},
        "status": row.status,
        "summary": row.summary,
        "analysis": row.analysis_json or {},
        "error": row.error,
        "retry_count": row.retry_count,
        "claim_owner": row.claim_owner,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def lookup_last_problem_for_trigger(trigger_id: str, host: str) -> dict[str, Any] | None:
    """查找同一 trigger+host 最近一条 problem 事件的分析结果。

    用于恢复事件对称过滤：如果问题事件被 Agent 判断为不严重（urgency=low/unknown
    且 needs_human=False），恢复事件也跳过通知。
    """
    with Session(engine) as db:
        rows = db.exec(
            select(ExternalGateEvent)
            .where(
                ExternalGateEvent.gate_task_id == GATE_TASK_ID,
                ExternalGateEvent.status == "analyzed",
            )
            .order_by(ExternalGateEvent.created_at.desc())
            .limit(100)
        ).all()
        for row in rows:
            snapshot = row.snapshot_json or {}
            if (
                snapshot.get("trigger_id") == trigger_id
                and snapshot.get("host") == host
                and not snapshot.get("is_recovery", False)
            ):
                return {
                    "event_id": row.id,
                    "urgency": (row.analysis_json or {}).get("urgency", "unknown"),
                    "needs_human": bool((row.analysis_json or {}).get("needs_human")),
                    "created_at": row.created_at.isoformat(),
                }
    return None
