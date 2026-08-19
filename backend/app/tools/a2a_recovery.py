from __future__ import annotations

import threading

from sqlmodel import Session, select

from app.db import engine
from app.db.models import A2ATaskRun, Tool, utc_now
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall


_RECOVERABLE_STATES = {"submitted", "working"}


def recover_a2a_client_tasks() -> None:
    """Resume durable outbound A2A tasks after an application restart."""

    with Session(engine) as db:
        run_ids = list(
            db.exec(
                select(A2ATaskRun.id).where(
                    A2ATaskRun.direction == "client",
                    A2ATaskRun.status.in_(_RECOVERABLE_STATES),
                )
            ).all()
        )
    for run_id in run_ids:
        threading.Thread(
            target=_recover_one,
            args=(str(run_id),),
            name=f"a2a-client-recovery-{run_id}",
            daemon=True,
        ).start()


def _recover_one(run_id: str) -> None:
    with Session(engine) as db:
        run = db.get(A2ATaskRun, run_id)
        if run is None or run.direction != "client" or run.status not in _RECOVERABLE_STATES:
            return
        if not run.tool_id or not run.invocation_id:
            run.status = "failed"
            run.error_json = {
                "code": "A2A_RECOVERY_INVALID",
                "message": "A2A 恢复记录缺少 tool_id 或 invocation_id。",
            }
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            db.add(run)
            db.commit()
            return
        tool = db.get(Tool, run.tool_id)
        if tool is None:
            run.status = "failed"
            run.error_json = {
                "code": "A2A_RECOVERY_TOOL_MISSING",
                "message": "A2A 恢复记录关联的工具已不存在。",
            }
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            db.add(run)
            db.commit()
            return
        request = run.request_json if isinstance(run.request_json, dict) else {}
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        ToolExecutor(db).execute(
            tenant_id=run.tenant_id,
            tool_call=ToolCall(name=tool.name, arguments=arguments),
            agent_id=run.agent_id,
            session_id=run.session_id,
            invocation_id=run.invocation_id,
        )
