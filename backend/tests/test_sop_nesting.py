import pytest

from app.db.models import Skill
from app.skills.nesting import (
    SopNestingError,
    discoverable_sops,
    expand_sop_for_execution,
    expand_visible_sops,
    validate_sop_nesting,
)


def _skill(
    skill_id: str,
    *,
    nodes: list[dict],
    edges: list[dict] | None = None,
    start: str | None = None,
    terminals: list[str] | None = None,
    scope: str = "general",
    status: str = "published",
) -> Skill:
    node_ids = [str(node["node_id"]) for node in nodes]
    return Skill(
        tenant_id="tenant_test",
        skill_id=skill_id,
        version="1.0.0",
        name=skill_id,
        status=status,
        content_json={
            "skill_id": skill_id,
            "name": skill_id,
            "version": "1.0.0",
            "description": "",
            "capability_scope": scope,
            "trigger_intents": [],
            "user_utterance_examples": [],
            "goal": [],
            "required_info": [],
            "response_rules": [],
            "nodes": nodes,
            "edges": edges or [],
            "start_node_id": start or node_ids[0],
            "terminal_node_ids": terminals or [node_ids[-1]],
            "interruption_policy": {},
        },
    )


def test_sop_specific_is_not_a_routing_candidate() -> None:
    general = _skill("general", nodes=[{"node_id": "done", "name": "Done"}])
    nested_only = _skill(
        "nested_only",
        nodes=[{"node_id": "done", "name": "Done"}],
        scope="sop_specific",
    )

    assert [row.skill_id for row in discoverable_sops([general, nested_only])] == ["general"]


def test_nested_sop_is_expanded_and_parent_edges_are_rewired() -> None:
    child = _skill(
        "child",
        nodes=[
            {"node_id": "collect", "name": "Collect", "expected_user_info": ["email"]},
            {"node_id": "reply", "name": "Reply", "type": "response"},
        ],
        edges=[{"source_node_id": "collect", "next_node_id": "reply", "priority": 0}],
        start="collect",
        terminals=["reply"],
        scope="sop_specific",
    )
    parent = _skill(
        "parent",
        nodes=[
            {"node_id": "start", "name": "Start"},
            {
                "node_id": "nested",
                "name": "Nested",
                "type": "subflow",
                "sub_sop_id": "child",
                "instruction": "This legacy placeholder work must not execute.",
            },
            {"node_id": "done", "name": "Done", "type": "response"},
        ],
        edges=[
            {"source_node_id": "start", "next_node_id": "nested", "priority": 0},
            {"source_node_id": "nested", "next_node_id": "done", "priority": 0},
        ],
        start="start",
        terminals=["done"],
    )

    expanded = expand_sop_for_execution(parent, [parent, child])
    content = expanded.content_json
    node_ids = {node["node_id"] for node in content["nodes"]}
    child_start = "nested::child::collect"
    child_terminal = "nested::child::reply"

    assert "nested" not in node_ids
    assert {"start", child_start, child_terminal, "done"} <= node_ids
    assert {
        (edge["source_node_id"], edge["next_node_id"])
        for edge in content["edges"]
    } >= {
        ("start", child_start),
        (child_start, child_terminal),
        (child_terminal, "done"),
    }
    nested_start = next(node for node in content["nodes"] if node["node_id"] == child_start)
    assert nested_start["metadata"]["source_sop_id"] == "child"
    assert nested_start["metadata"]["parent_sop_node_id"] == "nested"
    assert nested_start["metadata"]["slot_scope"] == "parent_task_frame"
    assert nested_start["expected_user_info"] == ["email"]
    assert "legacy placeholder" not in nested_start.get("instruction", "")


def test_nested_sop_preserves_deepest_source_metadata() -> None:
    leaf = _skill("leaf", nodes=[{"node_id": "leaf_done", "name": "Leaf"}])
    child = _skill(
        "child",
        nodes=[
            {
                "node_id": "leaf_flow",
                "name": "Leaf flow",
                "type": "subflow",
                "sub_sop_id": "leaf",
            }
        ],
    )
    parent = _skill(
        "parent",
        nodes=[
            {
                "node_id": "child_flow",
                "name": "Child flow",
                "type": "subflow",
                "sub_sop_id": "child",
            }
        ],
    )

    expanded = expand_sop_for_execution(parent, [parent, child, leaf])
    leaf_node = expanded.content_json["nodes"][0]

    assert leaf_node["metadata"]["source_sop_id"] == "leaf"
    assert leaf_node["metadata"]["source_node_id"] == "leaf_done"
    assert leaf_node["metadata"]["nested_sop_path"] == ["parent", "child", "leaf"]


def test_nested_sop_cycle_is_rejected() -> None:
    first = _skill(
        "first",
        nodes=[
            {
                "node_id": "second_flow",
                "name": "Second",
                "type": "subflow",
                "sub_sop_id": "second",
            }
        ],
    )
    second = _skill(
        "second",
        nodes=[
            {
                "node_id": "first_flow",
                "name": "First",
                "type": "subflow",
                "sub_sop_id": "first",
            }
        ],
    )

    with pytest.raises(SopNestingError, match="cycle"):
        validate_sop_nesting("first", first.content_json, [first, second])


def test_deep_nested_sop_cycle_is_rejected() -> None:
    first = _skill(
        "first",
        nodes=[{"node_id": "to_second", "name": "Second", "type": "subflow", "sub_sop_id": "second"}],
    )
    second = _skill(
        "second",
        nodes=[{"node_id": "to_third", "name": "Third", "type": "subflow", "sub_sop_id": "third"}],
    )
    third = _skill(
        "third",
        nodes=[{"node_id": "to_first", "name": "First", "type": "subflow", "sub_sop_id": "first"}],
    )

    with pytest.raises(SopNestingError, match="first -> second -> third -> first"):
        validate_sop_nesting("first", first.content_json, [first, second, third])


def test_missing_or_unpublished_nested_sop_is_rejected() -> None:
    parent = _skill(
        "parent",
        nodes=[
            {
                "node_id": "missing_flow",
                "name": "Missing",
                "type": "subflow",
                "sub_sop_id": "missing",
            }
        ],
    )

    with pytest.raises(SopNestingError, match="missing or unpublished"):
        validate_sop_nesting("parent", parent.content_json, [parent])


def test_broken_nested_sop_does_not_disable_unrelated_sops() -> None:
    broken = _skill(
        "broken",
        nodes=[
            {
                "node_id": "missing_flow",
                "name": "Missing",
                "type": "subflow",
                "sub_sop_id": "missing",
            }
        ],
    )
    healthy = _skill("healthy", nodes=[{"node_id": "done", "name": "Done"}])

    assert [row.skill_id for row in expand_visible_sops([broken, healthy])] == ["healthy"]
