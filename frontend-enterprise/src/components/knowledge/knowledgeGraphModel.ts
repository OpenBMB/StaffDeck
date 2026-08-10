import type { KnowledgeConceptRead } from '@/types';

export const KNOWLEDGE_GRAPH_NODE_LIMIT = 500;

export type KnowledgeGraphUnresolvedLink = {
  label: string;
  target: string;
  kind: 'external' | 'unresolved';
};

export type KnowledgeGraphNode = {
  id: string;
  concept: KnowledgeConceptRead;
  degree: number;
  inbound: string[];
  outbound: string[];
  unresolvedLinks: KnowledgeGraphUnresolvedLink[];
};

export type KnowledgeGraphEdge = {
  id: string;
  source: string;
  target: string;
};

export type KnowledgeGraphModel = {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  nodeById: Map<string, KnowledgeGraphNode>;
  stats: {
    nodeCount: number;
    edgeCount: number;
    isolatedCount: number;
    unresolvedCount: number;
  };
};

export type BuildKnowledgeGraphOptions = {
  includeArchived?: boolean;
  conceptIds?: ReadonlySet<string>;
};

const EXTERNAL_SCHEME = /^[a-z][a-z\d+.-]*:/i;
const DEFAULT_PREVIEW_CHARACTER_LIMIT = 1_200;

export function buildKnowledgeGraph(
  concepts: KnowledgeConceptRead[],
  options: BuildKnowledgeGraphOptions = {},
): KnowledgeGraphModel {
  const includedConcepts = concepts.filter((concept) => {
    if (!options.includeArchived && concept.status === 'archived') return false;
    return !options.conceptIds || options.conceptIds.has(concept.concept_id);
  });
  const conceptsById = new Map(includedConcepts.map((concept) => [concept.concept_id, concept]));
  const inbound = new Map<string, Set<string>>();
  const outbound = new Map<string, Set<string>>();
  const unresolved = new Map<string, KnowledgeGraphUnresolvedLink[]>();
  const edgeByKey = new Map<string, KnowledgeGraphEdge>();

  for (const concept of includedConcepts) {
    inbound.set(concept.concept_id, new Set());
    outbound.set(concept.concept_id, new Set());
    unresolved.set(concept.concept_id, []);
  }

  for (const concept of includedConcepts) {
    const citationKeys = new Set(
      (concept.citations || []).map((citation) => relationKey(citation.label, citation.target)),
    );
    for (const link of concept.links || []) {
      const label = stringField(link.label);
      const target = stringField(link.target);
      if (!target || citationKeys.has(relationKey(label, target))) continue;
      const resolved = resolveKnowledgeLink(concept.concept_id, target);
      if (resolved.kind === 'external') {
        unresolved.get(concept.concept_id)?.push({ label, target, kind: 'external' });
        continue;
      }
      if (!resolved.conceptId || !conceptsById.has(resolved.conceptId)) {
        unresolved.get(concept.concept_id)?.push({ label, target, kind: 'unresolved' });
        continue;
      }
      if (resolved.conceptId === concept.concept_id) continue;
      const key = `${concept.concept_id}\u0000${resolved.conceptId}`;
      if (edgeByKey.has(key)) continue;
      edgeByKey.set(key, {
        id: `edge:${concept.concept_id}->${resolved.conceptId}`,
        source: concept.concept_id,
        target: resolved.conceptId,
      });
      outbound.get(concept.concept_id)?.add(resolved.conceptId);
      inbound.get(resolved.conceptId)?.add(concept.concept_id);
    }
  }

  const nodes = includedConcepts.map((concept) => {
    const nodeInbound = [...(inbound.get(concept.concept_id) || [])];
    const nodeOutbound = [...(outbound.get(concept.concept_id) || [])];
    return {
      id: concept.concept_id,
      concept,
      degree: nodeInbound.length + nodeOutbound.length,
      inbound: nodeInbound,
      outbound: nodeOutbound,
      unresolvedLinks: unresolved.get(concept.concept_id) || [],
    };
  });
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edges = [...edgeByKey.values()];
  const unresolvedCount = nodes.reduce((total, node) => total + node.unresolvedLinks.length, 0);

  return {
    nodes,
    edges,
    nodeById,
    stats: {
      nodeCount: nodes.length,
      edgeCount: edges.length,
      isolatedCount: nodes.filter((node) => node.degree === 0).length,
      unresolvedCount,
    },
  };
}

export function knowledgeConceptMatches(concept: KnowledgeConceptRead, rawQuery: string): boolean {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) return true;
  const tags = Array.isArray(concept.frontmatter?.tags) ? concept.frontmatter.tags : [];
  return [concept.title, concept.concept_id, ...tags]
    .filter((value) => typeof value === 'string')
    .some((value) => String(value).toLocaleLowerCase().includes(query));
}

export function knowledgeConceptPreviewMarkdown(
  concept: KnowledgeConceptRead,
  characterLimit = DEFAULT_PREVIEW_CHARACTER_LIMIT,
): string {
  const description = normalizePreviewMarkdown(concept.description || '');
  const source = description || firstMeaningfulMarkdownSection(stripOkfFrontmatter(concept.content_md || ''));
  const withoutDuplicateTitle = removeDuplicateLeadingHeading(source, concept.title || '');
  return truncateMarkdownAtBlockBoundary(withoutDuplicateTitle, characterLimit);
}

export function highestDegreeActiveNode(model: KnowledgeGraphModel): KnowledgeGraphNode | undefined {
  return [...model.nodes]
    .filter((node) => node.concept.status !== 'archived')
    .sort((left, right) => right.degree - left.degree || left.id.localeCompare(right.id))[0];
}

export function resolveKnowledgeLink(
  sourceConceptId: string,
  rawTarget: string,
): { kind: 'internal'; conceptId: string } | { kind: 'external'; conceptId?: undefined } {
  const target = rawTarget.trim();
  if (!target || target.startsWith('//') || EXTERNAL_SCHEME.test(target)) {
    return { kind: 'external' };
  }
  let pathname = target.split('#', 1)[0]?.split('?', 1)[0] || '';
  try {
    pathname = decodeURIComponent(pathname);
  } catch {
    // Keep malformed URI text so it is reported as unresolved instead of crashing the graph.
  }
  if (!pathname) return { kind: 'internal', conceptId: sourceConceptId };
  const isAbsolute = pathname.startsWith('/');
  const sourceParts = sourceConceptId.split('/').filter(Boolean);
  const targetParts = pathname.split('/');
  const parts = isAbsolute ? [] : sourceParts.slice(0, -1);
  for (const part of targetParts) {
    if (!part || part === '.') continue;
    if (part === '..') {
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  const lastIndex = parts.length - 1;
  if (lastIndex >= 0) parts[lastIndex] = parts[lastIndex].replace(/\.md$/i, '');
  return { kind: 'internal', conceptId: parts.join('/') };
}

function stripOkfFrontmatter(markdown: string): string {
  return markdown
    .replace(/^\uFEFF?---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/, '')
    .trim();
}

function normalizePreviewMarkdown(markdown: string): string {
  const normalized = markdown
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+(#{1,6}\s+)/g, '\n\n$1')
    .replace(/[ \t]+([-*]\s+)/g, '\n$1')
    .replace(/[ \t]+(\d+[.)]\s+)/g, '\n$1')
    .trim();
  return repairFlattenedTables(normalized);
}

function repairFlattenedTables(markdown: string): string {
  let result = markdown;
  const separatorPattern = /\|\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)+\|/;
  let searchFrom = 0;
  while (searchFrom < result.length) {
    const tail = result.slice(searchFrom);
    const match = tail.match(separatorPattern);
    if (!match || match.index === undefined) break;
    const separatorStart = searchFrom + match.index;
    const separatorEnd = separatorStart + match[0].length;
    const columnCount = match[0].split('|').slice(1, -1).length;
    const pipePositions: number[] = [];
    for (let index = 0; index < separatorStart; index += 1) {
      if (result[index] === '|') pipePositions.push(index);
    }
    const headerStart = pipePositions[pipePositions.length - columnCount - 1];
    if (headerStart === undefined || headerStart < result.lastIndexOf('\n', separatorStart)) {
      searchFrom = separatorEnd;
      continue;
    }

    const afterSeparator = result.slice(separatorEnd);
    const blockBoundary = afterSeparator.search(/\n|\s+(?=#{1,6}\s+|[-*]\s+|\d+[.)]\s+)/);
    const tableTail = blockBoundary >= 0 ? afterSeparator.slice(0, blockBoundary) : afterSeparator;
    const remainder = blockBoundary >= 0 ? afterSeparator.slice(blockBoundary).trimStart() : '';
    const headerCells = tableCells(result.slice(headerStart, separatorStart));
    const rowCells = tableCells(tableTail);
    if (headerCells.length !== columnCount || rowCells.length < columnCount) {
      searchFrom = separatorEnd;
      continue;
    }

    const rows: string[] = [];
    for (let index = 0; index + columnCount <= rowCells.length; index += columnCount) {
      rows.push(markdownTableRow(rowCells.slice(index, index + columnCount)));
    }
    const rebuilt = [
      markdownTableRow(headerCells),
      markdownTableRow(Array.from({ length: columnCount }, () => '---')),
      ...rows,
    ].join('\n');
    const prefix = result.slice(0, headerStart).trimEnd();
    result = `${prefix}${prefix ? '\n\n' : ''}${rebuilt}${remainder ? `\n\n${remainder}` : ''}`;
    searchFrom = prefix.length + rebuilt.length + 2;
  }
  return result.trim();
}

function tableCells(value: string): string[] {
  return value.split('|').map((cell) => cell.trim()).filter(Boolean);
}

function markdownTableRow(cells: string[]): string {
  return `| ${cells.join(' | ')} |`;
}

function firstMeaningfulMarkdownSection(markdown: string): string {
  const lines = normalizePreviewMarkdown(markdown).split('\n');
  const firstLineIndex = lines.findIndex((line) => line.trim());
  if (firstLineIndex < 0) return '';

  const firstHeading = lines[firstLineIndex].trim().match(/^(#{1,6})\s+(.+)$/);
  let endIndex = lines.length;
  if (firstHeading) {
    const firstLevel = firstHeading[1].length;
    for (let index = firstLineIndex + 1; index < lines.length; index += 1) {
      const heading = lines[index].trim().match(/^(#{1,6})\s+/);
      if (heading && heading[1].length <= firstLevel) {
        endIndex = index;
        break;
      }
    }
  } else {
    const nextHeadingOffset = lines
      .slice(firstLineIndex + 1)
      .findIndex((line) => /^(#{1,6})\s+/.test(line.trim()));
    if (nextHeadingOffset >= 0) endIndex = firstLineIndex + 1 + nextHeadingOffset;
  }
  return lines.slice(firstLineIndex, endIndex).join('\n').trim();
}

function removeDuplicateLeadingHeading(markdown: string, title: string): string {
  const normalized = normalizePreviewMarkdown(markdown);
  const lines = normalized.split('\n');
  const firstLineIndex = lines.findIndex((line) => line.trim());
  if (firstLineIndex < 0 || !title.trim()) return normalized;

  const heading = lines[firstLineIndex].trim().match(/^(#{1,6})\s+(.+)$/);
  if (!heading) return normalized;
  const headingContent = heading[2].trim();
  const normalizedTitle = normalizeHeadingText(title);
  if (normalizeHeadingText(headingContent) === normalizedTitle) {
    lines.splice(firstLineIndex, 1);
    return lines.join('\n').trim();
  }

  const rawTitle = title.trim();
  if (headingContent.startsWith(rawTitle) && /^\s/.test(headingContent.slice(rawTitle.length, rawTitle.length + 1))) {
    lines[firstLineIndex] = headingContent.slice(rawTitle.length).trim();
    return lines.join('\n').trim();
  }
  return normalized;
}

function normalizeHeadingText(value: string): string {
  return value
    .replace(/[`*_~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLocaleLowerCase();
}

function truncateMarkdownAtBlockBoundary(markdown: string, characterLimit: number): string {
  const normalized = normalizePreviewMarkdown(markdown);
  if (!normalized || normalized.length <= characterLimit || characterLimit <= 0) return normalized;
  const blocks = markdownBlocks(normalized);
  const included: string[] = [];
  let length = 0;
  for (const block of blocks) {
    const nextLength = length + (included.length ? 2 : 0) + block.length;
    if (included.length && nextLength > characterLimit) break;
    included.push(block);
    length = nextLength;
    if (length >= characterLimit) break;
  }
  return included.join('\n\n').trim();
}

function markdownBlocks(markdown: string): string[] {
  const lines = markdown.split('\n');
  const blocks: string[] = [];
  let current: string[] = [];
  let inFence = false;
  const flush = () => {
    const block = current.join('\n').trim();
    if (block) blocks.push(block);
    current = [];
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) inFence = !inFence;
    if (!inFence && !line.trim()) {
      flush();
      continue;
    }
    current.push(line);
  }
  flush();
  return blocks;
}

function relationKey(label: unknown, target: unknown): string {
  return `${stringField(label)}\u0000${stringField(target)}`;
}

function stringField(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
