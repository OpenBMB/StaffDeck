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
        route_to_handoff_node=lambda session, skill: False,
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
        route_to_handoff_node=lambda session, skill: False,
        create_handoff=lambda *args: calls.append("handoff"),
        record_event=lambda tenant, session_id, name, payload: events.append((name, payload)),
        should_complete=lambda *args: True,
        complete_skill=lambda *args: calls.append("complete"),
    )

    assert result == "completed"
    assert calls == ["complete"]
    assert events[0][0] == "human_handoff_ignored"
    assert events[0][1]["step_handoff"] is True


def test_step_result_handoff_routes_to_handoff_node_when_current_step_disallows() -> None:
    """When step_result.handoff=True and current step doesn't declare handoff,
    the finalizer should call route_to_handoff_node before checking again."""
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    routed: list[bool] = []
    session = SimpleNamespace(id="session-1", active_step_id="collect", active_skill_id="skill-1")

    allow_handoff = [False, True]

    result = TurnFinalizer.finalize(
        "tenant-1",
        session,
        None,
        RouterDecision(decision="continue_active"),
        StepAgentResult(handoff=True),
        None,
        current_step_allows_handoff=lambda skill, step_id: allow_handoff.pop(0),
        route_to_handoff_node=lambda sess, skill: routed.append(True),
        create_handoff=lambda *args: calls.append("handoff"),
        record_event=lambda tenant, session_id, name, payload: events.append((name, payload)),
        should_complete=lambda *args: True,
        complete_skill=lambda *args: calls.append("complete"),
    )

    assert result == "handoff"
    assert calls == ["handoff"]
    assert routed == [True]
    assert events == []
