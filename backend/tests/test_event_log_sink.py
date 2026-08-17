from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.observability.event_log import EventLog


def _test_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_record_invokes_event_sink_with_traced_payload() -> None:
    received: list[tuple[str, dict]] = []

    def sink(event_type: str, payload: dict) -> None:
        received.append((event_type, dict(payload)))

    with _test_db() as db:
        events = EventLog(db, event_sink=sink)
        events.bind_turn("turn_1", "client_turn_1")
        events.record(
            "tenant_demo",
            "session_test",
            "step_result",
            {"reply": "ok"},
        )

    assert len(received) == 1
    event_type, payload = received[0]
    assert event_type == "step_result"
    assert payload["reply"] == "ok"
    assert payload["turn_id"] == "turn_1"
    assert payload["user_message_id"] == "turn_1"
    assert payload["client_turn_id"] == "client_turn_1"


def test_event_sink_none_does_not_raise() -> None:
    with _test_db() as db:
        events = EventLog(db)
        events.bind_turn("turn_1")
        event = events.record(
            "tenant_demo",
            "session_test",
            "step_result",
            {"reply": "ok"},
        )
    assert event.event_type == "step_result"
    assert event.payload_json["turn_id"] == "turn_1"


def test_event_sink_exception_does_not_propagate() -> None:
    def broken_sink(event_type: str, payload: dict) -> None:
        raise RuntimeError("sink exploded")

    with _test_db() as db:
        events = EventLog(db, event_sink=broken_sink)
        events.bind_turn("turn_1")
        event = events.record(
            "tenant_demo",
            "session_test",
            "step_result",
            {"reply": "ok"},
        )
    assert event.event_type == "step_result"
