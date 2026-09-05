from app.core.graph_rules import GraphRules
from app.skills.skill_schema import SkillCard, SkillGraphNode


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
        "sub_sop_id": None,
        "assignee_user_id": None,
        "assignee_notify_channel": None,
    }


def test_find_handoff_node_id_bfs_from_active_step() -> None:
    """Should find the handoff node reachable from active_step_id, not array order."""
    content = {
        "start_node_id": "intake",
        "nodes": [
            {"node_id": "intake", "allowed_actions": ["continue_flow"]},
            {"node_id": "handoff_a", "type": "handoff", "allowed_actions": ["handoff_human"]},
            {"node_id": "handoff_b", "type": "handoff", "allowed_actions": ["handoff_human"]},
        ],
        "edges": [
            {"source_node_id": "intake", "next_node_id": "handoff_b"},
            # handoff_a is NOT reachable from intake
        ],
    }
    assert GraphRules.find_handoff_node_id(content, "intake") == "handoff_b"


def test_find_handoff_node_id_fallback_to_start_when_no_active_step() -> None:
    """Without active_step_id, should BFS from start_node_id."""
    content = {
        "start_node_id": "start",
        "nodes": [
            {"node_id": "start", "allowed_actions": ["continue_flow"]},
            {"node_id": "mid", "allowed_actions": ["continue_flow"]},
            {"node_id": "handoff_a", "type": "handoff", "allowed_actions": ["handoff_human"]},
        ],
        "edges": [
            {"source_node_id": "start", "next_node_id": "mid"},
            {"source_node_id": "mid", "next_node_id": "handoff_a"},
        ],
    }
    assert GraphRules.find_handoff_node_id(content, None) == "handoff_a"


def test_find_handoff_node_id_fallback_to_array_order_without_edges() -> None:
    """With no edges, should fall back to array order."""
    content = {
        "start_node_id": "start",
        "nodes": [
            {"node_id": "start", "allowed_actions": ["continue_flow"]},
            {"node_id": "handoff_a", "type": "handoff"},
            {"node_id": "handoff_b", "type": "handoff"},
        ],
        "edges": [],
    }
    assert GraphRules.find_handoff_node_id(content, "start") == "handoff_a"


def test_find_handoff_node_id_detects_handoff_human_action() -> None:
    """Should detect handoff nodes by allowed_actions containing handoff_human."""
    content = {
        "start_node_id": "start",
        "nodes": [
            {"node_id": "start", "allowed_actions": ["continue_flow"]},
            {"node_id": "custom_node", "allowed_actions": ["handoff_human"]},
        ],
        "edges": [
            {"source_node_id": "start", "next_node_id": "custom_node"},
        ],
    }
    assert GraphRules.find_handoff_node_id(content, "start") == "custom_node"


def test_find_handoff_node_id_prefers_unconditional_path_over_conditional() -> None:
    """Should prefer handoff reachable via unconditional edge over conditional edge.

    当当前节点有两条出边:无条件边指向 handoff_a,条件边指向 handoff_b,
    BFS 第一阶段(仅无条件边)应返回 handoff_a,不会误入条件分支。
    """
    content = {
        "start_node_id": "decision",
        "nodes": [
            {"node_id": "decision", "allowed_actions": ["continue_flow"]},
            {"node_id": "handoff_a", "type": "handoff"},
            {"node_id": "handoff_b", "type": "handoff"},
        ],
        "edges": [
            {"source_node_id": "decision", "next_node_id": "handoff_a", "priority": 2},
            {"source_node_id": "decision", "next_node_id": "handoff_b", "condition": "故障未解决", "priority": 1},
        ],
    }
    # unconditional handoff_a should be preferred even though handoff_b has higher priority
    assert GraphRules.find_handoff_node_id(content, "decision") == "handoff_a"


def test_find_handoff_node_id_falls_back_to_conditional_edges() -> None:
    """When no unconditional path reaches a handoff node, should follow conditional edges."""
    content = {
        "start_node_id": "intake",
        "nodes": [
            {"node_id": "intake", "allowed_actions": ["continue_flow"]},
            {"node_id": "triage", "allowed_actions": ["continue_flow"]},
            {"node_id": "handoff_b", "type": "handoff"},
        ],
        "edges": [
            {"source_node_id": "intake", "next_node_id": "triage", "condition": "需要分诊"},
            {"source_node_id": "triage", "next_node_id": "handoff_b"},
        ],
    }
    # intake → triage is conditional, but phase 2 (all edges) should find handoff_b
    assert GraphRules.find_handoff_node_id(content, "intake") == "handoff_b"


def test_find_handoff_node_id_rejects_ambiguous_conditional_branches() -> None:
    content = {
        "start_node_id": "decision",
        "nodes": [
            {"node_id": "decision", "allowed_actions": ["continue_flow"]},
            {"node_id": "sales_handoff", "type": "handoff"},
            {"node_id": "support_handoff", "type": "handoff"},
        ],
        "edges": [
            {
                "source_node_id": "decision",
                "next_node_id": "sales_handoff",
                "condition": "售前咨询",
            },
            {
                "source_node_id": "decision",
                "next_node_id": "support_handoff",
                "condition": "售后故障",
            },
        ],
    }

    assert GraphRules.find_handoff_node_id(content, "decision") is None


def test_skill_graph_node_preserves_assignee_user_id() -> None:
    """SkillGraphNode schema should preserve assignee_user_id through Pydantic round-trip."""
    node = SkillGraphNode(
        node_id="handoff_node",
        type="handoff",
        name="转人工",
        assignee_user_id="user_abc",
    )
    dumped = node.model_dump()
    assert dumped["assignee_user_id"] == "user_abc"

    node_empty = SkillGraphNode(node_id="collect", name="收集")
    assert node_empty.assignee_user_id is None
    assert node_empty.model_dump()["assignee_user_id"] is None


def test_skill_graph_node_preserves_assignee_notify_channel() -> None:
    """SkillGraphNode schema should preserve assignee_notify_channel through round-trip."""
    node = SkillGraphNode(
        node_id="handoff_node",
        type="handoff",
        name="转人工",
        assignee_user_id="user_abc",
        assignee_notify_channel="feishu",
    )
    dumped = node.model_dump()
    assert dumped["assignee_notify_channel"] == "feishu"

    node_empty = SkillGraphNode(node_id="collect", name="收集")
    assert node_empty.assignee_notify_channel is None
    assert node_empty.model_dump()["assignee_notify_channel"] is None

    assert GraphRules.node_as_step(
        {"node_id": "handoff", "assignee_user_id": "user_abc", "assignee_notify_channel": "feishu"}
    )["assignee_notify_channel"] == "feishu"


def test_skill_card_round_trip_with_assignee_user_id() -> None:
    """Full SkillCard validation should preserve assignee_user_id on handoff nodes."""
    card = SkillCard(
        skill_id="test_sop",
        name="测试SOP",
        start_node_id="start",
        terminal_node_ids=["handoff"],
        nodes=[
            SkillGraphNode(node_id="start", name="开始"),
            SkillGraphNode(
                node_id="handoff",
                type="handoff",
                name="转人工",
                assignee_user_id="user_specialist",
            ),
        ],
        edges=[{"source_node_id": "start", "next_node_id": "handoff"}],
    )
    dumped = card.model_dump()
    handoff_node = next(n for n in dumped["nodes"] if n["node_id"] == "handoff")
    assert handoff_node["assignee_user_id"] == "user_specialist"
