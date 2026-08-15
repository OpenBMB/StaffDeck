import cytoscape, { type Core, type EdgeSingular, type ElementDefinition, type EventObject, type LayoutOptions, type NodeSingular } from 'cytoscape';
import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  ChevronUp,
  Expand,
  Focus,
  Grid2X2,
  Maximize2,
  Network,
  RotateCcw,
  Search,
} from 'lucide-react';
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { renderMarkdownBlocks } from '@/pages/chat/chatHelpers';
import type { KnowledgeConceptRead } from '@/types';
import {
  buildKnowledgeGraph,
  highestDegreeActiveNode,
  knowledgeConceptPreviewMarkdown,
  knowledgeConceptMatches,
  KNOWLEDGE_GRAPH_NODE_LIMIT,
  resolveKnowledgeLink,
  type KnowledgeGraphModel,
  type KnowledgeGraphNode,
} from './knowledgeGraphModel';

type GraphLayout = 'cose' | 'breadthfirst' | 'circle' | 'grid';

type KnowledgeGraphVisualizationProps = {
  concepts: KnowledgeConceptRead[];
  knowledgeBaseKey: string;
  onViewConcept: (concept: KnowledgeConceptRead) => void;
};

type GraphCanvasHandle = {
  fit: () => void;
  relayout: () => void;
};

const GRAPH_LAYOUTS: Array<{ value: GraphLayout; label: string }> = [
  { value: 'cose', label: '力导向' },
  { value: 'breadthfirst', label: '层级' },
  { value: 'circle', label: '环形' },
  { value: 'grid', label: '网格' },
];

const TYPE_COLORS: Record<string, string> = {
  'Source Document': '#1a71ff',
  'Source Section': '#5b8ff9',
  Topic: '#22a06b',
  'Business Rule': '#8b5cf6',
  Playbook: '#f59e0b',
  Policy: '#7a35cd',
  Skill: '#0891a5',
  Metric: '#d14c8b',
};

export function KnowledgeGraphVisualization({
  concepts,
  knowledgeBaseKey,
  onViewConcept,
}: KnowledgeGraphVisualizationProps) {
  const [query, setQuery] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [layout, setLayout] = useState<GraphLayout>('cose');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [scopedConceptIds, setScopedConceptIds] = useState<Set<string> | null>(null);
  const [expanded, setExpanded] = useState(false);
  const inlineCanvasRef = useRef<GraphCanvasHandle>(null);
  const expandedCanvasRef = useRef<GraphCanvasHandle>(null);

  useEffect(() => {
    setQuery('');
    setSelectedTypes([]);
    setShowArchived(false);
    setLayout('cose');
    setSelectedNodeId(null);
    setScopedConceptIds(null);
    setExpanded(false);
  }, [knowledgeBaseKey]);

  const availableConcepts = useMemo(
    () => concepts.filter((concept) => showArchived || concept.status !== 'archived'),
    [concepts, showArchived],
  );
  const conceptTypes = useMemo(
    () => [...new Set(availableConcepts.map((concept) => concept.concept_type))].sort(),
    [availableConcepts],
  );
  const scopeCandidates = useMemo(
    () => availableConcepts.filter((concept) => (
      (selectedTypes.length === 0 || selectedTypes.includes(concept.concept_type))
      && knowledgeConceptMatches(concept, query)
    )),
    [availableConcepts, query, selectedTypes],
  );
  const needsScopeSelection = availableConcepts.length > KNOWLEDGE_GRAPH_NODE_LIMIT && !scopedConceptIds;
  const graphConcepts = useMemo(
    () => scopedConceptIds
      ? availableConcepts.filter((concept) => scopedConceptIds.has(concept.concept_id))
      : availableConcepts,
    [availableConcepts, scopedConceptIds],
  );
  const model = useMemo(
    () => buildKnowledgeGraph(graphConcepts, { includeArchived: true }),
    [graphConcepts],
  );
  const searchResults = useMemo(
    () => query.trim()
      ? model.nodes.filter((node) => knowledgeConceptMatches(node.concept, query)).slice(0, 12)
      : [],
    [model, query],
  );

  useEffect(() => {
    if (needsScopeSelection || model.nodes.length === 0) {
      setSelectedNodeId(null);
      return;
    }
    setSelectedNodeId((current) => (
      current && model.nodeById.has(current)
        ? current
        : highestDegreeActiveNode(model)?.id || model.nodes[0]?.id || null
    ));
  }, [model, needsScopeSelection]);

  const toggleType = (type: string) => {
    setSelectedTypes((current) => current.includes(type)
      ? current.filter((item) => item !== type)
      : [...current, type]);
  };
  const handleSelectNode = useCallback((id: string | null) => {
    setSelectedNodeId(id);
  }, []);
  const handleViewConcept = useCallback((concept: KnowledgeConceptRead) => {
    setExpanded(false);
    onViewConcept(concept);
  }, [onViewConcept]);
  const confirmScope = () => {
    if (scopeCandidates.length === 0 || scopeCandidates.length > KNOWLEDGE_GRAPH_NODE_LIMIT) return;
    setScopedConceptIds(new Set(scopeCandidates.map((concept) => concept.concept_id)));
  };
  const resetScope = () => {
    setScopedConceptIds(null);
    setQuery('');
    setSelectedTypes([]);
    setSelectedNodeId(null);
  };
  const resetView = () => {
    setQuery('');
    setSelectedTypes([]);
    setSelectedNodeId(highestDegreeActiveNode(model)?.id || model.nodes[0]?.id || null);
    window.setTimeout(() => activeCanvasRef(expanded, inlineCanvasRef, expandedCanvasRef).current?.fit(), 0);
  };

  if (availableConcepts.length === 0) {
    return (
      <div className="kgv-empty">
        <Network aria-hidden="true" />
        <strong>暂无可视化知识</strong>
        <span>当前知识库还没有可展示的知识概念。</span>
      </div>
    );
  }

  const workspace = (isExpanded: boolean) => (
    <GraphWorkspace
      ref={isExpanded ? expandedCanvasRef : inlineCanvasRef}
      model={model}
      query={query}
      selectedTypes={selectedTypes}
      selectedNodeId={selectedNodeId}
      layout={layout}
      onSelectNode={handleSelectNode}
      onViewConcept={handleViewConcept}
      showDetail
      expanded={isExpanded}
    />
  );

  return (
    <div className="kgv-root">
      <div className="kgv-toolbar">
        <div className="kgv-toolbar-main">
          <div className="kgv-search">
            <Search aria-hidden="true" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索标题、路径或标签"
              aria-label="搜索知识图谱"
            />
            {searchResults.length > 0 && !needsScopeSelection && (
              <div className="kgv-search-results" role="listbox" aria-label="知识图谱搜索结果">
                {searchResults.map((node) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={selectedNodeId === node.id}
                    key={node.id}
                    onClick={() => handleSelectNode(node.id)}
                  >
                    <span style={{ background: typeColor(node.concept.concept_type) }} />
                    <strong>{node.concept.title || node.id}</strong>
                    <small>{node.id}</small>
                  </button>
                ))}
              </div>
            )}
          </div>

          <select
            className="kgv-layout-select"
            value={layout}
            aria-label="图谱布局"
            onChange={(event) => setLayout(event.target.value as GraphLayout)}
          >
            {GRAPH_LAYOUTS.map((item) => (
              <option value={item.value} key={item.value}>{item.label}</option>
            ))}
          </select>

          <button
            type="button"
            className={cn('kgv-archive-toggle', showArchived && 'is-active')}
            aria-pressed={showArchived}
            onClick={() => {
              setShowArchived((current) => !current);
              setScopedConceptIds(null);
            }}
          >
            显示已归档
          </button>

          <div className="kgv-toolbar-actions">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              title="重新布局"
              aria-label="重新布局"
              disabled={needsScopeSelection}
              onClick={() => activeCanvasRef(expanded, inlineCanvasRef, expandedCanvasRef).current?.relayout()}
            >
              <RotateCcw />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              title="适应画布"
              aria-label="适应画布"
              disabled={needsScopeSelection}
              onClick={() => activeCanvasRef(expanded, inlineCanvasRef, expandedCanvasRef).current?.fit()}
            >
              <Focus />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              title="重置视图"
              aria-label="重置视图"
              disabled={needsScopeSelection}
              onClick={resetView}
            >
              <Grid2X2 />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              title="展开图谱"
              aria-label="展开图谱"
              disabled={needsScopeSelection}
              onClick={() => setExpanded(true)}
            >
              <Maximize2 />
            </Button>
          </div>
        </div>

        <div className="kgv-type-legend" aria-label="按知识类型筛选">
          {conceptTypes.map((type) => {
            const active = selectedTypes.length === 0 || selectedTypes.includes(type);
            const count = availableConcepts.filter((concept) => concept.concept_type === type).length;
            return (
              <button
                type="button"
                key={type}
                className={cn(!active && 'is-muted')}
                aria-pressed={active}
                onClick={() => toggleType(type)}
              >
                <span style={{ background: typeColor(type) }} />
                {type}
                <small>{count}</small>
              </button>
            );
          })}
        </div>
      </div>

      {needsScopeSelection ? (
        <GraphScopeGate
          total={availableConcepts.length}
          candidateCount={scopeCandidates.length}
          onConfirm={confirmScope}
        />
      ) : (
        <>
          {scopedConceptIds && (
            <div className="kgv-scope-note">
              <span>当前绘制筛选后的 {model.stats.nodeCount} 个节点，不代表整库全貌。</span>
              <button type="button" onClick={resetScope}>重新选择范围</button>
            </div>
          )}
          {!expanded && workspace(false)}
          <GraphStats model={model} />
        </>
      )}

      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent
          aria-describedby={undefined}
          className="kgv-expanded-dialog"
        >
          <div className="kgv-expanded-header">
            <DialogTitle className="kgv-expanded-title">
              <Network aria-hidden="true" />
              知识图谱可视化
            </DialogTitle>
            <div className="kgv-expanded-controls">
              <div className="kgv-search">
                <Search aria-hidden="true" />
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索标题、路径或标签"
                  aria-label="在展开图谱中搜索"
                />
              </div>
              <select
                className="kgv-layout-select"
                value={layout}
                aria-label="展开图谱布局"
                onChange={(event) => setLayout(event.target.value as GraphLayout)}
              >
                {GRAPH_LAYOUTS.map((item) => (
                  <option value={item.value} key={item.value}>{item.label}</option>
                ))}
              </select>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                title="重新布局"
                aria-label="在展开图谱中重新布局"
                onClick={() => expandedCanvasRef.current?.relayout()}
              >
                <RotateCcw />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                title="适应画布"
                aria-label="在展开图谱中适应画布"
                onClick={() => expandedCanvasRef.current?.fit()}
              >
                <Focus />
              </Button>
            </div>
          </div>
          <div className="kgv-expanded-content">{expanded && workspace(true)}</div>
          <GraphStats model={model} />
        </DialogContent>
      </Dialog>

    </div>
  );
}

const GraphWorkspace = forwardRef<GraphCanvasHandle, {
  model: KnowledgeGraphModel;
  query: string;
  selectedTypes: string[];
  selectedNodeId: string | null;
  layout: GraphLayout;
  onSelectNode: (id: string | null) => void;
  onViewConcept: (concept: KnowledgeConceptRead) => void;
  showDetail: boolean;
  expanded: boolean;
}>(function GraphWorkspace({
  model,
  query,
  selectedTypes,
  selectedNodeId,
  layout,
  onSelectNode,
  onViewConcept,
  showDetail,
  expanded,
}, ref) {
  const canvasRef = useRef<GraphCanvasHandle>(null);
  useImperativeHandle(ref, () => ({
    fit: () => canvasRef.current?.fit(),
    relayout: () => canvasRef.current?.relayout(),
  }), []);
  const selectedNode = selectedNodeId ? model.nodeById.get(selectedNodeId) || null : null;
  return (
    <div className={cn('kgv-workspace', expanded && 'is-expanded')}>
      <GraphCanvas
        ref={canvasRef}
        model={model}
        query={query}
        selectedTypes={selectedTypes}
        selectedNodeId={selectedNodeId}
        layout={layout}
        onSelectNode={onSelectNode}
      />
      {showDetail && (
        <aside className="kgv-detail" aria-label="知识节点详情">
          {selectedNode ? (
            <NodeDetail
              node={selectedNode}
              model={model}
              onSelectNode={onSelectNode}
              onViewConcept={onViewConcept}
            />
          ) : (
            <div className="kgv-detail-empty">
              <Expand aria-hidden="true" />
              <strong>选择一个知识节点</strong>
              <span>查看摘要、上下游关系和未解析链接。</span>
            </div>
          )}
        </aside>
      )}
    </div>
  );
});

const GraphCanvas = forwardRef<GraphCanvasHandle, {
  model: KnowledgeGraphModel;
  query: string;
  selectedTypes: string[];
  selectedNodeId: string | null;
  layout: GraphLayout;
  onSelectNode: (id: string | null) => void;
}>(function GraphCanvas({ model, query, selectedTypes, selectedNodeId, layout, onSelectNode }, ref) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const lastLayoutRef = useRef(layout);

  const runLayout = () => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.layout(layoutOptions(layout, false)).run();
  };
  useImperativeHandle(ref, () => ({
    fit: () => cyRef.current?.fit(undefined, 40),
    relayout: runLayout,
  }));

  useEffect(() => {
    if (!containerRef.current) return;
    const elements: ElementDefinition[] = [
      ...model.nodes.map((node) => ({
        data: {
          id: node.id,
          label: node.concept.title || node.id,
          type: node.concept.concept_type,
          color: typeColor(node.concept.concept_type),
          size: Math.min(52, 28 + Math.sqrt(node.degree) * 7),
          degree: node.degree,
          archived: node.concept.status === 'archived',
          hub: node.degree >= 3,
        },
      })),
      ...model.edges.map((edge) => ({ data: edge })),
    ];
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': 'data(color)',
            'border-color': '#ffffff',
            'border-width': 2,
            color: '#18181a',
            'font-family': 'Geist Variable, PingFang SC, sans-serif',
            'font-size': 11,
            height: 'data(size)',
            label: 'data(label)',
            'overlay-opacity': 0,
            'text-background-color': '#ffffff',
            'text-background-opacity': 0.88,
            'text-background-padding': '3px',
            'text-margin-y': 5,
            'text-max-width': '124px',
            'text-outline-width': 0,
            'text-valign': 'bottom',
            'text-wrap': 'ellipsis',
            width: 'data(size)',
          },
        },
        {
          selector: 'node[?archived]',
          style: {
            opacity: 0.42,
            'border-color': '#858b9c',
            'border-style': 'dashed',
          },
        },
        {
          selector: 'node.is-selected',
          style: {
            'border-color': '#1a71ff',
            'border-width': 4,
            'underlay-color': '#1a71ff',
            'underlay-opacity': 0.14,
            'underlay-padding': 8,
          },
        },
        { selector: 'node.label-hidden', style: { label: '' } },
        { selector: '.is-dimmed', style: { opacity: 0.1 } },
        {
          selector: 'edge',
          style: {
            'arrow-scale': 0.8,
            'curve-style': 'bezier',
            'line-color': '#d8dee9',
            'target-arrow-color': '#aeb7c6',
            'target-arrow-shape': 'triangle',
            width: 1.35,
          },
        },
        {
          selector: 'edge.is-related',
          style: {
            'line-color': '#1a71ff',
            'target-arrow-color': '#1a71ff',
            opacity: 0.82,
            width: 2.2,
          },
        },
      ],
      layout: layoutOptions(layout, false),
      minZoom: 0.2,
      maxZoom: 3,
    });
    cy.on('tap', 'node', (event: EventObject) => onSelectNode(String(event.target.id())));
    cy.on('tap', (event: EventObject) => {
      if (event.target === cy) onSelectNode(null);
    });
    cy.on('zoom', () => updateSemanticLabels(cy));
    cyRef.current = cy;
    updateSemanticLabels(cy);
    window.setTimeout(() => cy.fit(undefined, 40), 0);
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [model, onSelectNode]);

  useEffect(() => {
    if (lastLayoutRef.current === layout) return;
    lastLayoutRef.current = layout;
    runLayout();
  }, [layout]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass('is-dimmed is-related is-selected');
    const selected = selectedNodeId ? cy.getElementById(selectedNodeId) : cy.collection();
    const matchingNodes = cy.nodes().filter((node: NodeSingular) => {
      const graphNode = model.nodeById.get(node.id());
      if (!graphNode) return false;
      const matchesType = selectedTypes.length === 0 || selectedTypes.includes(graphNode.concept.concept_type);
      return matchesType && knowledgeConceptMatches(graphNode.concept, query);
    });
    cy.nodes().difference(matchingNodes).addClass('is-dimmed');
    cy.edges().forEach((edge: EdgeSingular) => {
      if (edge.source().hasClass('is-dimmed') || edge.target().hasClass('is-dimmed')) edge.addClass('is-dimmed');
    });
    if (selected.length) {
      const neighborhood = selected.closedNeighborhood();
      cy.elements().difference(neighborhood).addClass('is-dimmed');
      neighborhood.edges().addClass('is-related');
      selected.removeClass('is-dimmed').addClass('is-selected');
    }
    updateSemanticLabels(cy);
  }, [model, query, selectedNodeId, selectedTypes]);

  return <div ref={containerRef} className="kgv-canvas" role="img" aria-label="知识概念关系图" />;
});

function NodeDetail({
  node,
  model,
  onSelectNode,
  onViewConcept,
}: {
  node: KnowledgeGraphNode;
  model: KnowledgeGraphModel;
  onSelectNode: (id: string) => void;
  onViewConcept: (concept: KnowledgeConceptRead) => void;
}) {
  const tags = Array.isArray(node.concept.frontmatter?.tags) ? node.concept.frontmatter.tags : [];
  return (
    <div className="kgv-detail-content">
      <div className="kgv-detail-head">
        <div>
          <span className="kgv-type-badge" style={{ '--kgv-type-color': typeColor(node.concept.concept_type) } as React.CSSProperties}>
            {node.concept.concept_type}
          </span>
          {node.concept.status === 'archived' && <span className="kgv-status-badge">已归档</span>}
        </div>
        <h4>{node.concept.title || node.id}</h4>
        <code>{node.id}</code>
      </div>
      <KnowledgeMarkdownSummary node={node} model={model} onSelectNode={onSelectNode} />
      {tags.length > 0 && (
        <div className="kgv-tags">
          {tags.slice(0, 8).map((tag) => <span key={String(tag)}>{String(tag)}</span>)}
        </div>
      )}
      <div className="kgv-relation-summary">
        <span><ArrowDownLeft aria-hidden="true" />{node.inbound.length} 个上游</span>
        <span><ArrowUpRight aria-hidden="true" />{node.outbound.length} 个下游</span>
      </div>
      <RelationList
        title="引用此节点"
        ids={node.inbound}
        model={model}
        onSelectNode={onSelectNode}
      />
      <RelationList
        title="此节点关联"
        ids={node.outbound}
        model={model}
        onSelectNode={onSelectNode}
      />
      {node.unresolvedLinks.length > 0 && (
        <section className="kgv-unresolved">
          <strong>外部或未解析链接</strong>
          <ul>
            {node.unresolvedLinks.slice(0, 8).map((link, index) => (
              <li key={`${link.target}-${index}`}>
                <span>{link.label || link.target}</span>
                <code>{link.target}</code>
              </li>
            ))}
          </ul>
        </section>
      )}
      <Button type="button" className="kgv-view-button" onClick={() => onViewConcept(node.concept)}>
        查看完整内容
      </Button>
    </div>
  );
}

function KnowledgeMarkdownSummary({
  node,
  model,
  onSelectNode,
}: {
  node: KnowledgeGraphNode;
  model: KnowledgeGraphModel;
  onSelectNode: (id: string) => void;
}) {
  const markdown = knowledgeConceptPreviewMarkdown(node.concept) || '暂无摘要';
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const previewRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setExpanded(false);
  }, [node.id]);

  useEffect(() => {
    if (expanded) return;
    const preview = previewRef.current;
    if (!preview) return;
    const measure = () => setOverflowing(preview.scrollHeight > preview.clientHeight + 1);
    const frame = window.requestAnimationFrame(measure);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(preview);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [expanded, markdown]);

  return (
    <section className="kgv-markdown-summary" aria-label="知识摘要">
      <div
        ref={previewRef}
        className={cn('kgv-markdown-preview', !expanded && 'is-collapsed', overflowing && !expanded && 'has-overflow')}
      >
        {renderMarkdownBlocks(markdown, false, {
          renderInternalLink: ({ label, href, key }) => {
            const resolved = resolveKnowledgeLink(node.id, href);
            const targetId = resolved.kind === 'internal' ? resolved.conceptId : '';
            if (targetId && model.nodeById.has(targetId)) {
              return (
                <button
                  type="button"
                  key={key}
                  className="kgv-md-internal-link"
                  title={targetId}
                  onClick={() => onSelectNode(targetId)}
                >
                  {label}
                </button>
              );
            }
            return <span key={key} className="md-link-label" title={href}>{label}</span>;
          },
        })}
      </div>
      {(overflowing || expanded) && (
        <button
          type="button"
          className="kgv-summary-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
          {expanded ? '收起摘要' : '展开摘要'}
        </button>
      )}
    </section>
  );
}

function RelationList({
  title,
  ids,
  model,
  onSelectNode,
}: {
  title: string;
  ids: string[];
  model: KnowledgeGraphModel;
  onSelectNode: (id: string) => void;
}) {
  if (ids.length === 0) return null;
  return (
    <section className="kgv-relation-list">
      <strong>{title}</strong>
      <div>
        {ids.slice(0, 12).map((id) => (
          <button type="button" key={id} onClick={() => onSelectNode(id)}>
            <span style={{ background: typeColor(model.nodeById.get(id)?.concept.concept_type || '') }} />
            {model.nodeById.get(id)?.concept.title || id}
          </button>
        ))}
      </div>
    </section>
  );
}

function GraphScopeGate({
  total,
  candidateCount,
  onConfirm,
}: {
  total: number;
  candidateCount: number;
  onConfirm: () => void;
}) {
  const canDraw = candidateCount > 0 && candidateCount <= KNOWLEDGE_GRAPH_NODE_LIMIT;
  return (
    <div className="kgv-scope-gate">
      <Network aria-hidden="true" />
      <strong>知识库包含 {total} 个节点</strong>
      <p>为保证交互流畅，请先通过搜索或类型筛选，将范围缩小到 {KNOWLEDGE_GRAPH_NODE_LIMIT} 个节点以内。</p>
      <span className={cn(canDraw && 'is-ready')}>当前匹配 {candidateCount} 个节点</span>
      <Button type="button" disabled={!canDraw} onClick={onConfirm}>
        绘制这 {candidateCount} 个节点
      </Button>
    </div>
  );
}

function GraphStats({ model }: { model: KnowledgeGraphModel }) {
  return (
    <div className="kgv-stats" aria-label="知识图谱统计">
      <span><strong>{model.stats.nodeCount}</strong> 节点</span>
      <span><strong>{model.stats.edgeCount}</strong> 关系</span>
      <span><strong>{model.stats.isolatedCount}</strong> 孤立节点</span>
      <span><strong>{model.stats.unresolvedCount}</strong> 未解析链接</span>
    </div>
  );
}

function layoutOptions(layout: GraphLayout, animate: boolean): LayoutOptions {
  if (layout === 'breadthfirst') {
    return { name: layout, directed: true, padding: 40, spacingFactor: 1.2, animate };
  }
  if (layout === 'cose') {
    return { name: layout, padding: 40, nodeRepulsion: () => 5600, idealEdgeLength: () => 92, animate };
  }
  return { name: layout, padding: 40, animate };
}

function updateSemanticLabels(cy: Core) {
  const lowZoom = cy.zoom() < 0.72;
  cy.nodes().forEach((node: NodeSingular) => {
    const keepLabel = !lowZoom || Boolean(node.data('hub')) || node.hasClass('is-selected');
    node.toggleClass('label-hidden', !keepLabel);
  });
}

function typeColor(type: string): string {
  if (TYPE_COLORS[type]) return TYPE_COLORS[type];
  const palette = ['#1a71ff', '#22a06b', '#8b5cf6', '#f59e0b', '#0891a5', '#d14c8b'];
  const hash = [...type].reduce((total, character) => total + character.charCodeAt(0), 0);
  return palette[hash % palette.length];
}

function activeCanvasRef(
  expanded: boolean,
  inlineRef: React.RefObject<GraphCanvasHandle | null>,
  expandedRef: React.RefObject<GraphCanvasHandle | null>,
) {
  return expanded ? expandedRef : inlineRef;
}
