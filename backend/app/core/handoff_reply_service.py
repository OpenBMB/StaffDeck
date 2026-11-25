"""Durable reply/resume service shared by web, channels and pluggable interaction hosts."""

from __future__ import annotations

import threading
from fastapi import HTTPException
from sqlalchemy import update
from sqlmodel import Session, select
from app.db import engine
from app.db.models import AgentEvent, ChatSession, HarnessTaskFrameRecord, HumanHandoffRequest, Message, utc_now
from app.session.session_schema import ChatTurnRequest


def apply_handoff_reply(
    db: Session,
    row: HumanHandoffRequest,
    reply: str,
    *,
    answered_by_user_id: str | None,
    source: str = "web",
    resume=None,
) -> None:
    """把一条 pending handoff 置为 answered 并触发 SOP 恢复。

    供网页 API(reply_human_handoff)与飞书 intake 回复分支复用。
    调用前需已完成权限校验与状态校验;本函数负责落库 + 事件 + 异步恢复。
    source: "web" 或 "feishu",由调用方显式指定(不再靠 user_id 前缀推断)。
    """
    from staffdeck_harness.handoff.core import authorize_reply

    reply = authorize_reply(db, row, reply, answered_by_user_id=answered_by_user_id, source=source)
    execution_failure = bool((row.metadata_json or {}).get("execution_failure"))
    if execution_failure:
        claimed = db.exec(update(HumanHandoffRequest).where(
            HumanHandoffRequest.id == row.id,
            HumanHandoffRequest.tenant_id == row.tenant_id,
            HumanHandoffRequest.status.in_(["pending", "assigned"]),
        ).values(id=HumanHandoffRequest.id).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="此接管请求已处理")
    now = utc_now()
    row.status = "answered"
    row.human_reply = reply
    row.answered_at = now
    row.updated_at = now
    row.resume_payload_json = {
        **(row.resume_payload_json or {}),
        "answered_by_user_id": answered_by_user_id,
    }
    db.add(row)

    chat_session = db.get(ChatSession, row.session_id)
    current_handoff = bool(chat_session and (chat_session.awaiting_input_json or {}).get("handoff_id") == row.id)
    if chat_session and chat_session.tenant_id == row.tenant_id and (not execution_failure or current_handoff):
        chat_session.status = "active"
        chat_session.awaiting_input_json = None
        chat_session.slots_json = {
            **dict(chat_session.slots_json or {}),
            "handoff_requested": False,
            "handoff_completed": True,
        }
        chat_session.summary = f"最近回复：{reply[:120]}"
        chat_session.updated_at = now
        db.add(chat_session)
    db.add(
        AgentEvent(
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            event_type="human_handoff_answered",
            payload_json={
                "handoff_id": row.id,
                "agent_id": row.agent_id,
                "trigger_skill_id": row.trigger_skill_id,
                "trigger_step_id": row.trigger_step_id,
                "answered_by_user_id": answered_by_user_id,
                "reply_preview": reply[:180],
                "source": source,
            },
            created_at=now,
        )
    )
    if execution_failure:
        if chat_session and chat_session.tenant_id == row.tenant_id and chat_session.agent_id == row.agent_id:
            # Record the human's reconciliation directly. Resuming the failed
            # node would allow a second write without a known provider outcome.
            task_id = (row.metadata_json or {}).get("task_frame_id")
            frame = db.exec(select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == row.tenant_id,
                HarnessTaskFrameRecord.session_id == row.session_id,
                HarnessTaskFrameRecord.task_id == task_id,
            )).first()
            if frame and frame.status == "handoff":
                frame.status = "cancelled"
                frame.updated_at = now
                frame.state_version += 1
                db.add(frame)
            if current_handoff and chat_session.active_skill_id == row.trigger_skill_id:
                chat_session.active_skill_id = None
                chat_session.active_step_id = None
                chat_session.resume_after_answer_json = None
                chat_session.skill_stack_json = []
            message = Message(tenant_id=row.tenant_id, session_id=row.session_id, role="assistant",
                              content=reply, metadata_json={"human_handoff_id": row.id})
            db.add_all([chat_session, message])
            from app.channels.service_outbox import stage_channel_delivery

            stage_channel_delivery(db, chat_session, message)
        db.commit()
        return
    db.commit()
    db.refresh(row)
    (resume or resume_human_handoff_async)(row.id)


def resume_human_handoff_async(handoff_id: str) -> None:
    thread = threading.Thread(target=resume_human_handoff_worker, args=(handoff_id,), daemon=True)
    thread.start()


def resume_human_handoff_worker(handoff_id: str) -> None:
    try:
        with Session(engine) as db:
            handoff = db.get(HumanHandoffRequest, handoff_id)
            if (not handoff or handoff.status != "answered" or not handoff.human_reply
                    or (handoff.metadata_json or {}).get("execution_failure")):
                return
            chat_session = db.get(ChatSession, handoff.session_id)
            if not chat_session or chat_session.tenant_id != handoff.tenant_id:
                return
            resume_payload = dict(handoff.resume_payload_json or {})
            original_channel = str(resume_payload.get("channel") or "").strip()
            original_binding_id = str(resume_payload.get("channel_binding_id") or "").strip()
            original_account_key = str(resume_payload.get("channel_account_key") or "").strip()
            original_target = resume_payload.get("channel_target")
            if original_channel:
                chat_session.channel = original_channel
            if original_binding_id:
                chat_session.channel_binding_id = original_binding_id
            if original_account_key:
                chat_session.channel_account_key = original_account_key
            if isinstance(original_target, dict) and original_target:
                chat_session.channel_target_json = dict(original_target)
            elif chat_session.channel_target_json:
                # Legacy handoffs predate the target snapshot. Keep the target
                # already anchored on the session, especially a WeCom group chatid.
                chat_session.channel_target_json = dict(chat_session.channel_target_json)
            db.add(chat_session)
            metadata = dict(handoff.metadata_json or {})
            if metadata.get("resume_started_at"):
                return
            now = utc_now()
            metadata["resume_started_at"] = now.isoformat()
            handoff.metadata_json = metadata
            db.add(handoff)
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_started",
                    payload_json={
                        "handoff_id": handoff.id,
                        "agent_id": handoff.agent_id,
                        "trigger_skill_id": handoff.trigger_skill_id,
                        "trigger_step_id": handoff.trigger_step_id,
                    },
                    created_at=now,
                )
            )
            db.commit()

            # 会话属主是恢复请求的权威 user:渠道身份重绑(懒建账号→web 账号)会迁移
            # session.user_id,而 handoff.requester_user_id 是创建时的快照,可能已过期;
            # 优先旧快照会触发 harness 的 session-user 围栏校验失败。
            request = ChatTurnRequest(
                tenant_id=handoff.tenant_id,
                session_id=handoff.session_id,
                agent_id=handoff.agent_id or chat_session.agent_id,
                user_id=chat_session.user_id or handoff.requester_user_id or None,
                message=handoff.human_reply,
                channel="human_handoff_resume",
                debug=False,
            )
            from app.core.agent_loop import AgentLoop

            AgentLoop(db).handle_turn(request)
            # resume turn 完成后不再写 resume_finished_at 标记:
            # _inject_handoff_context 已改为用 request.channel == "human_handoff_resume"
            # 判定 resume turn,时序可靠,无需事后标记。
    except Exception as exc:
        with Session(engine) as db:
            handoff = db.get(HumanHandoffRequest, handoff_id)
            if not handoff:
                return
            metadata = dict(handoff.metadata_json or {})
            metadata["resume_failed_at"] = utc_now().isoformat()
            metadata["resume_error"] = str(exc)[:300]
            handoff.status = "failed"
            handoff.metadata_json = metadata
            handoff.updated_at = utc_now()
            db.add(handoff)
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_failed",
                    payload_json={"handoff_id": handoff.id, "error": str(exc)[:300]},
                )
            )
            db.commit()
