from app.core.agent_loop import AgentLoop
from app.session.session_schema import StepAgentResult
from app.db.models import ChatSession, Skill


def _skill() -> Skill:
    return Skill(
        tenant_id="tenant_test",
        skill_id="skill_test",
        version="1.0.0",
        name="Test",
        content_json={"nodes": []},
    )


def test_skill_steps_keeps_ordered_nodes_patch_seam() -> None:
    loop = object.__new__(AgentLoop)
    loop._ordered_skill_nodes = lambda _skill: [{"node_id": "patched"}]

    assert loop._skill_steps(_skill())[0]["step_id"] == "patched"


def test_sibling_queue_keeps_edge_patch_seams() -> None:
    loop = object.__new__(AgentLoop)
    calls: list[str] = []
    loop._graph_outgoing_edges = lambda _skill: {
        "start": [
            {"next_node_id": "selected", "condition": "SAME"},
            {"next_node_id": "sibling", "condition": "same"},
        ]
    }

    def edge_condition(edge: dict) -> str:
        calls.append(str(edge["next_node_id"]))
        return str(edge["condition"]).lower()

    loop._edge_condition = edge_condition
    loop._graph_pending_steps = lambda _session: []
    stored: list[list[str]] = []
    loop._store_graph_pending_steps = (
        lambda _tenant_id, _session, pending: stored.append(pending)
    )

    loop._queue_graph_sibling_steps(
        "tenant_test",
        ChatSession(id="session_test", tenant_id="tenant_test"),
        _skill(),
        "start",
        "selected",
    )

    assert calls == ["selected", "sibling"]
    assert stored == [["sibling"]]


def test_intermediate_reply_node_does_not_complete_sop_when_graph_has_outgoing_edge() -> None:
    loop = object.__new__(AgentLoop)
    skill = Skill(
        tenant_id="tenant_test",
        skill_id="after_sales_refund",
        version="1.0.0",
        name="售后退款流程",
        content_json={
            "required_info": ["order_id", "refund_reason"],
            "nodes": [
                {
                    "node_id": "check_refund_eligibility",
                    "type": "tool_call",
                    "allowed_actions": ["answer_user", "continue"],
                },
                {
                    "node_id": "collect_refund_reason",
                    "type": "collect_info",
                    "allowed_actions": ["ask_user", "continue"],
                },
                {
                    "node_id": "execute_refund",
                    "type": "tool_call",
                    "allowed_actions": ["call_tool:order.refund", "answer_user"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "check_refund_eligibility",
                    "next_node_id": "collect_refund_reason",
                },
                {
                    "source_node_id": "collect_refund_reason",
                    "next_node_id": "execute_refund",
                },
            ],
            "terminal_node_ids": ["execute_refund"],
        },
    )
    session = ChatSession(
        id="session_test",
        tenant_id="tenant_test",
        active_skill_id=skill.skill_id,
        active_step_id="check_refund_eligibility",
        slots_json={"order_id": "ORDER-1", "refund_reason": "不想要了"},
    )

    should_complete = loop._should_complete_skill(
        skill,
        session,
        StepAgentResult(
            action="advance",
            next_step_id="check_refund_eligibility",
            is_step_completed=True,
        ),
        None,
    )

    assert should_complete is False
