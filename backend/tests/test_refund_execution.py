from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.mock import (
    MockOrderRefundRequest,
    MockProductPurchaseRequest,
    mock_order_query,
    mock_order_refund,
    mock_product_purchase,
)
from app.api.mock import MockOrderQueryRequest
from app.core.harness_agent import HarnessAction, _finish_result
from app.core.task_request_compiler import TaskRequirement
from app.db.seed import DEMO_TOOLS, REFUND_SKILL


def test_optional_handoff_action_does_not_force_handoff() -> None:
    requirement = TaskRequirement(
        task_frame_id="task-refund",
        kind="sop",
        goal="查询退款资格",
        sop_context={
            "step": {
                "node_id": "check_refund_eligibility",
                "type": "tool_call",
                "allowed_actions": ["call_tool:order.query", "handoff_human"],
            }
        },
        allowed_transitions=[{"next_node_id": "collect_refund_reason"}],
    )

    result = _finish_result(
        requirement,
        HarnessAction(
            action="finish",
            status="completed",
            next_step_id="collect_refund_reason",
            reply_fragment="请提供退款原因。",
        ),
        [],
        [],
        [],
        [],
        action_count=1,
    )

    assert result.status == "completed"
    assert result.next_step_id == "collect_refund_reason"


def test_terminal_handoff_node_still_forces_handoff() -> None:
    requirement = TaskRequirement(
        task_frame_id="task-handoff",
        kind="sop",
        goal="转人工",
        sop_context={"step": {"node_id": "handoff", "type": "handoff"}},
    )

    result = _finish_result(
        requirement,
        HarnessAction(action="finish", status="completed", reply_fragment="为您转人工。"),
        [],
        [],
        [],
        [],
        action_count=1,
    )

    assert result.status == "handoff"


def test_terminal_handoff_node_normalizes_model_failure_to_handoff() -> None:
    requirement = TaskRequirement(
        task_frame_id="task-handoff-failed-model",
        kind="sop",
        goal="转人工",
        sop_context={"step": {"node_id": "handoff", "type": "handoff"}},
    )

    result = _finish_result(
        requirement,
        HarnessAction(
            action="finish",
            status="failed",
            reply_fragment="当前没有可用的人工转接能力。",
        ),
        [],
        [],
        [],
        [],
        action_count=1,
    )

    assert result.status == "handoff"


def test_refund_skill_executes_real_refund_tool() -> None:
    node_ids = [node["node_id"] for node in REFUND_SKILL["nodes"]]
    nodes = {node["node_id"]: node for node in REFUND_SKILL["nodes"]}
    refund_tool = next(tool for tool in DEMO_TOOLS if tool["name"] == "order.refund")

    assert node_ids[-2:] == ["collect_refund_reason", "execute_refund"]
    assert nodes["check_refund_eligibility"]["capability_refs"] == {
        "tool_ids": ["order.query"],
        "required_tool_ids": ["order.query"]
    }
    assert nodes["execute_refund"]["capability_refs"] == {
        "tool_ids": ["order.refund"],
        "required_tool_ids": ["order.refund"]
    }
    assert REFUND_SKILL["start_node_id"] == "identify_refund_intent"
    assert REFUND_SKILL["terminal_node_ids"] == ["execute_refund"]
    assert [
        (edge["source_node_id"], edge["next_node_id"])
        for edge in REFUND_SKILL["edges"]
    ] == [
        ("identify_refund_intent", "collect_order_info"),
        ("collect_order_info", "confirm_refund_order"),
        ("confirm_refund_order", "check_refund_eligibility"),
        ("check_refund_eligibility", "collect_refund_reason"),
        ("collect_refund_reason", "execute_refund"),
    ]
    assert refund_tool["allowed_skills_json"] == ["after_sales_refund"]
    assert refund_tool["input_schema"]["required"] == ["order_id", "refund_reason"]


def test_mock_refund_updates_order_and_is_idempotent() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        purchase = mock_product_purchase(
            MockProductPurchaseRequest(user_id="hm", product_id="A1", quantity=1),
            db,
        )
        request = MockOrderRefundRequest(
            order_id=purchase["order_id"],
            refund_reason="不想要了",
        )

        first = mock_order_refund(request, db)
        second = mock_order_refund(request, db)
        queried = mock_order_query(MockOrderQueryRequest(order_id=purchase["order_id"]), db)

    assert first["refunded"] is True
    assert first["status"] == "refunded"
    assert first["refund_id"].startswith("REF")
    assert second["refunded"] is True
    assert second["idempotent_replay"] is True
    assert queried["status"] == "refunded"
    assert queried["refundable"] is False
