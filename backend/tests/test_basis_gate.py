"""Basis deterministic gate + scheduled task guard tests."""
from __future__ import annotations

import importlib.util
import sys

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import ScheduledTask, Tenant
from app.scheduled_tasks import service as scheduled_service


def _load_gate_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "patrol_gate_test",
        "/root/data-platform/deploy/dags/zabbix/patrol_gate.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _task(**overrides: object) -> ScheduledTask:
    defaults: dict[str, object] = {
        "id": "sched_test",
        "tenant_id": "tenant_demo",
        "agent_id": "agent_demo",
        "created_by_user_id": "user_demo",
        "title": "测试任务",
        "prompt": "测试",
        "status": "active",
        "schedule_type": "daily",
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


def test_snapshot_contains_required_fields() -> None:
    gate = _load_gate_module()
    problems = [{
        "eventid": "1",
        "hostid": "h1",
        "host": "H1",
        "triggerid": "t1",
        "problem": "disk full",
        "severity": 4,
        "severity_name": "严重",
        "started_at": "2026-08-18T10:00:00+08:00",
        "duration_seconds": 60,
        "duration": "1分钟",
        "acknowledged": False,
        "tags": {},
    }]
    snapshot = gate._snapshot_from_problems(
        problems,
        {"active_unsuppressed": 1, "needs_attention": 1, "filtered_total": 1},
        "patrol_trace",
        "a" * 32,
        "gate-1",
    )
    assert snapshot["event_set_hash"]
    assert snapshot["event_fingerprint"]
    assert snapshot["policy_version"] == "zabbix-patrol-gate-v1"
    assert snapshot["cooldown_bucket"] > 0
    assert snapshot["events"][0]["event_id"] == "1"
    assert snapshot["events"][0]["filter_reason"] == ""
    assert "evidence_refs" in snapshot["events"][0]


def test_archived_task_run_now_rejected() -> None:
    with _test_session() as db:
        task = _task(id="sched_archived", status="archived")
        db.add(task)
        db.commit()
        with pytest.raises(Exception) as exc:
            scheduled_service.start_scheduled_task_async(db, task, scheduled_for=scheduled_service.utc_now(), manual=True)
        assert exc.value.status_code == 400


def test_paused_task_run_now_rejected() -> None:
    with _test_session() as db:
        task = _task(id="sched_paused", status="paused")
        db.add(task)
        db.commit()
        with pytest.raises(Exception) as exc:
            scheduled_service.start_scheduled_task_async(db, task, scheduled_for=scheduled_service.utc_now(), manual=True)
        assert exc.value.status_code == 400


def test_external_gate_task_cannot_run_via_agent_loop() -> None:
    with _test_session() as db:
        task = _task(
            id="sched_gate",
            status="active",
            metadata_json={"execution_mode": "external_gate", "gate_owned": True},
        )
        db.add(task)
        db.commit()
        with pytest.raises(Exception) as exc:
            scheduled_service.start_scheduled_task_async(db, task, scheduled_for=scheduled_service.utc_now(), manual=True)
        assert exc.value.status_code == 400
        assert "external_gate" in exc.value.detail
