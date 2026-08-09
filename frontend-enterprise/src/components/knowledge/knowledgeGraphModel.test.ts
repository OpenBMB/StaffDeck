import { describe, expect, it } from 'vitest';

import type { KnowledgeConceptRead } from '@/types';
import {
  buildKnowledgeGraph,
  highestDegreeActiveNode,
  knowledgeConceptPreviewMarkdown,
  knowledgeConceptMatches,
  resolveKnowledgeLink,
} from './knowledgeGraphModel';

function concept(
  conceptId: string,
  overrides: Partial<KnowledgeConceptRead> = {},
): KnowledgeConceptRead {
  return {
    id: `row-${conceptId}`,
    tenant_id: 'tenant-default',
    knowledge_base_id: 'kb-1',
    concept_id: conceptId,
    concept_type: 'Topic',
    title: conceptId,
    content_md: '',
    frontmatter: {},
    links: [],
    citations: [],
    source_refs: [],
    status: 'active',
    created_at: '2026-08-09T00:00:00Z',
    updated_at: '2026-08-09T00:00:00Z',
    ...overrides,
  };
}

describe('knowledgeGraphModel', () => {
  it('resolves absolute, relative, parent, query, and fragment links', () => {
    expect(resolveKnowledgeLink('sources/manual/intro', '/topics/rules.md')).toEqual({
      kind: 'internal',
      conceptId: 'topics/rules',
    });
    expect(resolveKnowledgeLink('sources/manual/intro', './details.md#steps')).toEqual({
      kind: 'internal',
      conceptId: 'sources/manual/details',
    });
    expect(resolveKnowledgeLink('sources/manual/intro', '../index.md?mode=full')).toEqual({
      kind: 'internal',
      conceptId: 'sources/index',
    });
    expect(resolveKnowledgeLink('sources/manual/intro', 'https://example.com/rules')).toEqual({
      kind: 'external',
    });
  });

  it('builds directed internal edges while excluding citations, duplicates, and self-links', () => {
    const model = buildKnowledgeGraph([
      concept('topics/alpha', {
        links: [
          { label: 'Beta', target: '/topics/beta.md' },
          { label: 'Beta', target: '/topics/beta.md' },
          { label: 'Self', target: '/topics/alpha.md' },
          { label: 'Source', target: 'ultrarag://chunk/1' },
          { label: 'Citation', target: '/topics/cited.md' },
          { label: 'Missing', target: '/topics/missing.md' },
        ],
        citations: [{ label: 'Citation', target: '/topics/cited.md' }],
      }),
      concept('topics/beta'),
      concept('topics/cited'),
    ]);

    expect(model.edges).toEqual([
      {
        id: 'edge:topics/alpha->topics/beta',
        source: 'topics/alpha',
        target: 'topics/beta',
      },
    ]);
    expect(model.nodeById.get('topics/alpha')).toMatchObject({
      degree: 1,
      outbound: ['topics/beta'],
      unresolvedLinks: [
        { label: 'Source', target: 'ultrarag://chunk/1', kind: 'external' },
        { label: 'Missing', target: '/topics/missing.md', kind: 'unresolved' },
      ],
    });
    expect(model.nodeById.get('topics/beta')).toMatchObject({ degree: 1, inbound: ['topics/alpha'] });
    expect(model.stats).toEqual({
      nodeCount: 3,
      edgeCount: 1,
      isolatedCount: 1,
      unresolvedCount: 2,
    });
  });

  it('hides archived concepts by default and honors an explicit concept subset', () => {
    const concepts = [
      concept('topics/active', { links: [{ label: 'Old', target: '/topics/old.md' }] }),
      concept('topics/old', { status: 'archived' }),
      concept('topics/other'),
    ];

    expect(buildKnowledgeGraph(concepts).nodes.map((node) => node.id)).toEqual([
      'topics/active',
      'topics/other',
    ]);
    const model = buildKnowledgeGraph(concepts, {
      includeArchived: true,
      conceptIds: new Set(['topics/active', 'topics/old']),
    });
    expect(model.stats).toMatchObject({ nodeCount: 2, edgeCount: 1, isolatedCount: 0 });
  });

  it('searches titles, ids, and tags and selects the strongest active hub', () => {
    const alpha = concept('topics/alpha', {
      title: 'Meeting Room Rules',
      description: 'Administration Service',
      frontmatter: { tags: ['office', 'policy'] },
      links: [
        { label: 'Beta', target: '/topics/beta.md' },
        { label: 'Gamma', target: '/topics/gamma.md' },
      ],
    });
    const model = buildKnowledgeGraph([alpha, concept('topics/beta'), concept('topics/gamma')]);

    expect(knowledgeConceptMatches(alpha, 'OFFICE')).toBe(true);
    expect(knowledgeConceptMatches(alpha, 'Admin')).toBe(false);
    expect(knowledgeConceptMatches(alpha, 'missing')).toBe(false);
    expect(highestDegreeActiveNode(model)?.id).toBe('topics/alpha');
  });

  it('prefers the Markdown description and removes a duplicate leading title', () => {
    const preview = knowledgeConceptPreviewMarkdown(concept('topics/liability', {
      title: 'Liability Cases',
      description: '# Liability Cases\n\n**Case L-2022-008** with a penalty clause',
      content_md: '# This body should not be used',
    }));

    expect(preview).toBe('**Case L-2022-008** with a penalty clause');
  });

  it('falls back to the first meaningful body section without OKF frontmatter', () => {
    const preview = knowledgeConceptPreviewMarkdown(concept('topics/handbook', {
      title: 'Employee Handbook',
      content_md: [
        '---',
        'title: Employee Handbook',
        'tags: [policy]',
        '---',
        '# Employee Handbook',
        '',
        'Welcome to the handbook.',
        '',
        '# Second Chapter',
        '',
        'This should not enter the first section.',
      ].join('\n'),
    }));

    expect(preview).toBe('Welcome to the handbook.');
  });

  it('recovers content that follows a duplicate title on the same heading line', () => {
    const preview = knowledgeConceptPreviewMarkdown(concept('topics/liability', {
      title: 'Liability Cases',
      description: '# Liability Cases **Case L-2022-008** with a penalty clause',
    }));

    expect(preview).toBe('**Case L-2022-008** with a penalty clause');
  });

  it('repairs flattened Markdown tables and lists from legacy summaries', () => {
    const preview = knowledgeConceptPreviewMarkdown(concept('topics/access', {
      title: 'Access Levels',
      description: '# Access Levels | Level | Scope | |-|-| | L1 | Office | | L2 | Production | - **Expiry**: 90 days',
    }));

    expect(preview).toBe([
      '| Level | Scope |',
      '| --- | --- |',
      '| L1 | Office |',
      '| L2 | Production |',
      '',
      '- **Expiry**: 90 days',
    ].join('\n'));
  });

  it('soft-limits long previews at complete Markdown block boundaries', () => {
    const firstBlock = `First block ${'a'.repeat(700)}`;
    const secondBlock = `Second block ${'b'.repeat(700)}`;
    const preview = knowledgeConceptPreviewMarkdown(concept('topics/long', {
      description: `${firstBlock}\n\n${secondBlock}`,
    }), 1_200);

    expect(preview).toBe(firstBlock);
    expect(preview).not.toContain('Second block');
  });
});
