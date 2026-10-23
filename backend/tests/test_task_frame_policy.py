from app.core.plan.frame_policy import TaskFramePolicy
from app.db.models import ChatSession
from app.session.session_schema import PendingTask, RouterDecision


def test_pending_decision_prefers_persisted_slots_and_identity() -> None:
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_test",
        pending_tasks_json=[
            {
                "task_id": "task-1",
                "skill_id": "purchase",
                "step_id": "collect",
                "slots": {"product_id": "A1"},
                "slot_hints": {"product_id": "ignored"},
                "intent_summary": "购买 A1",
            }
        ],
    )

    decision = TaskFramePolicy.decision_from_pending(session, "task-1")

    assert decision is not None
    assert decision.selected_task_id == "task-1"
    assert decision.target_skill_id == "purchase"
    assert decision.slot_hints == {"product_id": "A1"}


def test_turn_followup_frames_preserves_router_order() -> None:
    first = PendingTask(task_id="task-1", target_skill_id="purchase")
    second = PendingTask(task_id="task-2", target_skill_id="refund")
    decision = RouterDecision(
        decision="start_new_task",
        target_skill_id="purchase",
        task_frames=[first, second],
    )

    assert TaskFramePolicy.turn_followup_frames(decision) == [second]
    assert TaskFramePolicy.decision_from_turn_frame(second).task_frames == [second]


def test_next_pending_task_skips_non_pending_and_invalid_frames() -> None:
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_test",
        pending_tasks_json=[
            {"task_id": "done", "skill_id": "a", "status": "completed"},
            {"task_id": "missing-skill"},
            {"task_id": "next", "target_skill_id": "b"},
        ],
    )

    assert TaskFramePolicy.next_pending_task_id(session) == "next"


def test_reply_merge_and_empty_continuation_preserve_legacy_shape() -> None:
    assert TaskFramePolicy.merge_reply_segment(["one"], " two ") == (
        ["one", "two"],
        False,
    )
    assert TaskFramePolicy.merge_reply_segment(["one"], " ") == (["one"], False)
