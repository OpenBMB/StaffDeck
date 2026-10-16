import pytest

from app.knowledge.publication_adapter import KnowledgeDiscoveryValidationError, validate_discovered_skill
from app.skills.skill_schema import SkillCard

def test_skill_card_rejects_legacy_steps_and_accepts_graph() -> None:
    with pytest.raises(Exception):
        SkillCard(
            skill_id="skill_test",
            name="测试技能",
            steps=[
                {
                    "step_id": "collect",
                    "name": "收集信息",
                    "instruction": "收集用户信息",
                    "expected_user_info": ["name"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        )

    card = SkillCard(
        skill_id="skill_test",
        name="测试技能",
        nodes=[
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集信息",
                "instruction": "收集用户信息",
                "expected_user_info": ["name"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            },
        ],
        edges=[{"source_node_id": "collect", "next_node_id": "reply"}],
        start_node_id="collect",
        terminal_node_ids=["reply"],
    )

    assert card.start_node_id == "collect"
    assert card.terminal_node_ids == ["reply"]
    assert [node.node_id for node in card.nodes] == ["collect", "reply"]
    assert card.edges[0].source_node_id == "collect"
    assert card.edges[0].next_node_id == "reply"



def test_discovered_skill_rejects_unknown_fields_instead_of_dropping_them() -> None:
    payload = {
        "skill_id": "expense.travel_approval",
        "name": "差旅报销审批",
        "nodes": [
            {
                "node_id": "start",
                "type": "response",
                "name": "反馈结果",
                "instruction": "反馈结果。",
                "prompt": "这个字段不能被静默丢弃",
            }
        ],
        "edges": [],
        "start_node_id": "start",
        "terminal_node_ids": ["start"],
    }

    with pytest.raises(KnowledgeDiscoveryValidationError, match="nodes.0.*prompt"):
        validate_discovered_skill(payload)



def test_discovered_skill_requires_connected_start_to_terminal_graph() -> None:
    payload = {
        "skill_id": "expense.travel_approval",
        "name": "差旅报销审批",
        "nodes": [
            {
                "node_id": "start",
                "type": "collect_info",
                "name": "收集材料",
                "instruction": "收集材料。",
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "反馈结果",
                "instruction": "反馈结果。",
                "allowed_actions": ["answer_user"],
            },
            {
                "node_id": "orphan",
                "type": "response",
                "name": "孤立节点",
                "instruction": "处理孤立步骤。",
            },
        ],
        "edges": [{"source_node_id": "start", "next_node_id": "reply"}],
        "start_node_id": "start",
        "terminal_node_ids": ["reply"],
    }

    with pytest.raises(KnowledgeDiscoveryValidationError, match="无法从开始节点到达"):
        validate_discovered_skill(payload)

    payload["edges"].append({"source_node_id": "start", "next_node_id": "orphan"})
    with pytest.raises(KnowledgeDiscoveryValidationError, match="无法到达结束节点"):
        validate_discovered_skill(payload)
