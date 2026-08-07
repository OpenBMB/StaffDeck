import { describe, expect, it } from 'vitest';

import type { SkillCard } from '../types';
import {
  skillNodeFlowPosition,
  withSkillNodeFlowPosition,
  withoutSkillEdgeAt,
} from './skillFlowModel';

function fixture(): SkillCard {
  return {
    skill_id: 'demo',
    name: 'Demo',
    version: '1.0.0',
    description: '',
    trigger_intents: [],
    user_utterance_examples: [],
    goal: [],
    required_info: [],
    response_rules: [],
    nodes: [
      { node_id: 'node1', name: 'Node 1', metadata: { retained: true } },
      { node_id: 'node3', name: 'Node 3' },
      { node_id: 'node6', name: 'Node 6' },
    ],
    edges: [
      { source_node_id: 'node1', next_node_id: 'node3', priority: 1 },
      { source_node_id: 'node3', next_node_id: 'node6', priority: 1 },
    ],
    start_node_id: 'node1',
    terminal_node_ids: ['node6'],
    interruption_policy: {},
  };
}

describe('skill flow editor model', () => {
  it('persists visual node coordinates without changing graph semantics', () => {
    const original = fixture();
    const next = withSkillNodeFlowPosition(original, 0, { x: 421.6, y: 238.2 });

    expect(skillNodeFlowPosition(next.nodes[0])).toEqual({ x: 422, y: 238 });
    expect(next.nodes[0].metadata).toMatchObject({ retained: true });
    expect(next.edges).toEqual(original.edges);
    expect(original.nodes[0].metadata).toEqual({ retained: true });
  });

  it('removes only the selected transition', () => {
    const original = fixture();
    const next = withoutSkillEdgeAt(original, 0);

    expect(next.edges).toEqual([
      { source_node_id: 'node3', next_node_id: 'node6', priority: 1 },
    ]);
    expect(original.edges).toHaveLength(2);
  });
});
