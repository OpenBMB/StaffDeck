from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.harness_turn_store import (
    HarnessTurnConflict,
    HarnessTurnStore,
)
from app.core.harness_v2_engine import _with_recoverable_first_session
from app.db.models import ChatSession
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    SessionPublic,
)


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _request(message: str = "hello") -> ChatTurnRequest:
    return ChatTurnRequest(
        tenant_id="tenant-demo",
        session_id="session-1",
        client_turn_id="turn-client-1",
        message=message,
    )


def test_harness_turn_receipt_replays_completed_response() -> None:
    engine = _engine()
    with Session(engine) as db:
        session = ChatSession(id="session-1", tenant_id="tenant-demo")
        db.add(session)
        db.commit()
        store = HarnessTurnStore(db)

        claim = store.claim(session, _request())
        assert claim.record is not None
        assert claim.replay is None
        store.bind_user_message(claim.record, "message-1")
        expected = ChatTurnResponse(
            reply="done",
            session_id=session.id,
            session_state=SessionPublic(
                session_id=session.id,
                tenant_id=session.tenant_id,
            ),
        )
        store.complete(claim.record, expected)

        replay = store.claim(session, _request())
        assert replay.replay == expected


def test_harness_turn_receipt_blocks_in_progress_and_mismatched_reuse() -> None:
    engine = _engine()
    with Session(engine) as db:
        session = ChatSession(id="session-1", tenant_id="tenant-demo")
        db.add(session)
        db.commit()
        store = HarnessTurnStore(db)
        store.claim(session, _request())

        try:
            store.claim(session, _request())
        except HarnessTurnConflict as exc:
            assert "不会重复执行" in str(exc)
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("duplicate in-progress turn was not blocked")

        try:
            store.claim(session, _request("different"))
        except HarnessTurnConflict as exc:
            assert "不能用于不同" in str(exc)
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("mismatched client_turn_id reuse was not blocked")


def test_harness_turn_terminal_receipt_allows_only_cancel_or_completion() -> None:
    engine = _engine()
    with Session(engine) as db:
        session = ChatSession(id="session-1", tenant_id="tenant-demo")
        db.add(session)
        db.commit()
        store = HarnessTurnStore(db)

        cancelled = store.claim(session, _request()).record
        assert cancelled is not None
        assert store.cancel(cancelled) is True
        assert cancelled.status == "cancelled"
        try:
            store.begin_completion(cancelled)
        except HarnessTurnConflict:
            pass
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("cancelled receipt allowed normal completion")

        completing_request = _request().model_copy(
            update={"client_turn_id": "turn-client-2"}
        )
        completing = store.claim(session, completing_request).record
        assert completing is not None
        store.begin_completion(completing)
        assert completing.status == "finalizing"
        assert store.cancel(completing) is False
        response = ChatTurnResponse(
            reply="done",
            session_id=session.id,
            session_state=SessionPublic(
                session_id=session.id,
                tenant_id=session.tenant_id,
            ),
        )
        store.complete(completing, response)
        assert completing.status == "completed"


def test_first_turn_retry_without_returned_session_id_replays_original() -> None:
    engine = _engine()
    original_request = ChatTurnRequest(
        tenant_id="tenant-demo",
        user_id="user-1",
        client_turn_id="turn-client-first",
        message="first message",
    )
    recovered_request = _with_recoverable_first_session(original_request)
    assert recovered_request.session_id

    with Session(engine) as db:
        session = ChatSession(
            id=str(recovered_request.session_id),
            tenant_id=original_request.tenant_id,
            user_id=original_request.user_id,
        )
        db.add(session)
        db.commit()
        store = HarnessTurnStore(db)
        first = store.claim(session, original_request)
        expected = ChatTurnResponse(
            reply="done",
            session_id=session.id,
            session_state=SessionPublic(
                session_id=session.id,
                tenant_id=session.tenant_id,
                user_id=session.user_id,
            ),
        )
        store.complete(first.record, expected)

        retry_request = _with_recoverable_first_session(
            original_request.model_copy()
        )
        retry_session = db.get(ChatSession, retry_request.session_id)
        assert retry_session is not None
        retry = store.claim(retry_session, original_request.model_copy())

        assert retry.replay == expected
        assert retry.replay.session_id == recovered_request.session_id
