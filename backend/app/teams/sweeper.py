"""团队任务超时清扫:周期把滞留在 bidding/in_progress/review 的任务升级给人。

守护线程仅由 main.py on_startup 启动(单端口应用进程内单实例);
测试直接调用 sweep_timed_out_tasks,不起线程。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.db import engine
from app.db.models import Team, TeamTask, TeamWakeEvent, utc_now
from app.teams.service import apply_task_transition
from app.teams.wakeup import _drain_member_queue

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60.0
DEFAULT_TASK_TIMEOUT_MINUTES = 30.0
# 参与超时判定的任务状态:终态(done/escalated)与待启动(pending/rework)不扫
TIMEOUT_SCAN_STATUSES = ("bidding", "in_progress", "review")


def task_timeout_minutes(team: Team) -> float:
    """团队任务超时阈值(分钟):默认 30,非法配置(非 dict/非数字/非正数)回退默认。"""
    config = team.config_json if isinstance(team.config_json, dict) else {}
    try:
        value = float(config.get("task_timeout_minutes", DEFAULT_TASK_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_TASK_TIMEOUT_MINUTES
    return value if value > 0 else DEFAULT_TASK_TIMEOUT_MINUTES


def sweep_timed_out_tasks(db: Session, *, now: datetime | None = None) -> list[TeamTask]:
    """扫一轮超时任务:置 escalated 记审计,关联 pending 唤醒标记 failed(error=timeout)。

    in_progress 任务超时后释放成员执行额度,尝试出队该成员的排队唤醒。
    返回本次被升级的任务列表。
    """
    now = now or utc_now()
    rows = db.exec(
        select(TeamTask).where(TeamTask.status.in_(list(TIMEOUT_SCAN_STATUSES)))
    ).all()
    escalated: list[TeamTask] = []
    for task in rows:
        team = db.get(Team, task.team_id)
        if team is None:
            continue
        timeout_minutes = task_timeout_minutes(team)
        if now - task.updated_at < timedelta(minutes=timeout_minutes):
            continue
        previous = task.status
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "timeout", "timeout_minutes": timeout_minutes},
        )
        for wake in db.exec(
            select(TeamWakeEvent).where(
                TeamWakeEvent.team_id == team.id, TeamWakeEvent.status == "pending"
            )
        ).all():
            payload = wake.payload_json if isinstance(wake.payload_json, dict) else {}
            if str(payload.get("task_id") or "") != task.id:
                continue
            wake.status = "failed"
            wake.error = "timeout"
            wake.updated_at = utc_now()
            db.add(wake)
        db.commit()
        escalated.append(task)
        if previous == "in_progress" and task.assignee_agent_id:
            _drain_member_queue(db, team, task.assignee_agent_id)
    return escalated


_stop_event = threading.Event()
_sweeper_thread: threading.Thread | None = None


def _sweep_loop(interval_seconds: float) -> None:
    while not _stop_event.wait(max(1.0, interval_seconds)):
        try:
            with Session(engine) as db:
                sweep_timed_out_tasks(db)
        except Exception:  # 后台清扫不让异常杀死守护线程
            logger.exception("团队任务超时清扫失败")


def start_timeout_sweeper(*, interval_seconds: float = SWEEP_INTERVAL_SECONDS) -> None:
    global _sweeper_thread
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    _stop_event.clear()
    _sweeper_thread = threading.Thread(
        target=_sweep_loop,
        args=(interval_seconds,),
        name="team-task-timeout-sweeper",
        daemon=True,
    )
    _sweeper_thread.start()


def stop_timeout_sweeper() -> None:
    _stop_event.set()
