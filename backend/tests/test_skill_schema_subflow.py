from app.skills.skill_schema import SkillCard


def test_subflow_node_is_normalized_to_orchestration_only() -> None:
    card = SkillCard.model_validate(
        {
            "skill_id": "parent",
            "name": "Parent",
            "nodes": [
                {
                    "node_id": "call_child",
                    "name": "Call child",
                    "type": "subflow",
                    "sub_sop_id": "child",
                    "instruction": "Do extra parent work",
                    "expected_user_info": ["parent_only"],
                    "allowed_actions": ["reply_user"],
                    "knowledge_scope": {"knowledge_base_ids": ["kb"]},
                    "capability_refs": {
                        "tool_ids": ["tool"],
                        "required_tool_ids": ["tool"],
                    },
                    "retry_policy": {"max_attempts": 3},
                }
            ],
            "start_node_id": "call_child",
            "terminal_node_ids": ["call_child"],
        }
    )

    node = card.nodes[0]
    assert node.instruction == ""
    assert node.expected_user_info == []
    assert node.allowed_actions == []
    assert node.knowledge_scope == {}
    assert node.capability_refs.tool_ids == []
    assert node.retry_policy == {}
