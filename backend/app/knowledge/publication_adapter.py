from __future__ import annotations
from typing import Any
from pydantic import ValidationError
from sqlmodel import select
from app.db.models import Skill, Tool
from app.capability_scope import normalize_capability_scope
from app.skills.skill_schema import SkillCard, SkillGraphNode, SkillGraphEdge


class KnowledgeDiscoveryValidationError(ValueError):
    """Raised when a model-produced discovery cannot safely enter the review queue."""

class KnowledgeDiscoveryConflictError(ValueError):
    """Raised when a discovery cannot transition from pending to confirmed."""

def validate_discovered_skill(payload: dict[str, Any]) -> SkillCard:
    unknown_skill_fields = sorted(set(payload) - set(SkillCard.model_fields))
    if unknown_skill_fields:
        raise KnowledgeDiscoveryValidationError(f"技能草稿包含未知字段：{', '.join(unknown_skill_fields)}。")
    for index, node in enumerate(payload.get('nodes', [])):
        if not isinstance(node, dict):
            continue
        unknown_node_fields = sorted(set(node) - set(SkillGraphNode.model_fields))
        if unknown_node_fields:
            raise KnowledgeDiscoveryValidationError(f"技能草稿节点 nodes.{index} 包含未知字段：{', '.join(unknown_node_fields)}。")
    for index, edge in enumerate(payload.get('edges', [])):
        if not isinstance(edge, dict):
            continue
        unknown_edge_fields = sorted(set(edge) - set(SkillGraphEdge.model_fields))
        if unknown_edge_fields:
            raise KnowledgeDiscoveryValidationError(f"技能草稿连线 edges.{index} 包含未知字段：{', '.join(unknown_edge_fields)}。")
    try:
        card = SkillCard.model_validate(payload)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_url=False)[:5]:
            field = '.'.join((str(item) for item in error.get('loc', ()))) or 'skill'
            details.append(f"{field}: {error.get('msg', '字段不合法')}")
        suffix = f"：{'；'.join(details)}" if details else '。'
        raise KnowledgeDiscoveryValidationError(f'技能草稿不符合 StaffDeck SkillCard 格式{suffix}') from exc
    if not card.skill_id.strip() or not card.name.strip():
        raise KnowledgeDiscoveryValidationError('技能草稿的 skill_id 和 name 不能为空。')
    incomplete_nodes = [node.node_id or '<empty>' for node in card.nodes if not node.node_id.strip() or not node.name.strip() or (not node.instruction.strip())]
    if incomplete_nodes:
        raise KnowledgeDiscoveryValidationError(f"技能草稿节点缺少 node_id、name 或 instruction：{', '.join(incomplete_nodes)}。")
    node_ids = {node.node_id for node in card.nodes}
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    reverse: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in card.edges:
        outgoing[edge.source_node_id].add(edge.next_node_id)
        reverse[edge.next_node_id].add(edge.source_node_id)
    reachable: set[str] = set()
    pending = [card.start_node_id]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(outgoing[node_id] - reachable)
    unreachable = sorted(node_ids - reachable)
    if unreachable:
        raise KnowledgeDiscoveryValidationError(f"技能草稿包含无法从开始节点到达的节点：{', '.join(unreachable)}。")
    reaches_terminal: set[str] = set()
    pending = list(card.terminal_node_ids)
    while pending:
        node_id = pending.pop()
        if node_id in reaches_terminal:
            continue
        reaches_terminal.add(node_id)
        pending.extend(reverse[node_id] - reaches_terminal)
    dead_ends = sorted(node_ids - reaches_terminal)
    if dead_ends:
        raise KnowledgeDiscoveryValidationError(f"技能草稿包含无法到达结束节点的节点：{', '.join(dead_ends)}。")
    return card

def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

class StaffDeckSuggestionPublisher:

    def __init__(self, db):
        self.db = db

    def _confirm_tool(self, suggestion: Any, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get('name') or payload.get('tool_name') or '').strip()
        if not name:
            raise KnowledgeDiscoveryValidationError('工具建议缺少 name。')
        url = str(payload.get('url') or '').strip()
        if not url:
            raise KnowledgeDiscoveryValidationError('工具建议缺少 url。')
        existing = self.db.exec(select(Tool).where(Tool.tenant_id == suggestion.tenant_id, Tool.name == name)).first()
        if existing:
            raise KnowledgeDiscoveryConflictError(f'工具名称 {name} 已存在，请修改建议后重试。')
        row = Tool(tenant_id=suggestion.tenant_id, name=name, display_name=_optional_str(payload.get('display_name')) or suggestion.title, description=_optional_str(payload.get('description') or suggestion.reason), bucket=str(payload.get('bucket') or '知识自发现工具').strip() or '知识自发现工具', method=str(payload.get('method') or 'POST').upper(), url=url, headers_json=payload.get('headers') if isinstance(payload.get('headers'), dict) else {}, auth_json=payload.get('auth') if isinstance(payload.get('auth'), dict) else {}, input_schema=payload.get('input_schema') if isinstance(payload.get('input_schema'), dict) else {}, output_schema=payload.get('output_schema') if isinstance(payload.get('output_schema'), dict) else {}, allowed_skills_json=payload.get('allowed_skills') if isinstance(payload.get('allowed_skills'), list) else [], capability_scope=normalize_capability_scope(payload.get('capability_scope')), enabled=True)
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return {'status': 'created', 'tool_id': row.id}

    def _confirm_skill(self, suggestion: Any, payload: dict[str, Any]) -> dict[str, Any]:
        skill_payload = payload.get('draft_skill') if isinstance(payload.get('draft_skill'), dict) else payload
        card = validate_discovered_skill(skill_payload)
        existing = self.db.exec(select(Skill).where(Skill.tenant_id == suggestion.tenant_id, Skill.skill_id == card.skill_id)).first()
        if existing:
            raise KnowledgeDiscoveryConflictError(f'技能 ID {card.skill_id} 已存在，不能通过知识发现覆盖现有技能。')
        row = Skill(tenant_id=suggestion.tenant_id, skill_id=card.skill_id, version=card.version, name=card.name, business_domain=card.business_domain, description=card.description, content_json=card.model_dump(mode='json'), status='draft')
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return {'status': 'created', 'skill_id': row.id}
