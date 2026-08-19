from __future__ import annotations

import calendar
import re
import socket
import threading
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.branching import model_for_agent, visible_published_skills
from app.core import AgentLoop
from app.core.harness_turn_store import HarnessTurnConflict
from app.db import engine
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    ScheduledTask,
    ScheduledTaskRun,
    User,
    new_id,
    utc_now,
)
from app.llm import LLMClient, LLMError
from app.observability.spans import llm_operation
from app.scheduled_tasks.schema import (
    ScheduledTaskCreateRequest,
    ScheduledTaskDraftRead,
    ScheduledTaskRead,
    ScheduledTaskRunRead,
    ScheduledTaskUpdateRequest,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant
from app.skills.nesting import SopNestingError, expand_sop_for_execution


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TASK_TIME = "09:00"
LEASE_SECONDS = 15 * 60
WORKER_SLEEP_SECONDS = 5
MISFIRE_GRACE_SECONDS = max(30, WORKER_SLEEP_SECONDS * 2)
CONFLICT_RETRY_SECONDS = 15
SCHEDULE_TYPES = {"once", "daily", "weekly", "monthly"}
SOP_VERSION_POLICIES = {"latest", "pinned"}
SOP_SNAPSHOT_METADATA_KEY = "_sop_snapshot"


class ScheduledTaskAgentUnavailable(RuntimeError):
    pass


class _LLMScheduledTaskDraft(BaseModel):
    should_create: bool = False
    title: str = ""
    prompt: str = ""
    description: str | None = None
    schedule_type: str = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = None
    rrule: str | None = None
    confidence: float = 0.0
    reason: str | None = None


SCHEDULE_DRAFT_PROMPT = """
你是 StaffDeck 数字员工的自动任务配置解析器。
用户已经在对话框中选择了“创建定时任务”模式。请把用户输入整理成一个可编辑的自动任务草案。
如果用户没有写清时间计划，默认每天 09:00 执行；如果用户没有写清任务目标，用原始输入作为执行内容。

返回一个 JSON object，字段如下：
- should_create: boolean
- title: 12 到 32 个中文字符，概括自动任务名称
- prompt: 每次到点后交给数字员工的新会话任务描述，不要包含“帮我设个定时任务”等配置话术
- description: 可选，解释为什么这样拆解
- schedule_type: one of "once", "daily", "weekly", "monthly"
- schedule:
  - once: {"run_at": "YYYY-MM-DDTHH:mm:ss±HH:MM"}
  - daily: {"time": "HH:mm"}
  - weekly: {"time": "HH:mm", "weekdays": [0-6]}，0=周一，6=周日
  - monthly: {"time": "HH:mm", "day_of_month": 1-31}
- timezone: IANA 时区，默认使用 default_timezone
- rrule: 可选 RRULE 字符串
- confidence: 0 到 1
- reason: 简短说明

时间不完整时可以合理补齐：只说“每天”默认 09:00；只说“每周一”默认 09:00。
调度类型判断规则：
- 用户只给出一个具体时间点，例如“下午2点10分”“14:10”“今晚8点”，且没有明确“每天/每日/每周/每月/定期/重复”等周期要求时，生成 once。
- once.run_at 使用 now 所在日期和用户给出的时间；如果该时间已经过去，则顺延到下一天。
- 只有用户明确说“每天/每日/每晚/每早/每周/每月/工作日/定期/重复”等周期要求时，才生成 daily/weekly/monthly。
不要输出 Markdown，不要输出解释文本，只输出 JSON。
"""


def scheduled_task_read(row: ScheduledTask) -> ScheduledTaskRead:
    metadata = dict(row.metadata_json or {})
    metadata.pop(SOP_SNAPSHOT_METADATA_KEY, None)
    return ScheduledTaskRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        created_by_user_id=row.created_by_user_id,
        title=row.title,
        prompt=row.prompt,
        description=row.description,
        schedule_type=row.schedule_type,
        schedule=row.schedule_json or {},
        timezone=row.timezone,
        rrule=row.rrule,
        status=row.status,
        concurrency_policy=row.concurrency_policy,
        misfire_policy=row.misfire_policy,
        max_runs=row.max_runs,
        end_at=_dt(row.end_at),
        next_run_at=_dt(row.next_run_at),
        last_run_at=_dt(row.last_run_at),
        last_status=row.last_status,
        run_count=row.run_count,
        source_session_id=row.source_session_id,
        metadata=metadata,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def scheduled_task_run_read(row: ScheduledTaskRun, task: ScheduledTask | None = None) -> ScheduledTaskRunRead:
    return ScheduledTaskRunRead(
        id=row.id,
        tenant_id=row.tenant_id,
        scheduled_task_id=row.scheduled_task_id,
        task_title=task.title if task else None,
        task_status=task.status if task else None,
        agent_id=row.agent_id,
        user_id=row.user_id,
        session_id=row.session_id,
        scheduled_for=row.scheduled_for.isoformat(),
        status=row.status,
        started_at=_dt(row.started_at),
        finished_at=_dt(row.finished_at),
        result_summary=row.result_summary,
        error=row.error,
        trace=row.trace_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def create_scheduled_task(
    db: Session,
    request: ScheduledTaskCreateRequest,
    current_user: User,
) -> ScheduledTask:
    ensure_tenant(db, request.tenant_id)
    _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
    schedule = normalize_schedule(request.schedule_type, request.schedule, request.timezone)
    now = utc_now()
    end_at = parse_user_datetime(request.end_at, request.timezone) if request.end_at else None
    row = ScheduledTask(
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        created_by_user_id=current_user.id,
        title=_nonempty(request.title, "自动任务名称不能为空", 80),
        prompt=_nonempty(request.prompt, "自动任务描述不能为空", 10000),
        description=(request.description or "").strip() or None,
        schedule_type=request.schedule_type,
        schedule_json=schedule,
        timezone=request.timezone or DEFAULT_TIMEZONE,
        rrule=(request.rrule or "").strip() or build_rrule(request.schedule_type, schedule),
        status=request.status,
        concurrency_policy=request.concurrency_policy,
        misfire_policy=request.misfire_policy,
        max_runs=request.max_runs,
        end_at=end_at,
        source_session_id=request.source_session_id,
        metadata_json=_prepare_scheduled_task_sop_metadata(
            db,
            request.tenant_id,
            request.agent_id,
            request.metadata,
        ),
        created_at=now,
        updated_at=now,
    )
    row.next_run_at = compute_next_run_at(row, after=now)
    if row.status != "active":
        row.next_run_at = None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scheduled_task(
    db: Session,
    row: ScheduledTask,
    request: ScheduledTaskUpdateRequest,
    current_user: User,
) -> ScheduledTask:
    _ensure_task_access(row, current_user)
    agent_changed = False
    if request.agent_id is not None and request.agent_id != row.agent_id:
        _ensure_agent_access(db, request.tenant_id, request.agent_id, current_user)
        row.agent_id = request.agent_id
        agent_changed = True
    if request.title is not None:
        row.title = _nonempty(request.title, "自动任务名称不能为空", 80)
    if request.prompt is not None:
        row.prompt = _nonempty(request.prompt, "自动任务描述不能为空", 10000)
    if request.description is not None:
        row.description = request.description.strip() or None
    if request.timezone is not None:
        row.timezone = request.timezone or DEFAULT_TIMEZONE
    if request.schedule_type is not None:
        row.schedule_type = request.schedule_type
    if request.schedule is not None or request.schedule_type is not None or request.timezone is not None:
        row.schedule_json = normalize_schedule(row.schedule_type, request.schedule or row.schedule_json, row.timezone)
        row.rrule = request.rrule if request.rrule is not None else build_rrule(row.schedule_type, row.schedule_json)
    elif request.rrule is not None:
        row.rrule = request.rrule.strip() or None
    if request.status is not None:
        row.status = request.status
    if request.concurrency_policy is not None:
        row.concurrency_policy = request.concurrency_policy
    if request.misfire_policy is not None:
        row.misfire_policy = request.misfire_policy
    if request.max_runs is not None:
        row.max_runs = request.max_runs
    if request.end_at is not None:
        row.end_at = parse_user_datetime(request.end_at, row.timezone) if request.end_at else None
    if request.metadata is not None:
        row.metadata_json = _prepare_scheduled_task_sop_metadata(
            db,
            row.tenant_id,
            row.agent_id,
            request.metadata,
            existing=None if agent_changed else row.metadata_json,
        )
    elif agent_changed:
        row.metadata_json = _prepare_scheduled_task_sop_metadata(
            db,
            row.tenant_id,
            row.agent_id,
            row.metadata_json,
        )
    row.updated_at = utc_now()
    row.next_run_at = compute_next_run_at(row, after=utc_now()) if row.status == "active" else None
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def detect_scheduled_task_draft(
    db: Session,
    tenant_id: str,
    agent_id: str,
    user_id: str,
    message: str,
    source_session_id: str | None = None,
    timezone: str | None = None,
) -> ScheduledTaskDraftRead | None:
    ensure_tenant(db, tenant_id)
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id or agent.is_overall or agent.status != "active":
        return None
    user_timezone = _safe_timezone(timezone)
    llm_draft = _detect_with_llm(db, tenant_id, agent_id, message, user_timezone)
    if llm_draft is None or not llm_draft.should_create:
        return None
    draft = llm_draft
    try:
        schedule_type = _normalize_schedule_type(draft.schedule_type)
        draft_timezone = _safe_timezone(draft.timezone, user_timezone)
        schedule = normalize_schedule(schedule_type, draft.schedule, draft_timezone)
    except HTTPException:
        return None
    title = (draft.title or _compact_title(message)).strip()[:80]
    prompt = (draft.prompt or _execution_goal_from_message(message)).strip()
    if not prompt:
        return None
    return ScheduledTaskDraftRead(
        should_create=True,
        tenant_id=tenant_id,
        agent_id=agent_id,
        title=title,
        prompt=prompt,
        description=draft.description,
        schedule_type=schedule_type,
        schedule=schedule,
        timezone=draft_timezone,
        rrule=draft.rrule or build_rrule(schedule_type, schedule),
        confidence=draft.confidence,
        reason=draft.reason,
        source_session_id=source_session_id,
    )


def due_scheduled_tasks(db: Session, now: datetime | None = None, limit: int = 10) -> list[ScheduledTask]:
    now = now or utc_now()
    db.exec(
        update(ScheduledTask)
        .where(
            ScheduledTask.status == "active",
            ScheduledTask.end_at != None,  # noqa: E711
            ScheduledTask.end_at < now,  # type: ignore[operator]
        )
        .values(
            status="completed",
            next_run_at=None,
            lease_owner=None,
            lease_until=None,
            updated_at=now,
        )
    )
    db.commit()
    candidate_ids = db.exec(
        select(ScheduledTask.id)
        .where(
            ScheduledTask.status == "active",
            ScheduledTask.next_run_at <= now,  # type: ignore[operator]
            or_(ScheduledTask.end_at == None, ScheduledTask.end_at >= now),  # noqa: E711
            or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
        )
        .order_by(ScheduledTask.next_run_at)
        .limit(limit)
    ).all()
    lease_owner = f"{socket.gethostname()}:{new_id('worker')}"
    claimed: list[ScheduledTask] = []
    for task_id in candidate_ids:
        result = db.exec(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.status == "active",
                ScheduledTask.next_run_at <= now,  # type: ignore[operator]
                or_(ScheduledTask.end_at == None, ScheduledTask.end_at >= now),  # noqa: E711
                or_(ScheduledTask.lease_until == None, ScheduledTask.lease_until < now),  # noqa: E711
            )
            .values(
                lease_owner=lease_owner,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            continue
        row = db.get(ScheduledTask, task_id)
        if row:
            claimed.append(row)
    if claimed:
        db.commit()
        for row in claimed:
            db.refresh(row)
    return claimed


def execute_scheduled_task(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun:
    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    skipped = _skip_misfired_run(db, task, scheduled_for, manual)
    if skipped is not None:
        return skipped
    run = _prepare_scheduled_task_run(db, task, scheduled_for, manual)
    if run.status != "running" or not run.session_id:
        return run
    return _execute_prepared_scheduled_task(db, task, run, manual=manual)


def start_scheduled_task_async(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None = None,
    manual: bool = False,
) -> ScheduledTaskRun:
    scheduled_for = scheduled_for or task.next_run_at or utc_now()
    skipped = _skip_misfired_run(db, task, scheduled_for, manual)
    if skipped is not None:
        return skipped
    run = _prepare_scheduled_task_run(db, task, scheduled_for, manual)
    if run.status == "running" and run.session_id:
        threading.Thread(
            target=_execute_prepared_scheduled_task_in_background,
            args=(task.id, run.id, manual),
            daemon=True,
        ).start()
    return run


def _prepare_scheduled_task_run(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    manual: bool,
) -> ScheduledTaskRun:
    existing = db.exec(
        select(ScheduledTaskRun).where(
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.scheduled_for == scheduled_for,
        )
    ).first()
    if existing:
        if existing.status == "retrying":
            existing.status = "running"
            existing.error = None
            existing.started_at = utc_now()
            existing.finished_at = None
            existing.updated_at = utc_now()
            db.add(existing)
            db.commit()
            db.refresh(existing)
        return existing
    if task.concurrency_policy == "forbid":
        running = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.status == "running",
            )
        ).first()
        if running:
            run = _create_run(db, task, scheduled_for, "skipped")
            run.error = "上一轮自动任务仍在执行，已按 forbid 策略跳过本次唤醒。"
            run.finished_at = utc_now()
            _finish_task_schedule(db, task, scheduled_for, "skipped", manual)
            db.add(run)
            db.commit()
            db.refresh(run)
            return run

    run = _create_run(db, task, scheduled_for, "running")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.exec(
            select(ScheduledTaskRun).where(
                ScheduledTaskRun.scheduled_task_id == task.id,
                ScheduledTaskRun.scheduled_for == scheduled_for,
            )
        ).first()
        if existing:
            return existing
        raise
    db.refresh(run)
    session = ChatSession(
        id=new_id("session"),
        tenant_id=task.tenant_id,
        user_id=task.created_by_user_id,
        agent_id=task.agent_id,
        title=f"自动任务：{task.title}",
        status="active",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    run.session_id = session.id
    run.updated_at = utc_now()
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _skip_misfired_run(
    db: Session,
    task: ScheduledTask,
    scheduled_for: datetime,
    manual: bool,
) -> ScheduledTaskRun | None:
    if manual or task.misfire_policy != "skip":
        return None
    if scheduled_for >= utc_now() - timedelta(seconds=MISFIRE_GRACE_SECONDS):
        return None
    existing = db.exec(
        select(ScheduledTaskRun).where(
            ScheduledTaskRun.scheduled_task_id == task.id,
            ScheduledTaskRun.scheduled_for == scheduled_for,
        )
    ).first()
    if existing:
        return existing
    run = _create_run(db, task, scheduled_for, "skipped")
    run.error = "计划执行时间已超过补偿窗口，已按 skip 策略跳过。"
    run.finished_at = utc_now()
    _finish_task_schedule(db, task, scheduled_for, "skipped", manual=False)
    task.lease_owner = None
    task.lease_until = None
    db.add(task)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _execute_prepared_scheduled_task_in_background(task_id: str, run_id: str, manual: bool) -> None:
    with Session(engine) as db:
        task = db.get(ScheduledTask, task_id)
        run = db.get(ScheduledTaskRun, run_id)
        if not task or not run:
            return
        _execute_prepared_scheduled_task(db, task, run, manual=manual)


def _execute_prepared_scheduled_task(
    db: Session,
    task: ScheduledTask,
    run: ScheduledTaskRun,
    *,
    manual: bool,
) -> ScheduledTaskRun:
    try:
        if not run.session_id:
            raise RuntimeError("自动任务缺少独立会话")
        _ensure_scheduled_execution_agent(db, task)
        request = ChatTurnRequest(
            tenant_id=task.tenant_id,
            session_id=run.session_id,
            agent_id=task.agent_id,
            client_turn_id=run.id,
            user_id=task.created_by_user_id,
            message=automatic_task_message(task),
            channel="scheduled_task",
            interaction_mode="scheduled_task",
            forced_sop_id=_scheduled_task_sop_id(task),
            forced_sop_snapshot=_scheduled_task_sop_snapshot(task),
            client_timezone=task.timezone,
        )
        result: ChatTurnResponse | None = None
        for seq, item in enumerate(AgentLoop(db).handle_turn_stream(request), start=1):
            _record_scheduled_task_stream_event(db, run, run.session_id, seq, item)
            if item.get("event") in {"complete", "done"} and isinstance(item.get("data"), dict):
                result = ChatTurnResponse.model_validate(item["data"])
        if result is None:
            raise RuntimeError("自动任务执行未返回完整结果")
        outcome = _scheduled_harness_outcome(db, run, result)
        run.status = str(outcome["status"])
        run.result_summary = result.reply[:500]
        run.error = str(outcome.get("error") or "") or None
        run.trace_json = dict(outcome["trace"])
        run.finished_at = utc_now()
        _finish_task_schedule(db, task, run.scheduled_for, run.status, manual)
    except HarnessTurnConflict as exc:
        run.status = "retrying"
        run.error = str(exc)
        run.finished_at = None
        if not manual:
            task.next_run_at = utc_now() + timedelta(seconds=CONFLICT_RETRY_SECONDS)
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = utc_now()
        if run.session_id:
            _record_scheduled_task_stream_event(
                db,
                run,
                run.session_id,
                0,
                {"event": "error", "data": {"message": str(exc), "sessionId": run.session_id}},
            )
        _finish_task_schedule(db, task, run.scheduled_for, "failed", manual)
        if isinstance(exc, ScheduledTaskAgentUnavailable):
            task.status = "paused"
            task.next_run_at = None
    finally:
        task.lease_owner = None
        task.lease_until = None
        run.updated_at = utc_now()
        task.updated_at = utc_now()
        db.add(task)
        db.add(run)
        db.commit()
        db.refresh(run)
    return run


def _ensure_scheduled_execution_agent(db: Session, task: ScheduledTask) -> AgentProfile:
    agent = db.get(AgentProfile, task.agent_id)
    if (
        agent is None
        or agent.tenant_id != task.tenant_id
        or agent.is_overall
        or agent.status != "active"
    ):
        raise ScheduledTaskAgentUnavailable(
            "自动任务绑定的员工已不可用；请重新选择启用中的员工后再运行。"
        )
    return agent


def _scheduled_harness_outcome(
    db: Session,
    run: ScheduledTaskRun,
    result: ChatTurnResponse,
) -> dict[str, Any]:
    """Resolve one scheduled run from durable Harness state, never reply text."""

    receipt = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == run.tenant_id,
            HarnessTurnRecord.session_id == run.session_id,
            HarnessTurnRecord.client_turn_id == run.id,
        )
    ).first()
    if receipt is None or not receipt.user_message_id:
        raise RuntimeError("自动任务未进入 Harness v2，已拒绝按旧链路判定成功。")

    frames = db.exec(
        select(HarnessTaskFrameRecord)
        .where(
            HarnessTaskFrameRecord.tenant_id == run.tenant_id,
            HarnessTaskFrameRecord.session_id == run.session_id,
            HarnessTaskFrameRecord.source_turn_id == receipt.user_message_id,
        )
        .order_by(HarnessTaskFrameRecord.sequence, HarnessTaskFrameRecord.created_at)
    ).all()
    if not frames:
        raise RuntimeError("Harness v2 未生成 TaskFrame，自动任务不能判定为成功。")

    harness_runs = db.exec(
        select(HarnessRunRecord).where(
            HarnessRunRecord.tenant_id == run.tenant_id,
            HarnessRunRecord.session_id == run.session_id,
            HarnessRunRecord.source_turn_id == receipt.user_message_id,
        )
    ).all()
    run_ids = {item.id for item in harness_runs}
    invocations = (
        db.exec(
            select(HarnessInvocationRecord).where(
                HarnessInvocationRecord.tenant_id == run.tenant_id,
                HarnessInvocationRecord.session_id == run.session_id,
                HarnessInvocationRecord.run_id.in_(run_ids),
            )
        ).all()
        if run_ids
        else []
    )

    frame_payloads = [_scheduled_frame_payload(frame) for frame in frames]
    effective_statuses = [str(item["effective_status"]) for item in frame_payloads]
    if all(status == "completed" for status in effective_statuses):
        status = "succeeded"
        error = None
    elif "awaiting_user" in effective_statuses:
        status = "needs_input"
        error = "自动任务需要补充输入后才能继续。"
    elif any(
        item in {"failed", "cancelled", "handoff"}
        for item in effective_statuses
    ):
        status = "failed"
        error = "一个或多个 TaskFrame 执行失败。"
    else:
        status = "incomplete"
        error = "TaskFrame 尚未全部完成，请查看执行记录后继续或重试。"

    authorized_specific = _scheduled_sop_specific_capabilities(harness_runs)
    authorized_names = {
        str(item.get("name") or "")
        for item in authorized_specific
        if str(item.get("name") or "").strip()
    }
    specific_knowledge = {
        str(item.get("capability_id") or ""): str(item.get("name") or "")
        for item in authorized_specific
        if item.get("kind") == "knowledge"
        and str(item.get("capability_id") or "").strip()
    }
    invoked_specific: set[str] = set()
    for invocation in invocations:
        if invocation.tool_name in authorized_names:
            invoked_specific.add(invocation.tool_name)
        if invocation.tool_name != "knowledge_search" or not specific_knowledge:
            continue
        arguments = (
            invocation.arguments_json
            if isinstance(invocation.arguments_json, dict)
            else {}
        )
        raw_requested_ids = arguments.get("knowledge_base_ids")
        requested_values = (
            raw_requested_ids if isinstance(raw_requested_ids, list) else []
        )
        requested_ids = {
            str(item)
            for item in requested_values
            if str(item).strip()
        }
        included_ids = requested_ids or set(specific_knowledge)
        invoked_specific.update(
            name
            for knowledge_id, name in specific_knowledge.items()
            if knowledge_id in included_ids
        )
    sop_skill_ids = sorted(
        {
            str(frame.skill_id)
            for frame in frames
            if frame.kind == "sop" and frame.skill_id
        }
    )
    trace = {
        "execution_engine": "harness_v2",
        "harness_turn_id": receipt.id,
        "source_turn_id": receipt.user_message_id,
        "router_decision": (
            result.router_decision.model_dump(mode="json")
            if result.router_decision
            else None
        ),
        "session_state": result.session_state.model_dump(mode="json"),
        "task_frames": frame_payloads,
        "harness_run_ids": [item.id for item in harness_runs],
        "sop_scope": {
            "includes_sop": bool(sop_skill_ids),
            "skill_ids": sop_skill_ids,
            "sop_specific_authorized": authorized_specific,
            "sop_specific_invoked": sorted(invoked_specific),
        },
    }
    return {"status": status, "error": error, "trace": trace}


def _scheduled_frame_payload(frame: HarnessTaskFrameRecord) -> dict[str, Any]:
    result = frame.result_json if isinstance(frame.result_json, dict) else {}
    result_status = str(result.get("status") or "").strip()
    effective_status = result_status or frame.status
    return {
        "task_frame_id": frame.task_id,
        "kind": frame.kind,
        "skill_id": frame.skill_id,
        "step_id": frame.step_id,
        "status": frame.status,
        "effective_status": effective_status,
        "depends_on_task_ids": list(frame.depends_on_json or []),
        "error": dict(frame.error_json or {}),
    }


def _scheduled_sop_specific_capabilities(
    runs: list[HarnessRunRecord],
) -> list[dict[str, str]]:
    capabilities: dict[tuple[str, str, str], dict[str, str]] = {}
    for harness_run in runs:
        snapshot = (
            harness_run.capability_snapshot_json
            if isinstance(harness_run.capability_snapshot_json, dict)
            else {}
        )
        for raw in snapshot.get("available") or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("capability_scope") == "sop_specific":
                item = {
                    "capability_id": str(raw.get("capability_id") or ""),
                    "name": str(raw.get("name") or ""),
                    "kind": str(raw.get("kind") or ""),
                }
                capabilities[(item["capability_id"], item["name"], item["kind"])] = item
            if raw.get("kind") != "knowledge":
                continue
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            scope_by_base = metadata.get("knowledge_scope_by_base_id")
            if not isinstance(scope_by_base, dict):
                continue
            for base_id, scope in scope_by_base.items():
                if scope != "sop_specific":
                    continue
                item = {
                    "capability_id": str(base_id),
                    "name": f"knowledge_search:{base_id}",
                    "kind": "knowledge",
                }
                capabilities[(item["capability_id"], item["name"], item["kind"])] = item
    return [capabilities[key] for key in sorted(capabilities)]


def _record_scheduled_task_stream_event(
    db: Session,
    run: ScheduledTaskRun,
    session_id: str,
    seq: int,
    item: dict[str, Any],
) -> None:
    event = str(item.get("event") or "")
    data = item.get("data")
    if not isinstance(data, dict):
        data = {"value": data}
    payload = dict(data)
    payload.setdefault("sessionId", session_id)
    receipt = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == run.tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == run.id,
        )
    ).first()
    turn_id = str(
        payload.get("turn_id")
        or payload.get("turnId")
        or payload.get("user_message_id")
        or (receipt.user_message_id if receipt else "")
    ).strip()
    if turn_id:
        payload.setdefault("turn_id", turn_id)
        payload.setdefault("user_message_id", turn_id)
    payload.setdefault("client_turn_id", run.id)
    db.add(
        AgentEvent(
            tenant_id=run.tenant_id,
            session_id=session_id,
            event_type="scheduled_task_stream_event",
            payload_json={
                "run_id": run.id,
                "seq": seq,
                "event": event,
                "turn_id": turn_id or None,
                "user_message_id": turn_id or None,
                "client_turn_id": run.id,
                "data": payload,
            },
            created_at=utc_now(),
        )
    )
    run.updated_at = utc_now()
    db.add(run)
    db.commit()


def automatic_task_message(task: ScheduledTask) -> str:
    return task.prompt.strip() or task.title


def _scheduled_task_sop_id(task: ScheduledTask) -> str | None:
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    value = str(metadata.get("sop_id") or "").strip()
    return value or None


def _scheduled_task_sop_snapshot(task: ScheduledTask) -> dict[str, Any] | None:
    metadata = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    if str(metadata.get("sop_version_policy") or "latest") != "pinned":
        return None
    snapshot = metadata.get(SOP_SNAPSHOT_METADATA_KEY)
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _prepare_scheduled_task_sop_metadata(
    db: Session,
    tenant_id: str,
    agent_id: str,
    incoming: dict[str, Any] | None,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(incoming or {})
    # Internal snapshots are always produced by the server, never trusted from
    # an API payload.
    metadata.pop(SOP_SNAPSHOT_METADATA_KEY, None)
    sop_id = str(metadata.get("sop_id") or "").strip()
    if not sop_id:
        metadata.pop("sop_id", None)
        metadata.pop("sop_version_policy", None)
        metadata.pop("sop_version", None)
        return metadata

    previous = dict(existing or {})
    previous_policy = str(previous.get("sop_version_policy") or "latest")
    requested_policy = str(
        metadata.get("sop_version_policy")
        or (previous_policy if str(previous.get("sop_id") or "") == sop_id else "latest")
    ).strip()
    policy = requested_policy if requested_policy in SOP_VERSION_POLICIES else "latest"
    metadata["sop_id"] = sop_id
    metadata["sop_version_policy"] = policy
    available = visible_published_skills(db, tenant_id, agent_id)
    selected = next((skill for skill in available if skill.skill_id == sop_id), None)
    if selected is None:
        raise HTTPException(status_code=400, detail="指定的 SOP 当前不可用")
    if policy == "latest":
        metadata.pop("sop_version", None)
        return metadata

    previous_snapshot = previous.get(SOP_SNAPSHOT_METADATA_KEY)
    if (
        previous_policy == "pinned"
        and str(previous.get("sop_id") or "") == sop_id
        and isinstance(previous_snapshot, dict)
    ):
        metadata["sop_version"] = str(
            previous.get("sop_version") or previous_snapshot.get("version") or ""
        )
        metadata[SOP_SNAPSHOT_METADATA_KEY] = dict(previous_snapshot)
        return metadata

    try:
        expanded = expand_sop_for_execution(selected, available)
    except SopNestingError as exc:
        raise HTTPException(status_code=400, detail=f"指定的 SOP 无法生成版本快照：{exc}") from exc
    metadata["sop_version"] = selected.version
    metadata[SOP_SNAPSHOT_METADATA_KEY] = {
        "skill_id": selected.skill_id,
        "version": selected.version,
        "name": selected.name,
        "business_domain": selected.business_domain,
        "description": selected.description,
        "content_json": expanded.content_json,
    }
    return metadata


def compute_next_run_at(task: ScheduledTask, after: datetime | None = None) -> datetime | None:
    if task.schedule_type == "once":
        run_at = parse_user_datetime(str((task.schedule_json or {}).get("run_at") or ""), task.timezone)
        return run_at if run_at and run_at > (after or utc_now()) else None
    after_local = _to_local(after or utc_now(), task.timezone)
    schedule = task.schedule_json or {}
    if task.schedule_type == "daily":
        candidate = datetime.combine(after_local.date(), _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME)))
        candidate = candidate.replace(tzinfo=_tz(task.timezone))
        if candidate <= after_local:
            candidate += timedelta(days=1)
        return _to_utc_naive(candidate)
    if task.schedule_type == "weekly":
        weekdays = _normalize_weekdays(schedule.get("weekdays") or [after_local.weekday()])
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        best: datetime | None = None
        for offset in range(0, 8):
            day = after_local.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            candidate = datetime.combine(day, target_time).replace(tzinfo=_tz(task.timezone))
            if candidate <= after_local:
                continue
            if not best or candidate < best:
                best = candidate
        return _to_utc_naive(best) if best else None
    if task.schedule_type == "monthly":
        target_time = _parse_time(str(schedule.get("time") or DEFAULT_TASK_TIME))
        day_of_month = _normalize_day_of_month(schedule.get("day_of_month") or 1)
        year = after_local.year
        month = after_local.month
        for _ in range(14):
            day = min(day_of_month, calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, target_time.hour, target_time.minute, tzinfo=_tz(task.timezone))
            if candidate > after_local:
                return _to_utc_naive(candidate)
            month += 1
            if month > 12:
                year += 1
                month = 1
    return None


def normalize_schedule(schedule_type: str, schedule: dict[str, Any], timezone: str) -> dict[str, Any]:
    schedule_type = _normalize_schedule_type(schedule_type)
    _tz(timezone)
    raw = schedule or {}
    if schedule_type == "once":
        run_at = raw.get("run_at") or raw.get("datetime") or raw.get("start_at")
        parsed = parse_user_datetime(str(run_at or ""), timezone)
        if not parsed:
            raise HTTPException(status_code=400, detail="一次性自动任务需要填写执行时间")
        return {"run_at": _to_local(parsed, timezone).isoformat()}
    if schedule_type == "daily":
        return {"time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME)))}
    if schedule_type == "weekly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "weekdays": _normalize_weekdays(raw.get("weekdays") or [0]),
        }
    if schedule_type == "monthly":
        return {
            "time": _format_time(_parse_time(str(raw.get("time") or DEFAULT_TASK_TIME))),
            "day_of_month": _normalize_day_of_month(raw.get("day_of_month") or 1),
        }
    raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")


def build_rrule(schedule_type: str, schedule: dict[str, Any]) -> str | None:
    time_text = str(schedule.get("time") or DEFAULT_TASK_TIME)
    hour, minute = time_text.split(":", 1)
    if schedule_type == "once":
        return None
    if schedule_type == "daily":
        return f"FREQ=DAILY;BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "weekly":
        byday = ",".join(["MO", "TU", "WE", "TH", "FR", "SA", "SU"][int(day)] for day in schedule.get("weekdays", [0]))
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
    if schedule_type == "monthly":
        return (
            f"FREQ=MONTHLY;BYMONTHDAY={int(schedule.get('day_of_month') or 1)};"
            f"BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0"
        )
    return None


def parse_user_datetime(value: str, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz(timezone))
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _create_run(db: Session, task: ScheduledTask, scheduled_for: datetime, status: str) -> ScheduledTaskRun:
    run = ScheduledTaskRun(
        tenant_id=task.tenant_id,
        scheduled_task_id=task.id,
        agent_id=task.agent_id,
        user_id=task.created_by_user_id,
        scheduled_for=scheduled_for,
        status=status,
        started_at=utc_now() if status == "running" else None,
    )
    db.add(run)
    return run


def _finish_task_schedule(db: Session, task: ScheduledTask, scheduled_for: datetime, status: str, manual: bool) -> None:
    now = utc_now()
    task.last_run_at = now
    task.last_status = status
    task.run_count += 1
    if not manual:
        schedule_after = scheduled_for + timedelta(seconds=1)
        if task.misfire_policy in {"coalesce", "skip"}:
            schedule_after = max(schedule_after, now)
        next_run = compute_next_run_at(task, after=schedule_after)
        if task.max_runs is not None and task.run_count >= task.max_runs:
            task.status = "completed"
            task.next_run_at = None
        elif task.end_at and next_run and next_run > task.end_at:
            task.status = "completed"
            task.next_run_at = None
        else:
            task.next_run_at = next_run
            if task.schedule_type == "once" and next_run is None:
                # A one-shot run that still needs input or has deferred frames
                # must remain retryable instead of disappearing as completed.
                task.status = "completed" if status == "succeeded" else "paused"
    db.add(task)


def _detect_with_llm(
    db: Session,
    tenant_id: str,
    agent_id: str,
    message: str,
    timezone: str,
) -> _LLMScheduledTaskDraft | None:
    model_config = model_for_agent(db, tenant_id, agent_id, "router") or model_for_agent(db, tenant_id, agent_id)
    if not model_config:
        return None
    try:
        with llm_operation("scheduled_task.detect"):
            raw = LLMClient(model_config).generate_json(
                SCHEDULE_DRAFT_PROMPT,
                {
                    "now": _to_local(utc_now(), timezone).isoformat(),
                    "default_timezone": timezone,
                    "user_message": message,
                },
            )
        return _LLMScheduledTaskDraft.model_validate(raw)
    except (LLMError, ValidationError):
        return None


def _execution_goal_from_message(message: str) -> str:
    return message.strip()


def _compact_title(message: str) -> str:
    text = _execution_goal_from_message(message)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    return (text[:28] or "自动任务").strip()


def _normalize_schedule_type(value: str) -> str:
    if value not in SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail="不支持的自动任务调度类型")
    return value


def _normalize_weekdays(value: Any) -> list[int]:
    if not isinstance(value, list):
        value = [value]
    days = sorted({int(item) for item in value if str(item).strip() != ""})
    if not days or any(day < 0 or day > 6 for day in days):
        raise HTTPException(status_code=400, detail="每周自动任务需要 0-6 的星期设置")
    return days


def _normalize_day_of_month(value: Any) -> int:
    day = int(value)
    if day < 1 or day > 31:
        raise HTTPException(status_code=400, detail="每月执行日需要在 1 到 31 之间")
    return day


def _parse_time(value: str) -> time:
    text = value.strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", text)
    if not match:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=400, detail="时间格式需要为 HH:mm")
    return time(hour, minute)


def _format_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _tz(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=400, detail="无效时区") from exc


def _safe_timezone(value: str | None, fallback: str = DEFAULT_TIMEZONE) -> str:
    candidate = (value or "").strip() or fallback
    try:
        _tz(candidate)
        return candidate
    except HTTPException:
        _tz(fallback)
        return fallback


def _to_local(value: datetime, timezone: str) -> datetime:
    source = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return source.astimezone(_tz(timezone))


def _to_utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _nonempty(value: str, message: str, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=message)
    return text[:max_length]


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ensure_agent_access(db: Session, tenant_id: str, agent_id: str, current_user: User) -> AgentProfile:
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id or agent.is_overall or agent.status != "active":
        raise HTTPException(status_code=404, detail="员工不可用")
    if _is_admin_user(current_user):
        return agent
    metadata = agent.metadata_json or {}
    owns_agent = _agent_owned_by_user(agent, current_user)
    in_gallery = metadata.get("published_to_gallery") is True
    if not (owns_agent or in_gallery):
        raise HTTPException(status_code=403, detail="无权为该员工设置自动任务")
    return agent


def _ensure_task_access(row: ScheduledTask, current_user: User) -> None:
    if _is_admin_user(current_user):
        return
    if row.created_by_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该自动任务")
