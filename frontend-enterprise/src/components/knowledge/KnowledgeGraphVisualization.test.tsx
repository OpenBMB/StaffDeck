// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { KnowledgeConceptRead } from '@/types';
import { KnowledgeGraphVisualization } from './KnowledgeGraphVisualization';

const cytoscapeMock = vi.hoisted(() => vi.fn());

vi.mock('cytoscape', () => ({ default: cytoscapeMock }));

function emptyCollection() {
  const collection = {
    length: 0,
    addClass: vi.fn(),
    removeClass: vi.fn(),
    difference: vi.fn(),
    edges: vi.fn(),
    forEach: vi.fn(),
    filter: vi.fn(),
  };
  collection.addClass.mockReturnValue(collection);
  collection.removeClass.mockReturnValue(collection);
  collection.difference.mockReturnValue(collection);
  collection.edges.mockReturnValue(collection);
  collection.filter.mockReturnValue(collection);
  return collection;
}

function createCore() {
  const collection = emptyCollection();
  return {
    collection: () => collection,
    destroy: vi.fn(),
    edges: () => collection,
    elements: () => collection,
    fit: vi.fn(),
    getElementById: () => collection,
    layout: () => ({ run: vi.fn() }),
    nodes: () => collection,
    on: vi.fn(),
    zoom: () => 1,
  };
}

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
    description: 'A knowledge concept',
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

beforeEach(() => {
  cytoscapeMock.mockReset().mockImplementation(createCore);
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

afterEach(cleanup);

describe('KnowledgeGraphVisualization', () => {
  it('shows active concepts by default and reveals archived concepts on request', async () => {
    render(
      <KnowledgeGraphVisualization
        concepts={[
          concept('topics/active', { title: 'Active Topic' }),
          concept('topics/archived', { title: 'Archived Topic', status: 'archived' }),
        ]}
        knowledgeBaseKey="kb-1"
        onViewConcept={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText('知识图谱统计').textContent).toContain('1'));
    expect(screen.queryByText('Archived Topic')).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: '显示已归档' }));
    await waitFor(() => expect(screen.getByLabelText('知识图谱统计').textContent).toContain('2'));
  });

  it('opens the existing full concept viewer from the selected node detail', async () => {
    const onViewConcept = vi.fn();
    const selected = concept('topics/hub', { title: 'Knowledge Hub' });
    render(
      <KnowledgeGraphVisualization
        concepts={[selected]}
        knowledgeBaseKey="kb-1"
        onViewConcept={onViewConcept}
      />,
    );

    const button = await screen.findByRole('button', { name: '查看完整内容' });
    await userEvent.click(button);
    expect(onViewConcept).toHaveBeenCalledWith(selected);
  });

  it('renders a safe Markdown summary and navigates resolvable graph links', async () => {
    render(
      <KnowledgeGraphVisualization
        concepts={[
          concept('topics/a', {
            title: 'Liability Cases',
            description: [
              '# Liability Cases',
              '',
              '**Case L-2022-008**',
              '',
              '[Read related terms](/topics/b.md)',
              '',
              '[External reference](https://example.com/reference)',
            ].join('\n'),
            links: [{ label: 'Read related terms', target: '/topics/b.md' }],
          }),
          concept('topics/b', { title: 'Related Terms', description: 'Term details' }),
        ]}
        knowledgeBaseKey="kb-markdown"
        onViewConcept={vi.fn()}
      />,
    );

    const formatted = await screen.findByText('Case L-2022-008');
    expect(formatted.tagName).toBe('STRONG');
    expect(screen.getAllByText('Liability Cases')).toHaveLength(1);
    const externalLink = screen.getByRole('link', { name: 'External reference' });
    expect(externalLink.getAttribute('target')).toBe('_blank');
    expect(externalLink.getAttribute('rel')).toBe('noreferrer');

    await userEvent.click(screen.getByRole('button', { name: 'Read related terms' }));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Related Terms' })).toBeTruthy());
    expect(screen.getByText('Term details')).toBeTruthy();
  });

  it('requires an explicit filtered scope before drawing more than 500 nodes', async () => {
    const concepts = Array.from({ length: 501 }, (_, index) => concept(`topics/node-${index}`, {
      title: index === 317 ? 'only-match' : `Topic ${index}`,
    }));
    render(
      <KnowledgeGraphVisualization
        concepts={concepts}
        knowledgeBaseKey="kb-large"
        onViewConcept={vi.fn()}
      />,
    );

    const gate = document.querySelector('.kgv-scope-gate');
    expect(gate?.textContent).toContain(String(concepts.length));
    expect(cytoscapeMock).not.toHaveBeenCalled();
    await userEvent.type(screen.getByRole('searchbox', { name: '搜索知识图谱' }), 'only-match');
    const drawButton = gate?.querySelector('button');
    expect(drawButton).toBeTruthy();
    await userEvent.click(drawButton as HTMLButtonElement);
    await waitFor(() => expect(cytoscapeMock).toHaveBeenCalledOnce());
    expect(screen.getByText(/当前绘制筛选后的 1 个节点/)).toBeTruthy();
  });
});
