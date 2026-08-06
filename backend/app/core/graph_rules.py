from __future__ import annotations

from collections.abc import Callable
from typing import Any


class GraphRules:
    """Pure Skill graph rules shared by Harness v2 execution and session projection."""

    @staticmethod
    def node_as_step(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "step_id": node.get("node_id"),
            "node_id": node.get("node_id"),
            "type": node.get("type"),
            "name": node.get("name"),
            "instruction": node.get("instruction"),
            "optional": node.get("optional", False),
            "condition": node.get("condition"),
            "expected_user_info": node.get("expected_user_info") or [],
            "allowed_actions": node.get("allowed_actions") or [],
            "knowledge_scope": node.get("knowledge_scope") or {},
            "retry_policy": node.get("retry_policy") or {},
            "metadata": node.get("metadata") or {},
        }

    @staticmethod
    def nodes(content: dict[str, Any]) -> list[dict[str, Any]]:
        return [node for node in content.get("nodes", []) if isinstance(node, dict)]

    @classmethod
    def steps(cls, content: dict[str, Any]) -> list[dict[str, Any]]:
        return cls.steps_from_nodes(cls.ordered_nodes(content))

    @classmethod
    def steps_from_nodes(cls, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [cls.node_as_step(node) for node in nodes]

    @classmethod
    def ordered_nodes(
        cls,
        content: dict[str, Any],
        *,
        nodes: list[dict[str, Any]] | None = None,
        outgoing: dict[str, list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        nodes = cls.nodes(content) if nodes is None else nodes
        if not nodes:
            return []
        nodes_by_id = {
            str(node.get("node_id") or ""): node for node in nodes if node.get("node_id")
        }
        start_node_id = str(content.get("start_node_id") or "").strip()
        if not start_node_id or start_node_id not in nodes_by_id:
            start_node_id = str(nodes[0].get("node_id") or "")
        outgoing = cls.outgoing_edges(content) if outgoing is None else outgoing
        ordered: list[dict[str, Any]] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if not node_id or node_id in visited:
                return
            node = nodes_by_id.get(node_id)
            if not node:
                return
            visited.add(node_id)
            ordered.append(node)
            for edge in outgoing.get(node_id, []):
                visit(str(edge.get("next_node_id") or ""))

        visit(start_node_id)
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            if node_id not in visited:
                ordered.append(node)
        return ordered

    @staticmethod
    def outgoing_edges(content: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        edges = [edge for edge in content.get("edges", []) if isinstance(edge, dict)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            source = str(edge.get("source_node_id") or "")
            target = str(edge.get("next_node_id") or "")
            if not source or not target:
                continue
            grouped.setdefault(source, []).append(edge)
        for source, items in grouped.items():
            grouped[source] = sorted(items, key=lambda item: int(item.get("priority") or 0))
        return grouped

    @classmethod
    def next_steps(
        cls, content: dict[str, Any], active_step_id: str | None
    ) -> list[dict[str, Any]]:
        if not active_step_id:
            return []
        return cls.next_steps_from_parts(
            cls.nodes(content), cls.outgoing_edges(content).get(active_step_id, [])
        )

    @classmethod
    def next_steps_from_parts(
        cls,
        nodes: list[dict[str, Any]],
        outgoing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        nodes_by_id = {str(node.get("node_id") or ""): cls.node_as_step(node) for node in nodes}
        return [
            nodes_by_id[target_id]
            for target_id in (str(edge.get("next_node_id") or "") for edge in outgoing)
            if target_id in nodes_by_id
        ]

    @classmethod
    def default_next_step(
        cls, content: dict[str, Any], active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return cls.default_next_step_from_parts(
            cls.nodes(content), cls.outgoing_edges(content).get(active_step_id, [])
        )

    @classmethod
    def default_next_step_from_parts(
        cls,
        nodes: list[dict[str, Any]],
        outgoing: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        nodes_by_id = {str(node.get("node_id") or ""): cls.node_as_step(node) for node in nodes}
        if not outgoing:
            return None
        if len(outgoing) == 1:
            return nodes_by_id.get(str(outgoing[0].get("next_node_id") or ""))
        unconditional = []
        for edge in outgoing:
            if cls.edge_condition(edge) not in {"", "default", "else"}:
                continue
            target = nodes_by_id.get(str(edge.get("next_node_id") or ""))
            if target:
                unconditional.append(target)
        return unconditional[0] if len(unconditional) == 1 else None

    @classmethod
    def current_step(
        cls, content: dict[str, Any], active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return cls.current_step_from_steps(cls.steps(content), active_step_id)

    @staticmethod
    def current_step_from_steps(
        steps: list[dict[str, Any]], active_step_id: str
    ) -> dict[str, Any] | None:
        for step in steps:
            step_ids = {
                str(step.get("step_id") or ""),
                str(step.get("node_id") or ""),
                str(step.get("id") or ""),
            }
            if active_step_id in step_ids:
                return step
        return None

    @classmethod
    def has_step(cls, content: dict[str, Any], step_id: str | None) -> bool:
        if not step_id:
            return False
        return any(node.get("node_id") == step_id for node in cls.nodes(content))

    @staticmethod
    def normalize_pending_steps(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        pending: list[str] = []
        for item in value:
            step_id = str(item or "").strip()
            if step_id and step_id not in pending:
                pending.append(step_id)
        return pending

    @classmethod
    def sibling_steps(
        cls,
        content: dict[str, Any],
        source_step_id: str | None,
        selected_step_id: str,
    ) -> list[str]:
        if not source_step_id:
            return []
        outgoing = cls.outgoing_edges(content).get(source_step_id) or []
        return cls.sibling_steps_from_edges(outgoing, selected_step_id, cls.edge_condition)

    @staticmethod
    def sibling_steps_from_edges(
        outgoing: list[dict[str, Any]],
        selected_step_id: str,
        edge_condition: Callable[[dict[str, Any]], str],
    ) -> list[str]:
        selected_conditions = {
            edge_condition(edge)
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip() == selected_step_id
        }
        return [
            str(edge.get("next_node_id") or "").strip()
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip()
            and str(edge.get("next_node_id") or "").strip() != selected_step_id
            and edge_condition(edge) in selected_conditions
        ]

    @staticmethod
    def edge_condition(edge: dict[str, Any]) -> str:
        return str(edge.get("condition") or "").strip().lower()

    @classmethod
    def step_actions(cls, step: dict[str, Any]) -> list[str]:
        return [
            action
            for action in (cls.normalize_action(item) for item in step.get("allowed_actions", []))
            if action
        ]

    @staticmethod
    def normalize_action(action: object) -> str:
        text = str(action or "").strip().strip("`'\"").strip()
        if not text:
            return ""
        if text.startswith("call_tool:"):
            tool_name = text.split(":", 1)[1].strip().strip("`'\"").strip()
            return f"call_tool:{tool_name}" if tool_name else ""
        return text

    @classmethod
    def actions_allow_final_reply(cls, actions: list[str]) -> bool:
        return "answer_user" in [cls.normalize_action(action) for action in actions]

    @staticmethod
    def slot_has_value(slots: dict[str, Any], field: str) -> bool:
        value = slots.get(field)
        return value is not None and value != ""

    @classmethod
    def slot_satisfied(cls, slots: dict[str, Any], field: str) -> bool:
        normalized = field.strip()
        return not normalized or cls.slot_has_value(slots, normalized)

    @classmethod
    def terminal_position(
        cls,
        content: dict[str, Any],
        active_step_id: str | None,
        slots: dict[str, Any],
    ) -> bool:
        if not active_step_id:
            return False
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        current_step = cls.current_step(content, active_step_id)
        return cls.terminal_position_from_step(
            content,
            active_step_id,
            slots,
            current_step,
            cls.slot_satisfied,
            cls.step_actions,
        )

    @staticmethod
    def terminal_position_from_step(
        content: dict[str, Any],
        active_step_id: str,
        slots: dict[str, Any],
        current_step: dict[str, Any] | None,
        slot_satisfied: Callable[[dict[str, Any], str], bool],
        step_actions: Callable[[dict[str, Any]], list[str]],
    ) -> bool:
        if not current_step:
            return False
        expected = [str(field) for field in current_step.get("expected_user_info", [])]
        if any(not slot_satisfied(slots, field) for field in expected):
            return False
        required = [str(field) for field in content.get("required_info", [])]
        if any(not slot_satisfied(slots, field) for field in required):
            return False
        actions = step_actions(current_step)
        if not actions:
            return True
        terminal_actions = {
            "answer_user",
            "handoff_human",
            "continue_flow",
            "ask_user",
            "ask_clarification",
        }
        return all(
            action in terminal_actions or action.startswith("call_tool:") for action in actions
        )
