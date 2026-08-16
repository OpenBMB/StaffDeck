from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from app.db.models import Skill


MAX_NESTED_SOP_DEPTH = 8


class SopNestingError(ValueError):
    pass


def sop_capability_scope(skill: Skill | dict[str, Any]) -> str:
    content = skill.content_json if isinstance(skill, Skill) else skill
    return (
        "sop_specific"
        if str((content or {}).get("capability_scope") or "").replace("-", "_")
        == "sop_specific"
        else "general"
    )


def discoverable_sops(skills: Iterable[Skill]) -> list[Skill]:
    return [skill for skill in skills if sop_capability_scope(skill) == "general"]


def nested_sop_ids(content: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(node.get("sub_sop_id") or "").strip()
            for node in content.get("nodes", [])
            if isinstance(node, dict) and str(node.get("type") or "") == "subflow"
            and str(node.get("sub_sop_id") or "").strip()
        )
    )


def validate_sop_nesting(
    skill_id: str,
    content: dict[str, Any],
    available: Iterable[Skill],
) -> None:
    contents = {
        row.skill_id: deepcopy(row.content_json or {})
        for row in available
        if row.status in {"published", "active"} or row.skill_id == skill_id
    }
    contents[skill_id] = deepcopy(content)

    def visit(current_id: str, path: list[str]) -> None:
        if len(path) > MAX_NESTED_SOP_DEPTH:
            raise SopNestingError(
                f"SOP nesting exceeds {MAX_NESTED_SOP_DEPTH} levels: "
                + " -> ".join(path)
            )
        current = contents.get(current_id)
        if current is None:
            raise SopNestingError(f"Nested SOP is missing or unpublished: {current_id}")
        for child_id in nested_sop_ids(current):
            if child_id in path:
                raise SopNestingError(
                    "SOP nesting cycle detected: " + " -> ".join([*path, child_id])
                )
            visit(child_id, [*path, child_id])

    visit(skill_id, [skill_id])


def expand_sop_for_execution(skill: Skill, available: Iterable[Skill]) -> Skill:
    by_id = {row.skill_id: row for row in available if row.status == "published"}
    by_id.setdefault(skill.skill_id, skill)
    validate_sop_nesting(skill.skill_id, skill.content_json or {}, by_id.values())
    expanded = deepcopy(skill)
    expanded.content_json = _expand_content(
        skill.skill_id,
        deepcopy(skill.content_json or {}),
        by_id,
        path=[skill.skill_id],
    )
    return expanded


def expand_visible_sops(skills: Iterable[Skill]) -> list[Skill]:
    rows = list(skills)
    expanded: list[Skill] = []
    for row in rows:
        try:
            expanded.append(expand_sop_for_execution(row, rows))
        except SopNestingError:
            # An archived/missing child must make its parent unavailable, but
            # it must not prevent unrelated SOPs and ordinary chat from
            # running. Publish-time validation is responsible for surfacing
            # the concrete configuration error to an editor.
            continue
    return expanded


def _expand_content(
    skill_id: str,
    content: dict[str, Any],
    by_id: dict[str, Skill],
    *,
    path: list[str],
) -> dict[str, Any]:
    nodes = [deepcopy(node) for node in content.get("nodes", []) if isinstance(node, dict)]
    edges = [deepcopy(edge) for edge in content.get("edges", []) if isinstance(edge, dict)]
    start_node_id = str(content.get("start_node_id") or "")
    terminal_node_ids = [str(value) for value in content.get("terminal_node_ids", [])]
    required_info = list(content.get("required_info") or [])
    response_rules = list(content.get("response_rules") or [])

    for placeholder in list(nodes):
        if str(placeholder.get("type") or "") != "subflow":
            continue
        placeholder_id = str(placeholder.get("node_id") or "").strip()
        child_id = str(placeholder.get("sub_sop_id") or "").strip()
        child = by_id.get(child_id)
        if not placeholder_id or child is None:
            raise SopNestingError(f"Nested SOP is missing or unpublished: {child_id or placeholder_id}")
        child_content = _expand_content(
            child_id,
            deepcopy(child.content_json or {}),
            by_id,
            path=[*path, child_id],
        )
        prefix = f"{placeholder_id}::{child_id}::"
        child_nodes = child_content.get("nodes", [])
        child_edges = child_content.get("edges", [])
        id_map = {
            str(node.get("node_id") or ""): prefix + str(node.get("node_id") or "")
            for node in child_nodes
            if isinstance(node, dict) and node.get("node_id")
        }
        child_start = id_map.get(str(child_content.get("start_node_id") or ""))
        child_terminals = [
            id_map[node_id]
            for node_id in (str(value) for value in child_content.get("terminal_node_ids", []))
            if node_id in id_map
        ]
        if not child_start or not child_terminals:
            raise SopNestingError(f"Nested SOP graph is incomplete: {child_id}")

        namespaced_nodes: list[dict[str, Any]] = []
        for child_node in child_nodes:
            if not isinstance(child_node, dict):
                continue
            source_node_id = str(child_node.get("node_id") or "")
            if source_node_id not in id_map:
                continue
            next_node = deepcopy(child_node)
            next_node["node_id"] = id_map[source_node_id]
            metadata = dict(next_node.get("metadata") or {})
            metadata.setdefault("nested_sop_path", [*path, child_id])
            metadata.setdefault("source_sop_id", child_id)
            metadata.setdefault("source_node_id", source_node_id)
            metadata.setdefault("parent_sop_node_id", placeholder_id)
            next_node["metadata"] = metadata
            if source_node_id == str(child_content.get("start_node_id") or ""):
                parent_instruction = str(placeholder.get("instruction") or "").strip()
                if parent_instruction:
                    next_node["instruction"] = "\n\n".join(
                        value
                        for value in [parent_instruction, str(next_node.get("instruction") or "").strip()]
                        if value
                    )
            namespaced_nodes.append(next_node)

        namespaced_edges = [
            {
                **deepcopy(edge),
                "source_node_id": id_map[str(edge.get("source_node_id") or "")],
                "next_node_id": id_map[str(edge.get("next_node_id") or "")],
            }
            for edge in child_edges
            if isinstance(edge, dict)
            and str(edge.get("source_node_id") or "") in id_map
            and str(edge.get("next_node_id") or "") in id_map
        ]

        incoming = [edge for edge in edges if str(edge.get("next_node_id") or "") == placeholder_id]
        outgoing = [edge for edge in edges if str(edge.get("source_node_id") or "") == placeholder_id]
        edges = [
            edge
            for edge in edges
            if str(edge.get("source_node_id") or "") != placeholder_id
            and str(edge.get("next_node_id") or "") != placeholder_id
        ]
        edges.extend({**edge, "next_node_id": child_start} for edge in incoming)
        for terminal_id in child_terminals:
            edges.extend({**edge, "source_node_id": terminal_id} for edge in outgoing)
        edges.extend(namespaced_edges)
        nodes = [node for node in nodes if str(node.get("node_id") or "") != placeholder_id]
        nodes.extend(namespaced_nodes)
        if start_node_id == placeholder_id:
            start_node_id = child_start
        if placeholder_id in terminal_node_ids:
            terminal_node_ids = [
                node_id for node_id in terminal_node_ids if node_id != placeholder_id
            ] + child_terminals
        required_info.extend(child_content.get("required_info") or [])
        response_rules.extend(child_content.get("response_rules") or [])

    content["nodes"] = nodes
    content["edges"] = edges
    content["start_node_id"] = start_node_id
    content["terminal_node_ids"] = list(dict.fromkeys(terminal_node_ids))
    content["required_info"] = list(dict.fromkeys(str(value) for value in required_info))
    content["response_rules"] = list(dict.fromkeys(str(value) for value in response_rules))
    content["runtime_expanded"] = True
    return content
