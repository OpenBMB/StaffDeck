from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    ChatSession,
    HarnessTurnRecord,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse


TURN_LEASE_SECONDS = 900


class HarnessTurnConflict(RuntimeError):
    pass


class HarnessTurnClaim:
    def __init__(
        self,
        *,
        record: HarnessTurnRecord | None,
        replay: ChatTurnResponse | None = None,
    ) -> None:
        self.record = record
        self.replay = replay


class HarnessTurnStore:
    """Durable exactly-once receipts keyed by the caller's client_turn_id."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def claim(
        self,
        session: ChatSession,
        request: ChatTurnRequest,
    ) -> HarnessTurnClaim:
        client_turn_id = str(request.client_turn_id or "").strip()
        if not client_turn_id:
            return HarnessTurnClaim(record=None)
        digest = _request_digest(request)
        existing = self._find(session, client_turn_id)
        if existing is not None:
            return self._existing_claim(existing, digest)

        now = utc_now()
        record = HarnessTurnRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            client_turn_id=client_turn_id,
            request_digest=digest,
            lease_owner=new_id("hturnlease"),
            lease_expires_at=now + timedelta(seconds=TURN_LEASE_SECONDS),
        )
        self.db.add(record)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._find(session, client_turn_id)
            if existing is None:
                raise
            return self._existing_claim(existing, digest)
        self.db.refresh(record)
        return HarnessTurnClaim(record=record)

    def bind_user_message(
        self,
        record: HarnessTurnRecord | None,
        user_message_id: str,
    ) -> None:
        if record is None:
            return
        self._fenced_update(
            record,
            values={
                "user_message_id": user_message_id,
                "updated_at": utc_now(),
            },
        )

    def complete(
        self,
        record: HarnessTurnRecord | None,
        response: ChatTurnResponse,
    ) -> None:
        if record is None:
            return
        now = utc_now()
        self._fenced_update(
            record,
            values={
                "status": "completed",
                "response_json": response.model_dump(mode="json"),
                "finished_at": now,
                "updated_at": now,
            },
        )

    def finish_with_error(
        self,
        record: HarnessTurnRecord | None,
        *,
        status: str,
        code: str,
        message: str,
    ) -> None:
        if record is None or record.status != "started":
            return
        now = utc_now()
        self._fenced_update(
            record,
            values={
                "status": status,
                "error_json": {
                    "code": code,
                    "message": str(message)[:2_000],
                },
                "finished_at": now,
                "updated_at": now,
            },
        )

    def _find(
        self,
        session: ChatSession,
        client_turn_id: str,
    ) -> HarnessTurnRecord | None:
        return self.db.exec(
            select(HarnessTurnRecord).where(
                HarnessTurnRecord.tenant_id == session.tenant_id,
                HarnessTurnRecord.session_id == session.id,
                HarnessTurnRecord.client_turn_id == client_turn_id,
            )
        ).first()

    def _existing_claim(
        self,
        existing: HarnessTurnRecord,
        request_digest: str,
    ) -> HarnessTurnClaim:
        if existing.request_digest != request_digest:
            raise HarnessTurnConflict(
                "同一个 client_turn_id 不能用于不同的 Harness 请求。"
            )
        if existing.status == "completed" and existing.response_json:
            return HarnessTurnClaim(
                record=existing,
                replay=ChatTurnResponse.model_validate(existing.response_json),
            )
        if existing.status == "started":
            state = (
                "仍在执行"
                if existing.lease_expires_at > utc_now()
                else "执行结果未知，需先核对执行记录"
            )
            raise HarnessTurnConflict(
                f"该 client_turn_id 对应的 Harness turn {state}，不会重复执行。"
            )
        raise HarnessTurnConflict(
            "该 client_turn_id 已结束且不能自动重试；请使用新的 client_turn_id。"
        )

    def _fenced_update(
        self,
        record: HarnessTurnRecord,
        *,
        values: dict[str, Any],
    ) -> None:
        result = self.db.exec(
            update(HarnessTurnRecord)
            .where(
                HarnessTurnRecord.id == record.id,
                HarnessTurnRecord.status == "started",
                HarnessTurnRecord.lease_owner == record.lease_owner,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise HarnessTurnConflict(
                "Harness turn receipt 已由其他执行者更新。"
            )
        self.db.commit()
        self.db.refresh(record)


def _request_digest(request: ChatTurnRequest) -> str:
    payload = request.model_dump(
        mode="json",
        exclude={"session_id", "client_turn_id"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "HarnessTurnClaim",
    "HarnessTurnConflict",
    "HarnessTurnStore",
]
