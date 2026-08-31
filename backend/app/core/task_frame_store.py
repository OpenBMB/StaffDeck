from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.core.harness_session_cleanup import harness_storage_root
from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    new_id,
    utc_now,
)
from app.session.session_schema import PlannedTaskFrame, TurnPlan

TERMINAL_FRAME_STATUSES = {"completed", "cancelled", "failed"}
DEPENDENCY_WAITING_ERROR_CODES = {
    "DEPENDENCY_BLOCKED",  # compatibility with the first Harness v2 scheduler
    "DEPENDENCY_WAITING",
}
HARNESS_CONTEXT_KEY = "harness_v2"
FRAME_LEASE_SECONDS = 900
MAX_TASK_FRAMES_PER_TURN = 8


class TaskFrameClaimConflict(RuntimeError):
    pass


class TaskFrameStore:
    def __init__(self, db: Session):
        self.db = db

    def ensure_agent_loop(
        self,
        row: HarnessTaskFrameRecord,
    ) -> HarnessAgentLoopRecord:
        """Return the durable logical loop that owns this TaskFrame."""

        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id) if row.agent_loop_id else None
        loop_key = f"sop:{row.id}" if row.kind == "sop" else f"general:{row.session_id}"
        if loop is None:
            loop = self.db.exec(
                select(HarnessAgentLoopRecord).where(
                    HarnessAgentLoopRecord.session_id == row.session_id,
                    HarnessAgentLoopRecord.loop_key == loop_key,
                )
            ).first()
        if loop is None:
            loop = HarnessAgentLoopRecord(
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                loop_key=loop_key,
                kind="sop" if row.kind == "sop" else "general",
                status="active",
                owner_task_frame_record_id=row.id,
                skill_id=row.skill_id,
                workspace_scope_id=row.id,
            )
            self.db.add(loop)
            self.db.flush()
        else:
            loop.owner_task_frame_record_id = row.id
            loop.skill_id = row.skill_id
            loop.status = "active"
            loop.finished_at = None
            loop.updated_at = utc_now()
            loop.state_version = max(1, int(loop.state_version or 0) + 1)
            self.db.add(loop)
        if row.agent_loop_id != loop.id:
            row.agent_loop_id = loop.id
            row.updated_at = utc_now()
            self.db.add(row)
            self.db.flush()
        return loop

    def save_agent_loop_checkpoint(
        self,
        loop: HarnessAgentLoopRecord,
        checkpoint: dict[str, Any],
        *,
        status: str | None = None,
        last_run_id: str | None = None,
    ) -> None:
        loop.checkpoint_json = dict(checkpoint)
        if status is not None:
            loop.status = status
        if last_run_id is not None:
            loop.last_run_id = last_run_id
        loop.updated_at = utc_now()
        loop.state_version = max(1, int(loop.state_version or 0) + 1)
        if loop.status in {"completed", "failed", "cancelled"}:
            loop.finished_at = utc_now()
        else:
            loop.finished_at = None
        self.db.add(loop)

    def finish_agent_loop_for_frame(
        self,
        row: HarnessTaskFrameRecord,
        *,
        result_status: str,
        checkpoint: dict[str, Any],
        last_run_id: str | None,
    ) -> None:
        if not row.agent_loop_id:
            return
        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id)
        if loop is None:
            return
        if result_status == "awaiting_user":
            loop_status = "suspended"
        elif row.kind != "sop":
            loop_status = "active"
        elif result_status == "completed":
            loop_status = "completed"
        elif result_status == "cancelled":
            loop_status = "cancelled"
        elif result_status == "failed":
            loop_status = "failed"
        else:
            loop_status = "active"
        self.save_agent_loop_checkpoint(
            loop,
            checkpoint,
            status=loop_status,
            last_run_id=last_run_id,
        )

    def persist_plan(
        self,
        session: ChatSession,
        source_turn_id: str,
        plan: TurnPlan,
    ) -> list[HarnessTaskFrameRecord]:
        """Persist a bounded plan while retaining every TaskFrame's first effective workspace root.

        Updates the provided session and database rows. Active concurrent TaskFrames can still raise
        ``RuntimeError`` when their execution lease has not expired.
        """

        # Project session state before reconciling its durable TaskFrame rows.
        self._apply_updates(session, plan)
        # Bound one turn before calculating dependencies between its TaskFrames.
        bounded_frames = list(plan.task_frames[:MAX_TASK_FRAMES_PER_TURN])
        bounded_task_ids = {
            str(frame.task_id or "").strip()
            for frame in bounded_frames
            if str(frame.task_id or "").strip()
        }
        records: list[HarnessTaskFrameRecord] = []
        for sequence, frame in enumerate(bounded_frames):
            task_id = str(frame.task_id or "").strip()
            if not task_id:
                continue
            row = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == session.id,
                    HarnessTaskFrameRecord.task_id == task_id,
                )
            ).first()
            created = row is None
            if created:
                row = HarnessTaskFrameRecord(
                    tenant_id=session.tenant_id,
                    session_id=session.id,
                    source_turn_id=source_turn_id,
                    task_id=task_id,
                    workspace_root=str(
                        harness_storage_root(tenant_id=session.tenant_id, db=self.db)
                    ),
                )
            elif row.status in TERMINAL_FRAME_STATUSES:
                continue
            elif row.status == "running":
                if (
                    row.lease_expires_at is not None
                    and row.lease_expires_at <= utc_now()
                ):
                    self._abandon_stale_runs(row)
                    row.status = "queued"
                    row.lease_owner = None
                    row.lease_expires_at = None
                else:
                    raise RuntimeError(
                        f"TaskFrame {row.task_id} is already running."
                    )
            row.source_turn_id = source_turn_id
            if created:
                row.kind = frame.kind
            row.decision = frame.decision
            row.status = "queued"
            row.sequence = sequence
            if created:
                row.skill_id = frame.target_skill_id
            continues_active = bool(
                frame.kind == "sop"
                and frame.decision == "continue_active"
                and frame.target_skill_id == session.active_skill_id
            )
            if created:
                row.step_id = (
                    session.active_step_id
                    if continues_active and session.active_step_id
                    else frame.target_step_id
                )
            row.user_intent = frame.user_intent
            row.requirements_json = list(frame.requirements)
            base_slots = (
                dict(session.slots_json or {})
                if continues_active
                else dict(row.slots_json or {})
            )
            row.slots_json = {**base_slots, **dict(frame.slot_hints)}
            planned_dependencies = [
                dependency_id
                for dependency_id in frame.depends_on_task_ids
                if dependency_id in bounded_task_ids
                and dependency_id != task_id
            ]
            if created or planned_dependencies:
                row.depends_on_json = planned_dependencies
            row.error_json = {}
            row.updated_at = utc_now()
            row.state_version = max(1, int(row.state_version or 0) + 1)
            self.db.add(row)
            records.append(row)
        self.db.flush()
        self.project_session(session)
        return sorted(records, key=lambda row: row.sequence)

    def mark_running(self, row: HarnessTaskFrameRecord) -> None:
        lease_owner = new_id("hlease")
        expected_version = int(row.state_version or 0)
        attempt_no = max(0, int(row.attempt_no or 0)) + 1
        now = utc_now()
        lease_expires_at = now + timedelta(
            seconds=FRAME_LEASE_SECONDS
        )
        statement = (
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "queued",
                HarnessTaskFrameRecord.state_version == expected_version,
            )
            .values(
                status="running",
                attempt_no=attempt_no,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                updated_at=now,
                state_version=expected_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        result = self.db.exec(statement)
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} was claimed by another turn."
            )
        self.db.refresh(row)

    def save_requirement(
        self,
        row: HarnessTaskFrameRecord,
        payload: dict[str, Any],
        *,
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        expected_owner = lease_owner or row.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(row.attempt_no or 0)
        )
        if not expected_owner:
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} no longer owns a running lease."
            )
        now = utc_now()
        statement = (
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "running",
                HarnessTaskFrameRecord.lease_owner == expected_owner,
                HarnessTaskFrameRecord.attempt_no == expected_attempt,
                HarnessTaskFrameRecord.lease_expires_at > now,
            )
            .values(
                task_requirement_json=payload,
                step_id=row.step_id,
                slots_json=dict(row.slots_json or {}),
                lease_expires_at=now + timedelta(seconds=FRAME_LEASE_SECONDS),
                updated_at=now,
                state_version=HarnessTaskFrameRecord.state_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        with self.db.no_autoflush:
            result = self.db.exec(statement)
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} lease renewal was fenced."
            )
        self.db.refresh(row)

    def renew_running_lease(
        self,
        row: HarnessTaskFrameRecord,
        *,
        lease_owner: str,
        attempt_no: int,
    ) -> None:
        """Renew one claimed frame and its active run under the same fence."""

        if not lease_owner:
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} no longer owns a running lease."
            )
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=FRAME_LEASE_SECONDS)
        result = self.db.exec(
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "running",
                HarnessTaskFrameRecord.lease_owner == lease_owner,
                HarnessTaskFrameRecord.attempt_no == attempt_no,
                HarnessTaskFrameRecord.lease_expires_at > now,
            )
            .values(
                lease_expires_at=lease_expires_at,
                updated_at=now,
                state_version=HarnessTaskFrameRecord.state_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} lease renewal was fenced."
            )
        self.db.exec(
            update(HarnessRunRecord)
            .where(
                HarnessRunRecord.task_frame_record_id == row.id,
                HarnessRunRecord.status == "running",
                HarnessRunRecord.lease_owner == lease_owner,
                HarnessRunRecord.attempt_no == attempt_no,
            )
            .values(
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.refresh(row)

    def finish_frame(
        self,
        row: HarnessTaskFrameRecord,
        *,
        status: str,
        step_id: str | None,
        slots: dict[str, Any],
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        error = result.get("error")
        error_json = dict(error) if isinstance(error, dict) else {}
        should_fence = lease_owner is not None or row.status == "running"
        if should_fence:
            expected_owner = lease_owner or row.lease_owner
            expected_attempt = (
                int(attempt_no)
                if attempt_no is not None
                else int(row.attempt_no or 0)
            )
            if not expected_owner:
                raise TaskFrameClaimConflict(
                    f"TaskFrame {row.task_id} has no lease owner."
                )
            now = utc_now()
            statement = (
                update(HarnessTaskFrameRecord)
                .where(
                    HarnessTaskFrameRecord.id == row.id,
                    HarnessTaskFrameRecord.status == "running",
                    HarnessTaskFrameRecord.lease_owner == expected_owner,
                    HarnessTaskFrameRecord.attempt_no == expected_attempt,
                    HarnessTaskFrameRecord.lease_expires_at > now,
                )
                .values(
                    status=status,
                    step_id=step_id,
                    slots_json=dict(slots),
                    result_json=dict(result),
                    error_json=error_json,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    state_version=HarnessTaskFrameRecord.state_version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            with self.db.no_autoflush:
                update_result = self.db.exec(statement)
            if getattr(update_result, "rowcount", 0) != 1:
                self.db.rollback()
                raise TaskFrameClaimConflict(
                    f"TaskFrame {row.task_id} completion was fenced."
                )
            self.db.refresh(row)
            return
        row.status = status
        row.step_id = step_id
        row.slots_json = dict(slots)
        row.result_json = result
        row.error_json = error_json
        row.updated_at = utc_now()
        row.lease_owner = None
        row.lease_expires_at = None
        row.state_version += 1
        self.db.add(row)

    def start_run(
        self,
        row: HarnessTaskFrameRecord,
        *,
        requirement: dict[str, Any],
        capability_snapshot: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> HarnessRunRecord:
        expected_owner = lease_owner or row.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(row.attempt_no or 0)
        )
        run = HarnessRunRecord(
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            task_frame_record_id=row.id,
            agent_loop_id=row.agent_loop_id,
            task_id=row.task_id,
            source_turn_id=row.source_turn_id,
            status="running",
            attempt_no=expected_attempt,
            lease_owner=expected_owner,
            lease_expires_at=row.lease_expires_at,
            task_requirement_json=requirement,
            capability_snapshot_json=capability_snapshot,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def update_run_context(
        self,
        run: HarnessRunRecord,
        *,
        requirement: dict[str, Any],
        capability_snapshot: dict[str, Any],
    ) -> None:
        """Refresh the current-node projection without creating another run."""

        run.task_requirement_json = dict(requirement)
        run.capability_snapshot_json = dict(capability_snapshot)
        run.updated_at = utc_now()
        self.db.add(run)

    def finish_run(
        self,
        run: HarnessRunRecord,
        *,
        status: str,
        action_count: int,
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        expected_owner = lease_owner or run.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(run.attempt_no or 0)
        )
        if not expected_owner:
            raise TaskFrameClaimConflict(
                f"Harness run {run.id} no longer owns a running lease."
            )
        now = utc_now()
        statement = (
            update(HarnessRunRecord)
            .where(
                HarnessRunRecord.id == run.id,
                HarnessRunRecord.status == "running",
                HarnessRunRecord.lease_owner == expected_owner,
                HarnessRunRecord.attempt_no == expected_attempt,
                HarnessRunRecord.lease_expires_at > now,
            )
            .values(
                status=status,
                action_count=max(0, int(action_count)),
                result_json=dict(result),
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        with self.db.no_autoflush:
            update_result = self.db.exec(statement)
        if getattr(update_result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"Harness run {run.id} completion was fenced."
            )
        self.db.refresh(run)

    def defer_for_action_budget(
        self,
        rows: list[HarnessTaskFrameRecord],
    ) -> None:
        """Keep unstarted frames durable and resumable after the turn budget ends."""

        for row in rows:
            if row.status in TERMINAL_FRAME_STATUSES:
                continue
            row.status = "queued"
            row.result_json = {
                "task_frame_id": row.task_id,
                "status": "action_budget",
                "task_summary": (
                    "本轮共享 action budget 已耗尽，TaskFrame 尚未执行并保持排队。"
                ),
            }
            row.error_json = {"code": "TURN_ACTION_BUDGET_DEFERRED"}
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utc_now()
            row.state_version += 1
            self.db.add(row)

    def latest_awaiting_conversation(
        self,
        session: ChatSession,
    ) -> HarnessTaskFrameRecord | None:
        return self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.kind == "conversation",
                HarnessTaskFrameRecord.status == "awaiting_user",
            )
            .order_by(
                HarnessTaskFrameRecord.updated_at.desc(),
                HarnessTaskFrameRecord.created_at.desc(),
                HarnessTaskFrameRecord.sequence.desc(),
            )
        ).first()

    def finish_source_turn_running_runs(
        self,
        session_id: str,
        source_turn_id: str,
        *,
        status: str,
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> list[HarnessRunRecord]:
        filters = [
            HarnessRunRecord.session_id == session_id,
            HarnessRunRecord.source_turn_id == source_turn_id,
            HarnessRunRecord.status == "running",
        ]
        if lease_owner is not None:
            filters.append(HarnessRunRecord.lease_owner == lease_owner)
        if attempt_no is not None:
            filters.append(HarnessRunRecord.attempt_no == attempt_no)
        runs = self.db.exec(
            select(HarnessRunRecord).where(*filters)
        ).all()
        for run in runs:
            self.finish_run(
                run,
                status=status,
                action_count=run.action_count,
                result=dict(result),
            )
        return list(runs)

    def cancel_source_turn(
        self,
        session: ChatSession,
        source_turn_id: str,
    ) -> list[HarnessTaskFrameRecord]:
        """Cancel every nonterminal frame and running attempt created by one turn."""

        self.finish_source_turn_running_runs(
            session.id,
            source_turn_id,
            status="cancelled",
            result={
                "status": "cancelled",
                "task_summary": "用户取消了当前 Harness 执行。",
            },
        )
        rows = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.source_turn_id == source_turn_id,
                HarnessTaskFrameRecord.status.notin_(TERMINAL_FRAME_STATUSES),
            )
        ).all()
        cancelled_task_ids: set[str] = set()
        for row in rows:
            cancelled_task_ids.add(row.task_id)
            self.finish_frame(
                row,
                status="cancelled",
                step_id=row.step_id,
                slots=dict(row.slots_json or {}),
                result={
                    "task_frame_id": row.task_id,
                    "status": "cancelled",
                    "task_summary": "用户取消了当前 Harness 执行。",
                },
            )
            self.finish_agent_loop_for_frame(
                row,
                result_status="cancelled",
                checkpoint=self.agent_loop_checkpoint(row),
                last_run_id=None,
            )
        if self.active_task_frame_id(session) in cancelled_task_ids:
            self.set_active_task_frame(session, None)
        self.project_session(session)
        return list(rows)

    def agent_loop_checkpoint(
        self,
        row: HarnessTaskFrameRecord,
    ) -> dict[str, Any]:
        if not row.agent_loop_id:
            return {}
        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id)
        return dict(loop.checkpoint_json or {}) if loop is not None else {}

    def dependencies_satisfied(
        self,
        row: HarnessTaskFrameRecord,
        records: list[HarnessTaskFrameRecord],
    ) -> bool:
        dependency_ids = {
            str(task_id)
            for task_id in row.depends_on_json or []
            if str(task_id).strip()
        }
        if not dependency_ids:
            return True
        status_by_id = {item.task_id: item.status for item in records}
        missing_ids = dependency_ids - set(status_by_id)
        if missing_ids:
            persisted = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == row.session_id,
                    HarnessTaskFrameRecord.task_id.in_(missing_ids),
                )
            ).all()
            status_by_id.update(
                {item.task_id: item.status for item in persisted}
            )
        return all(
            status_by_id.get(task_id) == "completed"
            for task_id in dependency_ids
        )

    def defer_for_dependencies(
        self,
        row: HarnessTaskFrameRecord,
    ) -> None:
        """Keep a dependent frame queued until every prerequisite completes."""

        row.status = "queued"
        row.result_json = {
            "task_frame_id": row.task_id,
            "status": "blocked",
            "reply_fragment": "前置任务完成后将自动继续该任务。",
            "task_summary": "TaskFrame 正在等待前置任务完成。",
            "action_count": 0,
            "error": {"code": "DEPENDENCY_WAITING"},
        }
        row.error_json = {"code": "DEPENDENCY_WAITING"}
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = utc_now()
        row.state_version += 1
        self.db.add(row)

    def ready_dependency_frames(
        self,
        session: ChatSession,
        *,
        exclude_task_ids: set[str] | None = None,
    ) -> list[HarnessTaskFrameRecord]:
        """Return durable follow-up frames whose prerequisites are complete.

        Old ``blocked/DEPENDENCY_BLOCKED`` rows are repaired here so sessions
        created before dependency waiting became resumable are not stranded.
        """

        excluded = set(exclude_task_ids or set())
        all_rows = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
            )
        ).all()
        status_by_id = {item.task_id: item.status for item in all_rows}
        candidates: list[HarnessTaskFrameRecord] = []
        for item in all_rows:
            dependency_ids = [
                str(task_id)
                for task_id in item.depends_on_json or []
                if str(task_id).strip()
            ]
            if (
                item.task_id in excluded
                or not dependency_ids
                or item.status not in {"queued", "blocked"}
            ):
                continue
            if item.status == "blocked" and str(
                (item.error_json or {}).get("code") or ""
            ) not in DEPENDENCY_WAITING_ERROR_CODES:
                continue
            if not all(
                status_by_id.get(task_id) == "completed"
                for task_id in dependency_ids
            ):
                continue
            if item.status == "blocked":
                item.status = "queued"
                item.result_json = {}
                item.error_json = {}
                item.updated_at = utc_now()
                item.state_version += 1
                self.db.add(item)
            candidates.append(item)
        self.db.flush()
        return sorted(
            candidates,
            key=lambda item: (
                item.created_at,
                item.sequence,
                item.task_id,
            ),
        )

    def dependency_results(
        self,
        row: HarnessTaskFrameRecord,
    ) -> list[dict[str, Any]]:
        """Project completed prerequisite results into the child TaskRequirement."""

        dependency_ids = [
            str(task_id)
            for task_id in row.depends_on_json or []
            if str(task_id).strip()
        ]
        if not dependency_ids:
            return []
        dependencies = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.session_id == row.session_id,
                HarnessTaskFrameRecord.task_id.in_(set(dependency_ids)),
                HarnessTaskFrameRecord.status == "completed",
            )
        ).all()
        by_id = {item.task_id: item for item in dependencies}
        projected: list[dict[str, Any]] = []
        for task_id in dependency_ids:
            dependency = by_id.get(task_id)
            if dependency is None:
                continue
            result = (
                dependency.result_json
                if isinstance(dependency.result_json, dict)
                else {}
            )
            projected.append(
                {
                    "task_frame_id": task_id,
                    "status": dependency.status,
                    "task_summary": result.get("task_summary"),
                    "slot_updates": result.get("slot_updates") or {},
                    "capability_results": result.get("capability_results") or [],
                    "artifacts": result.get("artifacts") or [],
                }
            )
        return projected

    def referenced_session_results(
        self,
        row: HarnessTaskFrameRecord,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Resolve durable prior capability results referenced by this frame's slots.

        A later TaskFrame may refer to an identifier produced by an earlier, unrelated
        frame (for example an order, document, ticket, or job id). Those frames do not
        have a planner dependency edge, but the model still needs the authoritative
        result behind the identifier instead of passing the opaque id to an unrelated
        lookup tool. Matching is exact, session-scoped, and only considers completed
        invocations, so no conversational history or sibling task is leaked.
        """

        reference_values = _reference_scalar_values(row.slots_json or {})
        if not reference_values:
            return []
        invocations = self.db.exec(
            select(HarnessInvocationRecord)
            .where(
                HarnessInvocationRecord.tenant_id == row.tenant_id,
                HarnessInvocationRecord.session_id == row.session_id,
                HarnessInvocationRecord.task_id != row.task_id,
                HarnessInvocationRecord.status == "completed",
            )
            .order_by(HarnessInvocationRecord.finished_at.desc())
            .limit(80)
        ).all()
        projected: list[dict[str, Any]] = []
        for invocation in invocations:
            result = (
                dict(invocation.response_cache_json or {})
                if isinstance(invocation.response_cache_json, dict)
                else {}
            )
            matches = sorted(reference_values.intersection(_all_scalar_values(result)))
            if not matches:
                continue
            projected.append(
                {
                    "task_frame_id": invocation.task_id,
                    "status": "completed",
                    "task_summary": (
                        f"此前能力 {invocation.tool_name} 的结果与当前任务引用匹配。"
                    ),
                    "slot_updates": {},
                    "capability_results": [
                        {
                            "tool_name": invocation.tool_name,
                            "arguments": dict(invocation.arguments_json or {}),
                            "result": result,
                        }
                    ],
                    "artifacts": [],
                    "reference_matches": matches,
                    "reference_source": "session_invocation",
                }
            )
            if len(projected) >= max(1, int(limit)):
                break
        return projected

    def project_session(self, session: ChatSession) -> None:
        rows = self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
            )
            .order_by(
                HarnessTaskFrameRecord.source_turn_id,
                HarnessTaskFrameRecord.sequence,
                HarnessTaskFrameRecord.created_at,
            )
        ).all()
        managed_ids = {row.task_id for row in rows}
        active_task_id = self.active_task_frame_id(session)
        legacy = [
            dict(item)
            for item in session.pending_tasks_json or []
            if isinstance(item, dict)
            and str(item.get("task_id") or "") not in managed_ids
        ]
        projected = [
            _legacy_projection(row)
            for row in rows
            if row.status not in TERMINAL_FRAME_STATUSES
            and row.kind == "sop"
            and row.task_id != active_task_id
        ]
        session.pending_tasks_json = [*legacy, *projected]
        session.updated_at = utc_now()
        self.db.add(session)

    def planner_state(self, session: ChatSession) -> list[dict[str, Any]]:
        rows = self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.status.notin_(TERMINAL_FRAME_STATUSES),
            )
            .order_by(
                HarnessTaskFrameRecord.updated_at.desc(),
                HarnessTaskFrameRecord.sequence,
            )
        ).all()
        active_task_id = self.active_task_frame_id(session)
        return [
            {
                "task_id": row.task_id,
                "kind": row.kind,
                "status": row.status,
                "skill_id": row.skill_id,
                "step_id": row.step_id,
                "user_intent": row.user_intent,
                "requirements": list(row.requirements_json or []),
                "slots": dict(row.slots_json or {}),
                "depends_on_task_ids": list(row.depends_on_json or []),
                "state_version": row.state_version,
                "active": row.task_id == active_task_id,
            }
            for row in rows
        ]

    def active_task_frame_id(self, session: ChatSession) -> str:
        state = (
            session.context_state_json
            if isinstance(session.context_state_json, dict)
            else {}
        )
        harness_state = state.get(HARNESS_CONTEXT_KEY)
        if isinstance(harness_state, dict):
            task_id = str(harness_state.get("active_task_frame_id") or "").strip()
            if task_id:
                return task_id
        awaiting = (
            session.awaiting_input_json
            if isinstance(session.awaiting_input_json, dict)
            else {}
        )
        return str(awaiting.get("task_id") or "").strip()

    def set_active_task_frame(
        self,
        session: ChatSession,
        row: HarnessTaskFrameRecord | None,
    ) -> None:
        state = dict(session.context_state_json or {})
        if row is None:
            state.pop(HARNESS_CONTEXT_KEY, None)
        else:
            state[HARNESS_CONTEXT_KEY] = {
                "active_task_frame_id": row.task_id,
                "kind": row.kind,
                "status": row.status,
                "state_version": row.state_version,
            }
        session.context_state_json = state
        session.updated_at = utc_now()
        self.db.add(session)

    def complete_active_frame(
        self,
        session: ChatSession,
        *,
        reason: str,
        task_id: str | None = None,
    ) -> HarnessTaskFrameRecord | None:
        resolved_task_id = str(
            task_id or self.active_task_frame_id(session) or ""
        ).strip()
        if not resolved_task_id:
            return None
        row = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.task_id == resolved_task_id,
            )
        ).first()
        if row is None or row.status in TERMINAL_FRAME_STATUSES:
            return row
        result = dict(row.result_json or {})
        result.update(
            {
                "task_frame_id": row.task_id,
                "status": "completed",
                "task_summary": reason,
            }
        )
        self.finish_frame(
            row,
            status="completed",
            step_id=row.step_id,
            slots=dict(row.slots_json or {}),
            result=result,
        )
        if resolved_task_id == self.active_task_frame_id(session):
            self.set_active_task_frame(session, None)
        return row

    def _apply_updates(self, session: ChatSession, plan: TurnPlan) -> None:
        for task_update in plan.task_updates:
            row = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == session.id,
                    HarnessTaskFrameRecord.task_id == task_update.task_id,
                )
            ).first()
            if row is None:
                continue
            if task_update.remove:
                row.status = "cancelled"
                row.lease_owner = None
                row.lease_expires_at = None
            elif task_update.status == "queued" and row.status not in {
                "running",
                *TERMINAL_FRAME_STATUSES,
            }:
                row.status = "queued"
            if task_update.user_intent is not None:
                row.user_intent = task_update.user_intent
            if task_update.slot_hints:
                row.slots_json = {
                    **dict(row.slots_json or {}),
                    **dict(task_update.slot_hints),
                }
            row.updated_at = utc_now()
            row.state_version += 1
            self.db.add(row)

    def _abandon_stale_runs(self, row: HarnessTaskFrameRecord) -> None:
        runs = self.db.exec(
            select(HarnessRunRecord).where(
                HarnessRunRecord.task_frame_record_id == row.id,
                HarnessRunRecord.status == "running",
            )
        ).all()
        for run in runs:
            run.status = "abandoned"
            run.result_json = {
                "status": "abandoned",
                "error": {"code": "LEASE_EXPIRED"},
            }
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            run.lease_owner = None
            run.lease_expires_at = None
            self.db.add(run)


def planned_frame_from_record(row: HarnessTaskFrameRecord) -> PlannedTaskFrame:
    decision = row.decision or (
        "answer_only"
        if row.kind == "conversation"
        else ("continue_active" if row.step_id else "start_new_task")
    )
    return PlannedTaskFrame(
        task_id=row.task_id,
        kind=row.kind,  # type: ignore[arg-type]
        status=(
            row.status
            if row.status
            in {
                "queued",
                "running",
                "awaiting_user",
                "blocked",
                "completed",
                "handoff",
                "failed",
                "cancelled",
            }
            else "queued"
        ),
        decision=decision,
        target_skill_id=row.skill_id,
        target_step_id=row.step_id,
        user_intent=row.user_intent,
        requirements=list(row.requirements_json or []),
        slot_hints=dict(row.slots_json or {}),
        depends_on_task_ids=list(row.depends_on_json or []),
    )


def _legacy_projection(row: HarnessTaskFrameRecord) -> dict[str, Any]:
    return {
        "task_id": row.task_id,
        "status": (
            "pending"
            if row.status in {"queued", "blocked", "action_budget"}
            else row.status
        ),
        "skill_id": row.skill_id,
        "target_skill_id": row.skill_id,
        "step_id": row.step_id,
        "target_step_id": row.step_id,
        "slots": dict(row.slots_json or {}),
        "slot_hints": dict(row.slots_json or {}),
        "intent_summary": row.user_intent,
        "user_intent": row.user_intent,
        "source_turn_id": row.source_turn_id,
        "resume_policy": "harness_v2",
        "updated_at": row.updated_at.isoformat(),
        "created_at": row.created_at.isoformat(),
    }


def _reference_scalar_values(value: Any) -> set[str]:
    """Return reference-like slot values without matching ordinary short entities."""

    return {
        item
        for item in _all_scalar_values(value)
        if len(item) >= 6
    }


def _all_scalar_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        collected: set[str] = set()
        for item in value.values():
            collected.update(_all_scalar_values(item))
        return collected
    if isinstance(value, list):
        collected = set()
        for item in value:
            collected.update(_all_scalar_values(item))
        return collected
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        normalized = str(value).strip()
        return {normalized} if normalized else set()
    return set()
