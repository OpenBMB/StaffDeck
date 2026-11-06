from app.core.slot_hydration_policy import SlotHydrationPolicy
from app.db.models import ChatSession, Skill
from app.session.session_schema import (
    AwaitingInput,
    PendingTask,
    PlannedTaskFrame,
    RouterDecision,
    TurnPlan,
)


def _skill() -> Skill:
    return Skill(
        tenant_id="tenant_test",
        skill_id="purchase",
        version="1.0.0",
        name="Purchase",
        content_json={
            "required_info": ["user_name", "employee_id", "communication_style"],
            "nodes": [{"node_id": "collect", "expected_user_info": ["quantity"]}],
        },
    )


def test_hydrate_applies_all_structured_memory_to_primary_and_task_frames() -> None:
    decision = RouterDecision(
        decision="continue_active",
        target_skill_id="purchase",
        awaiting_input=AwaitingInput(expected_fields=["user_name", "quantity"]),
        task_frames=[PendingTask(task_id="task-1", target_skill_id="purchase")],
    )
    memory = [
        {
            "kind": "profile",
            "content": "小明",
            "metadata": {"key": "preferred_name"},
        },
        {
            "kind": "profile",
            "content": "E-1024",
            "metadata": {"key": "employee_id"},
        },
        {
            "kind": "preference",
            "content": "简洁回答",
            "metadata": {"key": "communication_style"},
        },
        {
            "kind": "fact",
            "content": "Asia/Shanghai",
            "metadata": {"key": "timezone"},
        },
    ]

    result = SlotHydrationPolicy.hydrate(
        ChatSession(
            id="session_test",
            tenant_id="tenant_test",
            active_skill_id="purchase",
            slots_json={"quantity": 2},
        ),
        decision,
        [_skill()],
        memory,
    )

    assert decision.slot_hints == {
        "preferred_name": "小明",
        "user_name": "小明",
        "employee_id": "E-1024",
        "communication_style": "简洁回答",
        "timezone": "Asia/Shanghai",
    }
    assert decision.awaiting_input is None
    assert decision.task_frames[0].slot_hints == {
        "preferred_name": "小明",
        "user_name": "小明",
        "employee_id": "E-1024",
        "communication_style": "简洁回答",
        "timezone": "Asia/Shanghai",
    }
    assert result["awaiting_input_expected_fields"] == []


def test_hydration_uses_structured_memory_without_replacing_existing_slots() -> None:
    skill = _skill()
    memory = [
        {
            "kind": "profile",
            "content": "小明",
            "metadata": {"key": "preferred_name"},
        },
        {
            "kind": "profile",
            "content": "E-1024",
            "metadata": {"key": "employee_id"},
        },
        {
            "kind": "preference",
            "content": "简洁回答",
            "metadata": {"key": "communication-style"},
        },
        {
            "kind": "conversation",
            "content": "不应进入 Slot",
            "metadata": {"key": "quantity"},
        },
        {"kind": "fact", "content": "没有稳定 key"},
    ]

    assert SlotHydrationPolicy.patch(
        skill,
        {"user_name": "已有", "employee_id": "CURRENT"},
        memory,
    ) == {
        "preferred_name": "小明",
        "communication_style": "简洁回答",
    }
    assert SlotHydrationPolicy.patch(skill, {"user_name": []}, memory) == {
        "preferred_name": "小明",
        "user_name": "小明",
        "employee_id": "E-1024",
        "communication_style": "简洁回答",
    }


def test_hydrate_plan_applies_profile_before_sop_task_persistence() -> None:
    plan = TurnPlan(
        decision="start_new_task",
        task_frames=[
            PlannedTaskFrame(
                task_id="task-purchase",
                kind="sop",
                decision="start_new_task",
                target_skill_id="purchase",
                user_intent="购买商品 A1",
                slot_hints={"product_id": "A1", "quantity": 1},
            ),
            PlannedTaskFrame(
                task_id="task-weather",
                kind="conversation",
                decision="answer_only",
                user_intent="查询天气",
            ),
        ],
    )
    memory = [
        {
            "kind": "profile",
            "content": "小明",
            "metadata": {"key": "preferred_name"},
        },
        {
            "kind": "profile",
            "content": "E-1024",
            "metadata": {"key": "employee_id"},
        },
        {
            "kind": "preference",
            "content": "简洁回答",
            "metadata": {"key": "communication_style"},
        },
    ]

    result = SlotHydrationPolicy.hydrate_plan(
        ChatSession(id="session_test", tenant_id="tenant_test"),
        plan,
        [_skill()],
        memory,
    )

    assert plan.task_frames[0].slot_hints == {
        "product_id": "A1",
        "quantity": 1,
        "preferred_name": "小明",
        "user_name": "小明",
        "employee_id": "E-1024",
        "communication_style": "简洁回答",
    }
    assert plan.task_frames[1].slot_hints == {}
    assert result == {
        "tasks": [
            {
                "task_id": "task-purchase",
                "target_skill_id": "purchase",
                "slots": {
                    "preferred_name": "小明",
                    "user_name": "小明",
                    "employee_id": "E-1024",
                    "communication_style": "简洁回答",
                },
            }
        ]
    }


def test_hydration_keeps_newest_value_for_duplicate_memory_keys() -> None:
    memory = [
        {
            "kind": "fact",
            "content": "E-NEW",
            "metadata": {"key": "employee_id"},
        },
        {
            "kind": "fact",
            "content": "E-OLD",
            "metadata": {"key": "employee_id"},
        },
    ]

    assert SlotHydrationPolicy.patch(_skill(), {}, memory)["employee_id"] == "E-NEW"


def test_hydration_projects_keyed_memory_even_without_a_loaded_skill() -> None:
    memory = [
        {
            "kind": "preference",
            "content": "中文",
            "metadata": {"key": "language"},
        }
    ]

    assert SlotHydrationPolicy.patch(None, {}, memory) == {"language": "中文"}
