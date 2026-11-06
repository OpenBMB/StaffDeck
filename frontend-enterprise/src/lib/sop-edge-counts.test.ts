import { describe, expect, it } from 'vitest';
import { countEdgeLabels } from './sop-edge-counts';

describe('SOP edge label counts', () => {
  it('counts repeated labels and preserves reserved names as ordinary keys', () => {
    const result = countEdgeLabels([
      { source_node_id: '__proto__', label: 'constructor' },
      { source_node_id: '__proto__', label: 'constructor' },
      { source_node_id: 'source', label: '__proto__' },
      { source_node_id: '', label: 'ignored' },
    ], (edge) => edge.label);
    expect(result['__proto__']['constructor']).toBe(2);
    expect(result.source['__proto__']).toBe(1);
    expect(Object.prototype).not.toHaveProperty('source');
    expect(Object.keys(result)).toEqual(['__proto__', 'source']);
  });
});
