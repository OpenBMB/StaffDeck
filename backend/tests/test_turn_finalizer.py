from types import SimpleNamespace

from app.core.turn_finalizer import TurnFinalizer
from app.session.session_schema import RouterDecision, StepAgentResult


def test_handoff_is_terminal_and_does_not_complete_skill() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    session = SimpleNamespace(id="session-1", active_step_id="handoff", active_skill_id="skill-1")

    result = TurnFinalizer.finalize(
        "tenant-1",
        session,
        None,
        RouterDecision(decision="handoff_human"),
        StepAgentResult(handoff=True),
        None,
        current_step_allows_handoff=lambda skill, step_id: True,
        create_handoff=lambda *args: calls.append("handoff"),
        record_event=lambda tenant, session_id, name, payload: events.append((name, payload)),
        should_complete=lambda *args: True,
        complete_skill=lambda *args: calls.append("complete"),
    )

    assert result == "handoff"
    assert calls == ["handoff"]
    assert events == []


def test_ignored_handoff_preserves_completion_decision_and_event_payload() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    session = SimpleNamespace(id="session-1", active_step_id="step-1", active_skill_id="skill-1")
    result = TurnFinalizer.finalize(
        "tenant-1",
        session,
        None,
        RouterDecision(decision="handoff_human"),
        StepAgentResult(handoff=True),
        None,
        current_step_allows_handoff=lambda skill, step_id: False,
        create_handoff=lambda *args: calls.append("handoff"),
        record_event=lambda tenant, session_id, name, payload: events.append((name, payload)),
        should_complete=lambda *args: True,
        complete_skill=lambda *args: calls.append("complete"),
    )

    assert result == "completed"
    assert calls == ["complete"]
    assert events[0][0] == "human_handoff_ignored"
    assert events[0][1]["step_handoff"] is True
