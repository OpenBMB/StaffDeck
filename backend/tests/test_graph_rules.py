from app.core.graph_rules import GraphRules


def _graph() -> dict:
    return {
        "start_node_id": "start",
        "required_info": ["message_content"],
        "terminal_node_ids": ["report"],
        "nodes": [
            {"node_id": "report", "allowed_actions": ["answer_user"]},
            {"node_id": "start", "allowed_actions": ["continue_flow"]},
            {"node_id": "check_sensitive", "allowed_actions": ["continue_flow"]},
            {"node_id": "check_payee", "allowed_actions": ["continue_flow"]},
        ],
        "edges": [
            {
                "source_node_id": "start",
                "next_node_id": "check_sensitive",
                "condition": "ready",
                "priority": 1,
            },
            {
                "source_node_id": "start",
                "next_node_id": "check_payee",
                "condition": "ready",
                "priority": 0,
            },
            {"source_node_id": "check_payee", "next_node_id": "report", "priority": 0},
            {"source_node_id": "check_sensitive", "next_node_id": "report", "priority": 0},
        ],
    }


def test_graph_runtime_preserves_legacy_order_and_parallel_siblings() -> None:
    content = _graph()

    assert [node["node_id"] for node in GraphRules.ordered_nodes(content)] == [
        "start",
        "check_payee",
        "report",
        "check_sensitive",
    ]
    assert [step["step_id"] for step in GraphRules.next_steps(content, "start")] == [
        "check_payee",
        "check_sensitive",
    ]
    assert GraphRules.sibling_steps(content, "start", "check_payee") == ["check_sensitive"]


def test_graph_runtime_keeps_exclusive_conditions_out_of_pending_siblings() -> None:
    content = _graph()
    content["edges"][0]["condition"] = "sensitive"
    content["edges"][1]["condition"] = "payee"

    assert GraphRules.sibling_steps(content, "start", "check_payee") == []


def test_graph_runtime_normalizes_pending_without_reordering() -> None:
    assert GraphRules.normalize_pending_steps(
        [" check_sensitive ", "report", "check_sensitive", "", None]
    ) == ["check_sensitive", "report"]


def test_graph_runtime_default_next_step_matches_legacy_rules() -> None:
    content = _graph()
    assert GraphRules.default_next_step(content, "check_payee")["step_id"] == "report"
    assert GraphRules.default_next_step(content, "start") is None

    content["edges"][0]["condition"] = "else"
    assert GraphRules.default_next_step(content, "start")["step_id"] == "check_sensitive"


def test_graph_runtime_terminal_position_preserves_legacy_slot_semantics() -> None:
    content = _graph()

    assert not GraphRules.terminal_position(content, "report", {})
    assert GraphRules.terminal_position(content, "report", {"message_content": []})
    assert GraphRules.terminal_position(content, "report", {"message_content": "Golden report"})


def test_graph_runtime_legacy_node_defaults_and_dangling_edges() -> None:
    content = {
        "start_node_id": "missing",
        "nodes": [{"node_id": "start"}, {"node_id": "end"}, "invalid"],
        "edges": [
            {"source_node_id": "start", "next_node_id": "missing", "priority": 0},
            {"source_node_id": "start", "next_node_id": "end", "priority": 0},
        ],
    }

    assert [node["node_id"] for node in GraphRules.ordered_nodes(content)] == [
        "start",
        "end",
    ]
    assert [step["step_id"] for step in GraphRules.next_steps(content, "start")] == ["end"]
    assert GraphRules.current_step(content, "end") == {
        "step_id": "end",
        "node_id": "end",
        "type": None,
        "name": None,
        "instruction": None,
        "optional": False,
        "condition": None,
        "expected_user_info": [],
        "allowed_actions": [],
        "knowledge_scope": {},
        "retry_policy": {},
        "metadata": {},
    }
