import type { SkillCard, SkillGraphNode } from '../types';

export type SkillFlowPosition = {
  x: number;
  y: number;
};

const FLOW_POSITION_KEY = 'flow_position';

export function skillNodeFlowPosition(node: Record<string, unknown>): SkillFlowPosition | null {
  const metadata = isRecord(node.metadata) ? node.metadata : {};
  const rawPosition = metadata[FLOW_POSITION_KEY];
  if (!isRecord(rawPosition)) return null;
  const x = Number(rawPosition.x);
  const y = Number(rawPosition.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

export function withSkillNodeFlowPosition(
  skill: SkillCard,
  nodeIndex: number,
  position: SkillFlowPosition,
): SkillCard {
  if (nodeIndex < 0 || nodeIndex >= skill.nodes.length) return skill;
  const next = cloneSkill(skill);
  const node = { ...(next.nodes[nodeIndex] || {}) } as SkillGraphNode;
  const metadata = isRecord(node.metadata) ? { ...node.metadata } : {};
  metadata[FLOW_POSITION_KEY] = {
    x: Math.round(position.x),
    y: Math.round(position.y),
  };
  node.metadata = metadata;
  next.nodes[nodeIndex] = node;
  return next;
}

export function withoutSkillEdgeAt(skill: SkillCard, edgeIndex: number): SkillCard {
  if (edgeIndex < 0 || edgeIndex >= skill.edges.length) return skill;
  const next = cloneSkill(skill);
  next.edges.splice(edgeIndex, 1);
  return next;
}

function cloneSkill(skill: SkillCard): SkillCard {
  return JSON.parse(JSON.stringify(skill)) as SkillCard;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
