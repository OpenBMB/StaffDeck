import {
  CheckOutlined,
  CloseOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  FileAddOutlined,
  HistoryOutlined,
  InboxOutlined,
  MoreOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { Button, Card, Col, Collapse, Dropdown, Empty, Input, Modal, Progress, Row, Select, Space, Table, Tag, Typography, Upload, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import type {
  KnowledgeBaseRead,
  KnowledgeBucketRead,
  KnowledgeChunkRead,
  KnowledgeDiscoveryRead,
  KnowledgeDocumentRead,
  KnowledgeIngestJobRead,
  KnowledgeSearchResponse,
  AgentProfileRead,
} from '../types';

const { Dragger } = Upload;
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type KnowledgeBaseVersionRead = {
  id: string;
  version: string;
  name: string;
  description?: string;
  status: string;
  is_head: boolean;
  is_base: boolean;
  updated_at: string;
  created_at: string;
};

type IngestStepView = {
  key: string;
  label: string;
  progress: number;
  status: 'pending' | 'running' | 'done';
};

const DEFAULT_INGEST_STEPS: IngestStepView[] = [
  { key: 'queued', label: '排队中', progress: 0, status: 'pending' },
  { key: 'parsing', label: '解析', progress: 0.08, status: 'pending' },
  { key: 'normalizing', label: '整理', progress: 0.16, status: 'pending' },
  { key: 'documenting', label: '写入', progress: 0.24, status: 'pending' },
  { key: 'bucketing', label: '分桶', progress: 0.36, status: 'pending' },
  { key: 'bucket_writing', label: '桶摘要', progress: 0.48, status: 'pending' },
  { key: 'chunking', label: '切片', progress: 0.62, status: 'pending' },
  { key: 'summarizing', label: '整理', progress: 0.74, status: 'pending' },
  { key: 'discovering', label: '发现', progress: 0.88, status: 'pending' },
  { key: 'done', label: '完成', progress: 1, status: 'pending' },
];

export default function KnowledgeManagePage() {
  const navigate = useNavigate();
  const [documents, setDocuments] = useState<KnowledgeDocumentRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [selectedDocument, setSelectedDocument] = useState<KnowledgeDocumentRead | null>(null);
  const [buckets, setBuckets] = useState<KnowledgeBucketRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  const [importSourceAgentId, setImportSourceAgentId] = useState('');
  const [importSourceKnowledgeBases, setImportSourceKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [importSelectedKnowledgeBaseIds, setImportSelectedKnowledgeBaseIds] = useState<string[]>([]);
  const [importLoading, setImportLoading] = useState(false);
  const [editingKnowledgeBase, setEditingKnowledgeBase] = useState<KnowledgeBaseRead | null>(null);
  const [knowledgeBaseDraft, setKnowledgeBaseDraft] = useState({ name: '', description: '', status: 'active' });
  const [versionKnowledgeBase, setVersionKnowledgeBase] = useState<KnowledgeBaseRead | null>(null);
  const [knowledgeBaseVersions, setKnowledgeBaseVersions] = useState<KnowledgeBaseVersionRead[]>([]);
  const [editingDocument, setEditingDocument] = useState<KnowledgeDocumentRead | null>(null);
  const [documentDraft, setDocumentDraft] = useState({ title: '', status: 'ready' });
  const [editingBucket, setEditingBucket] = useState<KnowledgeBucketRead | null>(null);
  const [bucketDraft, setBucketDraft] = useState({ title: '', summary: '' });
  const [bucketChunks, setBucketChunks] = useState<KnowledgeChunkRead[]>([]);
  const [chunkDrafts, setChunkDrafts] = useState<Record<string, { content: string; summary: string }>>({});
  const [contentSaving, setContentSaving] = useState(false);
  const [documentSearch, setDocumentSearch] = useState('');
  const [knowledgeBaseFilter, setKnowledgeBaseFilter] = useState('__all__');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResponse | null>(null);

  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const isOverallAgent = !currentAgent || currentAgent.is_overall;
  const visibleKnowledgeBases = useMemo(
    () => knowledgeBases.filter((item) => !isEmptyDefaultKnowledgeBase(item)),
    [knowledgeBases],
  );
  const selectedKnowledgeBase = useMemo(() => {
    if (selectedDocument) {
      return visibleKnowledgeBases.find((item) => item.id === selectedDocument.knowledge_base_id) || null;
    }
    if (knowledgeBaseFilter !== '__all__') {
      return visibleKnowledgeBases.find((item) => item.id === knowledgeBaseFilter) || null;
    }
    return visibleKnowledgeBases[0] || null;
  }, [knowledgeBaseFilter, selectedDocument, visibleKnowledgeBases]);
  const filteredKnowledgeBases = useMemo(() => {
    const query = documentSearch.trim().toLowerCase();
    if (!query) return visibleKnowledgeBases;
    return visibleKnowledgeBases.filter((item) => {
      const searchable = [
        item.name,
        item.description,
        item.status,
        item.version,
        item.branch_sync_state,
        item.document_count,
        item.bucket_count,
        item.chunk_count,
      ]
        .filter((value) => value !== undefined && value !== null)
        .join(' ')
        .toLowerCase();
      return searchable.includes(query);
    });
  }, [documentSearch, visibleKnowledgeBases]);
  useEffect(() => {
    void refresh();
  }, [agentId]);

  useEffect(() => {
    if (knowledgeBaseFilter !== '__all__' && !visibleKnowledgeBases.some((item) => item.id === knowledgeBaseFilter)) {
      setKnowledgeBaseFilter('__all__');
    }
  }, [visibleKnowledgeBases, knowledgeBaseFilter]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setAgentId((event as CustomEvent<{ agentId?: string }>).detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  async function refresh() {
    setLoading(true);
    const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    try {
      const [docRows, kbRows, agentRows] = await Promise.all([
        api.get<KnowledgeDocumentRead[]>(`/api/enterprise/knowledge/documents?tenant_id=${TENANT_ID}&include_all_versions=true${suffix}`),
        api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${suffix}`),
        api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
      ]);
      setDocuments(docRows);
      setKnowledgeBases(kbRows);
      setAgents(agentRows);
      const scopedDocRows =
        knowledgeBaseFilter === '__all__'
          ? docRows
          : docRows.filter((item) => item.knowledge_base_id === knowledgeBaseFilter);
      const current = selectedDocument
        ? scopedDocRows.find((item) => item.id === selectedDocument.id) || scopedDocRows[0] || null
        : scopedDocRows[0] || null;
      setSelectedDocument(current);
      if (current) {
        await loadBuckets(current, false);
      } else {
        setBuckets([]);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '刷新知识库失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadBuckets(document: KnowledgeDocumentRead, select = true) {
    if (select) setSelectedDocument(document);
    setSearchResult(null);
    try {
      const rows = await api.get<KnowledgeBucketRead[]>(
        `/api/enterprise/knowledge/documents/${document.id}/buckets?tenant_id=${TENANT_ID}`,
      );
      setBuckets(rows);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载知识桶失败');
    }
  }

  function selectKnowledgeBase(knowledgeBaseId: string) {
    setKnowledgeBaseFilter(knowledgeBaseId);
    const nextDocument =
      knowledgeBaseId === '__all__'
        ? documents[0] || null
        : documents.find((item) => item.knowledge_base_id === knowledgeBaseId) || null;
    if (nextDocument) {
      void loadBuckets(nextDocument);
      return;
    }
    setSelectedDocument(null);
    setBuckets([]);
    setSearchResult(null);
  }

  async function runKnowledgeSearch() {
    const query = searchQuery.trim();
    if (!query) {
      message.warning('请输入要调试的知识问题');
      return;
    }
    setSearchLoading(true);
    try {
      const response = await api.post<KnowledgeSearchResponse>('/api/enterprise/knowledge/search', {
        tenant_id: TENANT_ID,
        agent_id: agentId || undefined,
        knowledge_base_ids:
          knowledgeBaseFilter !== '__all__'
            ? [knowledgeBaseFilter]
            : selectedDocument?.knowledge_base_id
              ? [selectedDocument.knowledge_base_id]
              : undefined,
        query,
        mode: 'debug',
        max_depth: 3,
        need_evidence_pack: true,
      });
      setSearchResult(response);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '知识检索失败');
    } finally {
      setSearchLoading(false);
    }
  }

  async function openImportKnowledgeBases() {
    try {
      const agentRows = agents.length ? agents : await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(agentRows);
      const firstSource = agentRows.find((item) => item.id !== agentId)?.id || '';
      setImportSourceAgentId(firstSource);
      setImportSelectedKnowledgeBaseIds([]);
      setImportOpen(true);
      if (firstSource) {
        await loadImportSourceKnowledgeBases(firstSource);
      } else {
        setImportSourceKnowledgeBases([]);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载员工失败');
    }
  }

  async function loadImportSourceKnowledgeBases(sourceAgentId: string) {
    setImportSourceKnowledgeBases([]);
    setImportSelectedKnowledgeBaseIds([]);
    if (!sourceAgentId) return;
    try {
      const rows = await api.get<KnowledgeBaseRead[]>(
        `/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      setImportSourceKnowledgeBases(rows);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载来源知识库失败');
    }
  }

  async function submitImportKnowledgeBases() {
    if (!agentId) {
      message.warning('请先选择目标员工');
      return;
    }
    if (!importSourceAgentId) {
      message.warning('请选择来源员工');
      return;
    }
    if (importSelectedKnowledgeBaseIds.length === 0) {
      message.warning('请选择要学习的业务资料');
      return;
    }
    setImportLoading(true);
    try {
      const result = await api.post<{ imported: Array<Record<string, unknown>>; missing: Array<Record<string, unknown>> }>(
        `/api/enterprise/agents/${agentId}/resources/import`,
        {
          tenant_id: TENANT_ID,
          source_agent_id: importSourceAgentId,
          resource_type: 'knowledge_base',
          resource_ids: importSelectedKnowledgeBaseIds,
        },
      );
      const importedCount = result.imported?.length || 0;
      const missingCount = result.missing?.length || 0;
      message.success(`已学习 ${importedCount} 个业务资料${missingCount ? `，${missingCount} 个未学习` : ''}`);
      setImportOpen(false);
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '导入失败');
    } finally {
      setImportLoading(false);
    }
  }

  function openEditKnowledgeBase(row: KnowledgeBaseRead) {
    setEditingKnowledgeBase(row);
    setKnowledgeBaseDraft({
      name: row.name,
      description: row.description || '',
      status: row.status === 'archived' ? 'archived' : 'active',
    });
  }

  async function saveKnowledgeBase() {
    if (!editingKnowledgeBase) return;
    const suffix = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
    try {
      const next = await api.put<KnowledgeBaseRead>(`/api/enterprise/knowledge-bases/${editingKnowledgeBase.id}${suffix}`, {
        tenant_id: TENANT_ID,
        name: knowledgeBaseDraft.name,
        description: knowledgeBaseDraft.description,
        status: knowledgeBaseDraft.status,
      });
      setKnowledgeBases((current) => current.map((item) => (item.id === next.id ? next : item)));
      setEditingKnowledgeBase(null);
      message.success('已保存知识库');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存知识库失败');
    }
  }

  async function setKnowledgeBaseStatus(row: KnowledgeBaseRead, active: boolean) {
    const suffix = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
    try {
      const next = await api.put<KnowledgeBaseRead>(`/api/enterprise/knowledge-bases/${row.id}${suffix}`, {
        tenant_id: TENANT_ID,
        status: active ? 'active' : 'archived',
      });
      setKnowledgeBases((current) => current.map((item) => (item.id === next.id ? next : item)));
      message.success(active ? '已上线知识库' : '已下线知识库');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : active ? '上线失败' : '下线失败');
    }
  }

  function deleteKnowledgeBase(row: KnowledgeBaseRead) {
    const branchMode = !isOverallAgent;
    Modal.confirm({
      title: branchMode ? `从当前员工移除业务资料：${row.name}` : `删除业务资料：${row.name}`,
      content: branchMode
        ? '这只会在当前员工中隐藏该业务资料；组织资源库和其他员工仍然保留。'
        : '组织资源库会永久删除该业务资料及其文档、桶、片段和版本记录。',
      okText: branchMode ? '移除' : '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
        try {
          await api.delete(`/api/enterprise/knowledge-bases/${row.id}?tenant_id=${TENANT_ID}${suffix}`);
          message.success(branchMode ? '已从当前员工移除业务资料' : '已删除业务资料');
          await refresh();
        } catch (error) {
          message.error(error instanceof Error ? error.message : '删除失败');
        }
      },
    });
  }

  async function openKnowledgeBaseVersions(row: KnowledgeBaseRead) {
    const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    try {
      const versions = await api.get<KnowledgeBaseVersionRead[]>(
        `/api/enterprise/knowledge-bases/${row.id}/versions?tenant_id=${TENANT_ID}${suffix}`,
      );
      setVersionKnowledgeBase(row);
      setKnowledgeBaseVersions(versions);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载版本失败');
    }
  }

  async function syncKnowledgeBaseFromOverall(row: KnowledgeBaseRead) {
    if (!agentId) {
      message.warning('请先选择员工');
      return;
    }
    try {
      await api.post(`/api/enterprise/knowledge-bases/${row.id}/sync-from-overall?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`);
      message.success('已同步组织业务资料');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '同步失败');
    }
  }

  async function promoteKnowledgeBaseToOverall(row: KnowledgeBaseRead) {
    if (!agentId) {
      message.warning('请先选择员工');
      return;
    }
    try {
      await api.post(`/api/enterprise/knowledge-bases/${row.id}/promote-to-overall?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`);
      message.success('已沉淀到组织业务资料');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '推送失败');
    }
  }

  async function rollbackKnowledgeBaseVersion(version: KnowledgeBaseVersionRead) {
    if (!versionKnowledgeBase || !agentId) return;
    try {
      await api.post(`/api/enterprise/knowledge-bases/${versionKnowledgeBase.id}/rollback`, {
        tenant_id: TENANT_ID,
        agent_id: agentId,
        version: version.version,
      });
      message.success(`已回滚到 ${version.version}`);
      await openKnowledgeBaseVersions(versionKnowledgeBase);
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '回滚失败');
    }
  }

  function openEditDocument(row: KnowledgeDocumentRead) {
    setEditingDocument(row);
    setDocumentDraft({
      title: row.title || row.filename,
      status: row.status,
    });
  }

  async function saveDocument() {
    if (!editingDocument) return;
    try {
      const next = await api.put<KnowledgeDocumentRead>(`/api/enterprise/knowledge/documents/${editingDocument.id}`, {
        tenant_id: TENANT_ID,
        title: documentDraft.title,
        status: documentDraft.status,
      });
      setDocuments((current) => current.map((item) => (item.id === next.id ? next : item)));
      setSelectedDocument((current) => (current?.id === next.id ? next : current));
      setEditingDocument(null);
      message.success('已保存文档');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存文档失败');
    }
  }

  async function openBucketEditor(row: KnowledgeBucketRead) {
    setEditingBucket(row);
    setBucketDraft({ title: row.title, summary: row.summary });
    try {
      const chunks = await api.get<KnowledgeChunkRead[]>(`/api/enterprise/knowledge/buckets/${row.id}/chunks?tenant_id=${TENANT_ID}`);
      setBucketChunks(chunks);
      setChunkDrafts(
        Object.fromEntries(chunks.map((chunk) => [chunk.id, { content: chunk.content, summary: chunk.summary || '' }])),
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载片段失败');
    }
  }

  async function saveBucketAndChunks() {
    if (!editingBucket) return;
    setContentSaving(true);
    try {
      await api.put<KnowledgeBucketRead>(`/api/enterprise/knowledge/buckets/${editingBucket.id}`, {
        tenant_id: TENANT_ID,
        title: bucketDraft.title,
        summary: bucketDraft.summary,
      });
      await Promise.all(
        bucketChunks.map((chunk) =>
          api.put<KnowledgeChunkRead>(`/api/enterprise/knowledge/chunks/${chunk.id}`, {
            tenant_id: TENANT_ID,
            content: chunkDrafts[chunk.id]?.content ?? chunk.content,
            summary: chunkDrafts[chunk.id]?.summary ?? chunk.summary,
          }),
        ),
      );
      message.success('已保存知识内容');
      setEditingBucket(null);
      if (selectedDocument) await loadBuckets(selectedDocument, false);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存知识内容失败');
    } finally {
      setContentSaving(false);
    }
  }

  return (
    <div className="knowledge-page knowledge-manage-page">
      <div className="knowledge-hero">
        <div>
          <Typography.Title level={3}>业务资料库</Typography.Title>
          <Typography.Text type="secondary">管理员工可引用的业务资料，查看文档卡片、知识结构、知识桶和证据片段。</Typography.Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refresh()} loading={loading}>刷新</Button>
          <Button onClick={() => void openImportKnowledgeBases()}>向其他员工学习资料</Button>
          <Button type="primary" icon={<FileAddOutlined />} onClick={() => navigate('/enterprise/knowledge/new')}>
            新增业务资料
          </Button>
        </Space>
      </div>

      <Row className="knowledge-structure-row" gutter={[18, 18]} align="stretch">
        <Col xs={24} xl={8}>
          <Card
            className="knowledge-card knowledge-card-solid knowledge-library-card"
            title="业务资料"
            extra={<DatabaseOutlined />}
          >
            <div className="knowledge-management-toolbar">
              <Input.Search
                allowClear
                value={documentSearch}
                onChange={(event) => setDocumentSearch(event.target.value)}
                placeholder="搜索业务资料、状态或版本"
              />
            </div>
            {visibleKnowledgeBases.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无业务资料" />
            ) : filteredKnowledgeBases.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的业务资料" />
            ) : (
              <div className="knowledge-base-grid">
                {filteredKnowledgeBases.map((item) => (
                  <div
                    className={`knowledge-base-card ${item.id === selectedKnowledgeBase?.id ? 'is-active' : ''}`}
                    key={item.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => selectKnowledgeBase(item.id)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        selectKnowledgeBase(item.id);
                      }
                    }}
                  >
                    <div className="knowledge-base-card-head">
                      <div>
                        <Typography.Text strong>{item.name}</Typography.Text>
                        <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                          {item.description || '未填写描述'}
                        </Typography.Paragraph>
                      </div>
                      <span
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                      >
                        <Dropdown
                          trigger={['click']}
                          menu={{
                            items: [
                              { key: 'edit', icon: <EditOutlined />, label: '详情' },
                              { key: 'versions', icon: <HistoryOutlined />, label: '版本管理' },
                              !isOverallAgent ? { key: 'sync', label: '同步组织版' } : null,
                              !isOverallAgent ? { key: 'promote', label: '沉淀到组织' } : null,
                              item.status === 'archived'
                                ? { key: 'publish', icon: <PlayCircleOutlined />, label: '上线' }
                                : { key: 'archive', icon: <PauseCircleOutlined />, label: '下线' },
                              {
                                key: 'delete',
                                icon: <DeleteOutlined />,
                                label: isOverallAgent ? '删除' : '从当前员工移除',
                                danger: true,
                              },
                            ].filter(Boolean),
                            onClick: ({ key }) => {
                              if (key === 'edit') openEditKnowledgeBase(item);
                              if (key === 'versions') void openKnowledgeBaseVersions(item);
                              if (key === 'sync') void syncKnowledgeBaseFromOverall(item);
                              if (key === 'promote') void promoteKnowledgeBaseToOverall(item);
                              if (key === 'publish') void setKnowledgeBaseStatus(item, true);
                              if (key === 'archive') void setKnowledgeBaseStatus(item, false);
                              if (key === 'delete') deleteKnowledgeBase(item);
                            },
                          }}
                        >
                          <Button type="text" size="small" icon={<MoreOutlined />} />
                        </Dropdown>
                      </span>
                    </div>
                    <Space size={6} wrap>
                      {statusTag(item.status)}
                      {item.version && <Tag>v{item.version}</Tag>}
                      {item.branch_sync_state && <Tag color={item.branch_sync_state === 'diverged' ? 'gold' : 'green'}>
                        {item.branch_sync_state === 'diverged' ? '分支修改' : '已同步'}
                      </Tag>}
                      <Tag>{item.document_count} 文档</Tag>
                      <Tag>{item.bucket_count} 桶</Tag>
                      <Tag>{item.chunk_count} 片段</Tag>
                    </Space>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Col>
        <Col xs={24} xl={16}>
          <div className="knowledge-structure-card-frame">
            <Card className="knowledge-card knowledge-card-solid knowledge-structure-card" title="知识结构">
              {!selectedDocument ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择业务资料后查看文档卡片、章节、知识桶和证据片段" />
              ) : (
                <PageIndexOverview
                  document={selectedDocument}
                  knowledgeBase={selectedKnowledgeBase}
                  buckets={buckets}
                  onEditDocument={openEditDocument}
                  onEditBucket={openBucketEditor}
                />
              )}
            </Card>
          </div>
        </Col>
      </Row>

      <Card className="knowledge-card knowledge-card-solid knowledge-card-compact" title="渐进检索调试">
        <Space className="knowledge-debug-stack" direction="vertical" size={14} style={{ width: '100%' }}>
          <Input.Search
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            onSearch={() => void runKnowledgeSearch()}
            loading={searchLoading}
            placeholder="输入知识问题"
            enterButton="检索"
          />
          <KnowledgeSearchDebug result={searchResult} loading={searchLoading} />
        </Space>
      </Card>

      <Modal
        open={importOpen}
        title="向其他员工学习业务资料"
        width={720}
        okText="学习"
        cancelText="取消"
        confirmLoading={importLoading}
        onOk={() => void submitImportKnowledgeBases()}
        onCancel={() => setImportOpen(false)}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Select
            value={importSourceAgentId || undefined}
            placeholder="选择来源员工"
            onChange={(value) => {
              setImportSourceAgentId(value);
              void loadImportSourceKnowledgeBases(value);
            }}
            options={agents
              .filter((item) => item.id !== agentId)
              .map((item) => ({
                value: item.id,
                label: `${item.name}${item.is_overall ? '（组织资源库）' : ''}`,
              }))}
            style={{ width: '100%' }}
          />
          <Select
            mode="multiple"
            value={importSelectedKnowledgeBaseIds}
            placeholder="选择一个或多个业务资料"
            onChange={setImportSelectedKnowledgeBaseIds}
            options={importSourceKnowledgeBases.map((item) => ({
              value: item.id,
              label: `${item.name} · ${item.version || '1.0.0'} · ${item.status}`,
            }))}
            optionFilterProp="label"
            style={{ width: '100%' }}
          />
          <Typography.Text type="secondary">
            学习会复制来源员工中选中业务资料的员工版本；目标为组织资源库时，会将员工版本沉淀为组织资料新版本。
          </Typography.Text>
        </Space>
      </Modal>
      <Modal
        open={Boolean(editingKnowledgeBase)}
        title="业务资料详情"
        okText="保存"
        cancelText="取消"
        onOk={() => void saveKnowledgeBase()}
        onCancel={() => setEditingKnowledgeBase(null)}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Input
            value={knowledgeBaseDraft.name}
            onChange={(event) => setKnowledgeBaseDraft((prev) => ({ ...prev, name: event.target.value }))}
            placeholder="业务资料名称"
          />
          <Input.TextArea
            rows={4}
            value={knowledgeBaseDraft.description}
            onChange={(event) => setKnowledgeBaseDraft((prev) => ({ ...prev, description: event.target.value }))}
            placeholder="业务资料描述"
          />
          <Select
            value={knowledgeBaseDraft.status}
            onChange={(value) => setKnowledgeBaseDraft((prev) => ({ ...prev, status: value }))}
            options={[
              { value: 'active', label: '上线' },
              { value: 'archived', label: '下线' },
            ]}
          />
        </Space>
      </Modal>
      <Modal
        open={Boolean(versionKnowledgeBase)}
        title={versionKnowledgeBase ? `版本管理：${versionKnowledgeBase.name}` : '版本管理'}
        width={840}
        footer={<Button onClick={() => setVersionKnowledgeBase(null)}>关闭</Button>}
        onCancel={() => setVersionKnowledgeBase(null)}
      >
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={knowledgeBaseVersions}
          columns={[
            { title: '版本', dataIndex: 'version' },
            { title: '名称', dataIndex: 'name' },
            { title: '状态', dataIndex: 'status', render: (value) => statusTag(String(value)) },
            { title: 'Head', dataIndex: 'is_head', render: (value) => (value ? <Tag color="green">当前</Tag> : null) },
            { title: '更新时间', dataIndex: 'updated_at', render: (value) => String(value).slice(0, 10) },
            {
              title: '操作',
              width: 96,
              render: (_value, row) =>
                !isOverallAgent && !row.is_head ? (
                  <Button size="small" onClick={() => void rollbackKnowledgeBaseVersion(row)}>
                    回滚
                  </Button>
                ) : null,
            },
          ]}
        />
      </Modal>
      <Modal
        open={Boolean(editingDocument)}
        title="编辑文档"
        okText="保存"
        cancelText="取消"
        onOk={() => void saveDocument()}
        onCancel={() => setEditingDocument(null)}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Input
            value={documentDraft.title}
            onChange={(event) => setDocumentDraft((prev) => ({ ...prev, title: event.target.value }))}
            placeholder="文档标题"
          />
          <Select
            value={documentDraft.status}
            onChange={(value) => setDocumentDraft((prev) => ({ ...prev, status: value }))}
            options={[
              { value: 'ready', label: '可用' },
              { value: 'processing', label: '处理中' },
              { value: 'failed', label: '失败' },
              { value: 'archived', label: '下线' },
            ]}
          />
        </Space>
      </Modal>
      <Modal
        className="knowledge-editor-modal"
        open={Boolean(editingBucket)}
        title="编辑知识桶与片段"
        width={920}
        okText="保存"
        cancelText="取消"
        confirmLoading={contentSaving}
        onOk={() => void saveBucketAndChunks()}
        onCancel={() => setEditingBucket(null)}
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Input
            value={bucketDraft.title}
            onChange={(event) => setBucketDraft((prev) => ({ ...prev, title: event.target.value }))}
            placeholder="知识桶标题"
          />
          <Input.TextArea
            rows={4}
            value={bucketDraft.summary}
            onChange={(event) => setBucketDraft((prev) => ({ ...prev, summary: event.target.value }))}
            placeholder="知识桶摘要"
          />
          <div className="knowledge-chunk-editor-list">
            {bucketChunks.map((chunk) => (
              <div className="knowledge-chunk-editor" key={chunk.id}>
                <div className="knowledge-chunk-editor-head">
                  <Typography.Text strong>片段 {chunk.chunk_index + 1}</Typography.Text>
                  <Tag>{chunk.source_ref || 'chunk'}</Tag>
                </div>
                <Input.TextArea
                  rows={2}
                  value={chunkDrafts[chunk.id]?.summary || ''}
                  onChange={(event) =>
                    setChunkDrafts((prev) => ({
                      ...prev,
                      [chunk.id]: { ...(prev[chunk.id] || { content: chunk.content, summary: '' }), summary: event.target.value },
                    }))
                  }
                  placeholder="片段摘要"
                />
                <Input.TextArea
                  rows={6}
                  value={chunkDrafts[chunk.id]?.content || ''}
                  onChange={(event) =>
                    setChunkDrafts((prev) => ({
                      ...prev,
                      [chunk.id]: { ...(prev[chunk.id] || { content: '', summary: chunk.summary || '' }), content: event.target.value },
                    }))
                  }
                  placeholder="片段内容"
                />
              </div>
            ))}
          </div>
        </Space>
      </Modal>
    </div>
  );
}

export function KnowledgeAddPage() {
  const navigate = useNavigate();
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [jobs, setJobs] = useState<Record<string, KnowledgeIngestJobRead>>({});
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [checkedDiscoveryJobIds, setCheckedDiscoveryJobIds] = useState<string[]>([]);
  const [pendingDiscoveries, setPendingDiscoveries] = useState<KnowledgeDiscoveryRead[]>([]);
  const [discoveryModalOpen, setDiscoveryModalOpen] = useState(false);
  const activeJobs = useMemo(
    () => Object.values(jobs).filter((job) => ['queued', 'running'].includes(job.status)),
    [jobs],
  );
  const visibleKnowledgeBases = useMemo(
    () => knowledgeBases.filter((item) => !isEmptyDefaultKnowledgeBase(item)),
    [knowledgeBases],
  );

  useEffect(() => {
    void refreshKnowledgeBases();
  }, [agentId]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setAgentId((event as CustomEvent<{ agentId?: string }>).detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    if (activeJobs.length === 0) return;
    const timer = window.setInterval(() => {
      activeJobs.forEach((job) => {
        void api
          .get<KnowledgeIngestJobRead>(`/api/enterprise/knowledge/jobs/${job.id}?tenant_id=${TENANT_ID}`)
          .then((next) => setJobs((prev) => ({ ...prev, [next.id]: next })))
          .catch(() => undefined);
      });
    }, 1400);
    return () => window.clearInterval(timer);
  }, [activeJobs]);

  useEffect(() => {
    Object.values(jobs)
      .filter((job) => job.status === 'completed' && !checkedDiscoveryJobIds.includes(job.id))
      .forEach((job) => {
        void loadDiscoveriesForJob(job);
      });
  }, [jobs, checkedDiscoveryJobIds, agentId]);

  async function refreshKnowledgeBases() {
    try {
      const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const rows = await api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${suffix}`);
      setKnowledgeBases(rows);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载知识库失败');
    }
  }

  async function uploadFile(file: File) {
    try {
      const contentBase64 = await fileToBase64(file);
      const suffix = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
      const job = await api.post<KnowledgeIngestJobRead>(`/api/enterprise/knowledge/documents${suffix}`, {
        tenant_id: TENANT_ID,
        filename: file.name,
        title: file.name.replace(/\.[^.]+$/, ''),
        content_base64: contentBase64,
      });
      setJobs((prev) => ({ ...prev, [job.id]: job }));
      await refreshKnowledgeBases();
      message.success('已创建知识库和入库任务');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '上传失败');
    }
  }

  async function loadDiscoveriesForJob(job: KnowledgeIngestJobRead) {
    setCheckedDiscoveryJobIds((prev) => (prev.includes(job.id) ? prev : [...prev, job.id]));
    try {
      const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const rows = await api.get<KnowledgeDiscoveryRead[]>(`/api/enterprise/knowledge/discoveries?tenant_id=${TENANT_ID}${suffix}`);
      const next = rows.filter(
        (item) =>
          item.status === 'pending' &&
          item.suggestion_type !== 'warning' &&
          item.knowledge_base_id === job.knowledge_base_id &&
          (!job.document_id || item.document_id === job.document_id),
      );
      if (next.length === 0) return;
      setPendingDiscoveries((current) => {
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...next.filter((item) => !seen.has(item.id))];
      });
      setDiscoveryModalOpen(true);
    } catch (error) {
      message.warning(error instanceof Error ? error.message : '加载知识发现建议失败');
    }
  }

  async function confirmDiscovery(item: KnowledgeDiscoveryRead) {
    try {
      await api.post(`/api/enterprise/knowledge/discoveries/${item.id}/confirm?tenant_id=${TENANT_ID}`);
      message.success('已确认建议');
      setPendingDiscoveries((current) => current.filter((entry) => entry.id !== item.id));
      await refreshKnowledgeBases();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '确认失败');
    }
  }

  async function rejectDiscovery(item: KnowledgeDiscoveryRead) {
    try {
      await api.post(`/api/enterprise/knowledge/discoveries/${item.id}/reject?tenant_id=${TENANT_ID}`);
      message.success('已拒绝建议');
      setPendingDiscoveries((current) => current.filter((entry) => entry.id !== item.id));
    } catch (error) {
      message.error(error instanceof Error ? error.message : '拒绝失败');
    }
  }

  return (
    <div className="knowledge-page knowledge-add-page knowledge-floating-subpage">
      <div className="knowledge-floating-shell">
        <div className="knowledge-floating-head">
          <div>
            <Typography.Text className="section-kicker">业务资料库 / 悬浮新增</Typography.Text>
            <Typography.Title level={3}>新增业务资料</Typography.Title>
            <Typography.Text type="secondary">上传业务文档后，系统会自动解析、分桶、切片，并生成证据片段与自发现建议。</Typography.Text>
          </div>
          <Button icon={<RightOutlined />} onClick={() => navigate('/enterprise/knowledge')}>返回资料库</Button>
        </div>

        <Card className="knowledge-card knowledge-upload-card">
          <div className="knowledge-upload-controls">
            <div>
              <Typography.Text strong>上传文档即创建业务资料</Typography.Text>
              <Typography.Text type="secondary">一个文件对应一份独立业务资料；回到资料库后可查看文档卡片、知识结构、知识桶和证据片段。</Typography.Text>
            </div>
            <Button onClick={() => navigate('/enterprise/knowledge')}>管理已有业务资料</Button>
          </div>
        {visibleKnowledgeBases.length > 0 && (
          <div className="knowledge-base-target-strip">
            {visibleKnowledgeBases.map((item) => (
              <div
                key={item.id}
                className="knowledge-base-target"
              >
                <span>{item.name}</span>
                <small>
                  {item.document_count} 文档 / {item.bucket_count} 桶 / {item.chunk_count} 片段
                </small>
              </div>
            ))}
          </div>
        )}
        <Dragger
          multiple
          showUploadList={false}
          beforeUpload={(file) => {
            void uploadFile(file);
            return false;
          }}
          accept=".doc,.docx,.txt,.md,.markdown,.html,.htm,.pdf"
        >
          <div className="knowledge-upload-inner">
            <InboxOutlined />
            <div>
              <strong>拖拽文档到这里，或点击选择文件</strong>
              <span>支持 doc/docx/txt/md/html/pdf；旧版 doc 会提示转换为 docx。</span>
            </div>
          </div>
        </Dragger>
        </Card>

        <Card className="knowledge-card knowledge-card-solid" title="入库任务">
          {Object.values(jobs).length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="上传后这里会显示解析和分桶进度" />
          ) : (
            <div className="knowledge-jobs">
              {Object.values(jobs).map((job) => (
                <KnowledgeJobCard job={job} key={job.id} />
              ))}
            </div>
          )}
        </Card>
      </div>

      <Modal
        open={discoveryModalOpen && pendingDiscoveries.length > 0}
        title="发现可新增资源"
        footer={null}
        width={820}
        className="knowledge-discovery-modal"
        onCancel={() => setDiscoveryModalOpen(false)}
      >
        <DiscoveryColumn
          title="可确认建议"
          description="模型从本次上传的知识中发现了技能或工具草案，确认后才会写入系统。"
          items={pendingDiscoveries}
          onConfirm={confirmDiscovery}
          onReject={rejectDiscovery}
        />
      </Modal>
    </div>
  );
}

function KnowledgeJobCard({ job }: { job: KnowledgeIngestJobRead }) {
  const steps = ingestSteps(job);
  const stageLabel = stringFromMetadata(job.metadata.stage_label) || stageLabelFallback(job.stage);
  const stageDetail = stringFromMetadata(job.metadata.stage_detail);
  return (
    <div className="knowledge-job">
      <div className="knowledge-job-head">
        <div>
          <Typography.Text strong>{job.filename}</Typography.Text>
          <Typography.Text type="secondary"> · {stageLabel}</Typography.Text>
        </div>
        {statusTag(job.status)}
      </div>
      <SmoothProgress job={job} />
      <div className="knowledge-stage-track">
        {steps.map((step) => (
          <div className={`knowledge-stage-step is-${step.status}`} key={step.key}>
            <span />
            <small>{step.label}</small>
          </div>
        ))}
      </div>
      {stageDetail && <Typography.Text className="knowledge-job-detail">{stageDetail}</Typography.Text>}
      {job.error && <Typography.Text type="danger">{job.error}</Typography.Text>}
    </div>
  );
}

function SmoothProgress({ job }: { job: KnowledgeIngestJobRead }) {
  const target = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
  const [displayProgress, setDisplayProgress] = useState(target);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setDisplayProgress((current) => {
        if (current === target) return current;
        const diff = target - current;
        const step = Math.max(1, Math.ceil(Math.abs(diff) / 14));
        return current + Math.sign(diff) * Math.min(Math.abs(diff), step);
      });
    }, 80);
    return () => window.clearInterval(timer);
  }, [target]);

  return (
    <Progress
      percent={displayProgress}
      status={job.status === 'failed' ? 'exception' : undefined}
      strokeColor={job.status === 'failed' ? undefined : { '0%': '#0f7f74', '100%': '#16a34a' }}
    />
  );
}

function ingestSteps(job: KnowledgeIngestJobRead): IngestStepView[] {
  const raw = job.metadata.ingest_steps;
  if (Array.isArray(raw)) {
    return raw.map((item, index) => {
      const record = item && typeof item === 'object' ? (item as Record<string, unknown>) : {};
      const status = record.status === 'running' || record.status === 'done' ? record.status : 'pending';
      return {
        key: String(record.key || `step_${index}`),
        label: String(record.label || DEFAULT_INGEST_STEPS[index]?.label || `阶段 ${index + 1}`),
        progress: Number(record.progress || 0),
        status,
      };
    });
  }
  const currentProgress = job.progress || 0;
  return DEFAULT_INGEST_STEPS.map((step) => ({
    ...step,
    status:
      job.stage === step.key
        ? 'running'
        : step.progress < currentProgress || job.stage === 'done'
        ? 'done'
        : 'pending',
  }));
}

function stageLabelFallback(stage: string): string {
  return DEFAULT_INGEST_STEPS.find((item) => item.key === stage)?.label || stage || '处理中';
}

function stringFromMetadata(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

type KnowledgeDetailView = 'document' | 'sections' | 'buckets' | 'evidence';
type KnowledgeContentView = Exclude<KnowledgeDetailView, 'document'>;
const STRUCTURE_PREVIEW_LIMIT = 18;
const BUCKET_PREVIEW_LIMIT = 18;
const EVIDENCE_PREVIEW_LIMIT = 30;

function PageIndexOverview({
  document,
  knowledgeBase,
  buckets,
  onEditDocument,
  onEditBucket,
}: {
  document: KnowledgeDocumentRead;
  knowledgeBase: KnowledgeBaseRead | null;
  buckets: KnowledgeBucketRead[];
  onEditDocument: (document: KnowledgeDocumentRead) => void;
  onEditBucket: (bucket: KnowledgeBucketRead) => void | Promise<void>;
}) {
  const [detailView, setDetailView] = useState<KnowledgeDetailView | null>(null);
  const [activeContentView, setActiveContentView] = useState<KnowledgeContentView>('sections');
  const metadata = document.metadata || {};
  const documentCard = isRecord(metadata.document_card) ? metadata.document_card : {};
  const sectionTreeAll = Array.isArray(metadata.section_tree) ? metadata.section_tree.filter(isRecord) : [];
  const chunkStats = isRecord(metadata.chunk_stats) ? metadata.chunk_stats : {};
  const bucketQuality = Array.isArray(metadata.bucket_quality) ? metadata.bucket_quality.filter(isRecord) : [];
  const qualityByBucketId = new Map(
    bucketQuality.map((quality) => [String(quality.bucket_id || quality.bucket_key || quality.title || ''), quality]),
  );
  const sectionCount = Number(documentCard.section_count || sectionTreeAll.length || 0);
  const chunkCount = Number(chunkStats.total_chunks || document.chunk_count || 0);
  const previewSections = sectionTreeAll.slice(0, STRUCTURE_PREVIEW_LIMIT);
  const previewBuckets = buckets.slice(0, BUCKET_PREVIEW_LIMIT);
  const evidenceBuckets = buckets.filter((bucket) => bucket.chunk_count > 0);
  const previewEvidence = previewEvidenceItems(buckets, chunkCount, EVIDENCE_PREVIEW_LIMIT);
  const documentTitle = String(documentCard.title || document.title || knowledgeBase?.name || document.filename);
  const documentSummary = String(documentCard.summary || '暂无文档摘要');
  const overviewContent: Record<
    KnowledgeContentView,
    {
      title: string;
      description: string;
      count: number;
      emptyText: string;
      items: Array<{ key: string; title: string; summary: string }>;
    }
  > = {
    sections: {
      title: '知识结构',
      description: '按章节和自然段建立的可展开导航。',
      count: sectionCount,
      emptyText: '暂无章节结构',
      items: previewSections.map((section, index) => ({
        key: sectionKey(section, index),
        title: sectionTitle(section, index),
        summary: sectionSummary(section) || '暂无摘要',
      })),
    },
    evidence: {
      title: '证据片段',
      description: '最终回复可引用的最小证据单元。',
      count: chunkCount,
      emptyText: '暂无证据片段',
      items: previewEvidence,
    },
    buckets: {
      title: '知识桶',
      description: '跨章节聚合出的主题索引，用于快速定位知识区域。',
      count: buckets.length,
      emptyText: '暂无知识桶',
      items: previewBuckets.map((bucket) => ({
        key: bucket.id,
        title: bucket.title || bucket.bucket_key || '未命名知识桶',
        summary: bucket.summary || '暂无摘要',
      })),
    },
  };
  const activeContent = overviewContent[activeContentView];

  return (
    <div className="knowledge-pageindex">
      <div className="knowledge-pageindex-card">
        <div className="knowledge-document-card-body">
          <Typography.Text type="secondary">文档卡片</Typography.Text>
          <Typography.Title level={5}>{documentTitle}</Typography.Title>
          <Typography.Paragraph ellipsis={{ rows: 3 }}>{documentSummary}</Typography.Paragraph>
        </div>
        <div className="knowledge-pageindex-actions">
          <Button size="small" icon={<EditOutlined />} onClick={() => setDetailView('document')}>
            详情
          </Button>
        </div>
        <div className="knowledge-document-meta">
          <button type="button" className="knowledge-stat-pill" onClick={() => setDetailView('document')}>
            <span>格式</span>
            <strong>{document.file_type || 'unknown'}</strong>
          </button>
          <button
            type="button"
            className={`knowledge-stat-pill ${activeContentView === 'sections' ? 'is-active' : ''}`}
            aria-pressed={activeContentView === 'sections'}
            onClick={() => setActiveContentView('sections')}
          >
            <span>章节</span>
            <strong>{sectionCount}</strong>
          </button>
          <button
            type="button"
            className={`knowledge-stat-pill ${activeContentView === 'evidence' ? 'is-active' : ''}`}
            aria-pressed={activeContentView === 'evidence'}
            onClick={() => setActiveContentView('evidence')}
          >
            <span>证据片段</span>
            <strong>{chunkCount}</strong>
          </button>
          <button
            type="button"
            className={`knowledge-stat-pill ${activeContentView === 'buckets' ? 'is-active' : ''}`}
            aria-pressed={activeContentView === 'buckets'}
            onClick={() => setActiveContentView('buckets')}
          >
            <span>知识桶</span>
            <strong>{buckets.length}</strong>
          </button>
        </div>
      </div>

      <div className="knowledge-overview-panel">
        <div className="knowledge-overview-panel-head">
          <span>
            <strong>{activeContent.title}</strong>
            <small>{activeContent.description}</small>
          </span>
          <Space size={8}>
            <Tag>{activeContent.count}</Tag>
            <Button size="small" type="link" onClick={() => setDetailView(activeContentView)}>
              查看全部
            </Button>
          </Space>
        </div>
        <div className="knowledge-mini-list">
          {activeContent.items.length === 0 ? (
            <span className="knowledge-empty-note">{activeContent.emptyText}</span>
          ) : (
            activeContent.items.map((entry) => (
              <span className="knowledge-mini-item" key={`${activeContentView}-${entry.key}`}>
                <strong>{entry.title}</strong>
                <small>{entry.summary}</small>
              </span>
            ))
          )}
        </div>
      </div>

      <Modal
        open={Boolean(detailView)}
        title={knowledgeDetailTitle(detailView)}
        footer={null}
        width={920}
        className="knowledge-detail-modal"
        onCancel={() => setDetailView(null)}
      >
        {detailView === 'document' && (
          <div className="knowledge-detail-stack">
            <div className="knowledge-detail-header">
              <div>
                <Typography.Text type="secondary">文档卡片</Typography.Text>
                <Typography.Title level={4}>{documentTitle}</Typography.Title>
                <Typography.Paragraph>{documentSummary}</Typography.Paragraph>
              </div>
              <Button icon={<EditOutlined />} onClick={() => onEditDocument(document)}>
                修改
              </Button>
            </div>
            <div className="knowledge-evidence-stat is-inline">
              <strong>{document.file_type || 'unknown'}</strong>
              <span>文件格式</span>
            </div>
            <div className="knowledge-document-meta">
              <button type="button" className="knowledge-stat-pill" onClick={() => setDetailView('sections')}>
                <span>章节</span>
                <strong>{sectionCount}</strong>
              </button>
              <button type="button" className="knowledge-stat-pill" onClick={() => setDetailView('buckets')}>
                <span>知识桶</span>
                <strong>{buckets.length}</strong>
              </button>
              <button type="button" className="knowledge-stat-pill" onClick={() => setDetailView('evidence')}>
                <span>证据片段</span>
                <strong>{chunkCount}</strong>
              </button>
            </div>
          </div>
        )}

        {detailView === 'sections' && (
          <div className="knowledge-section-tree">
            {sectionTreeAll.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无章节结构" />
            ) : (
              <Collapse
                className="knowledge-section-collapse"
                ghost
                items={sectionTreeAll.map((section, index) => ({
                  key: sectionKey(section, index),
                  label: (
                    <span className="knowledge-section-label" style={{ paddingLeft: Math.max(0, sectionLevel(section) - 1) * 14 }}>
                      {sectionTitle(section, index)}
                    </span>
                  ),
                  children: (
                    <div className="knowledge-section-detail">
                      <Typography.Paragraph>{sectionSummary(section) || '暂无摘要'}</Typography.Paragraph>
                      <Space size={6} wrap>
                        <Tag>层级 {sectionLevel(section)}</Tag>
                        {section.path ? <Tag>{String(section.path)}</Tag> : null}
                      </Space>
                    </div>
                  ),
                }))}
              />
            )}
          </div>
        )}

        {detailView === 'buckets' && (
          <div className="knowledge-quality-list">
            {buckets.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无知识桶" />
            ) : (
              buckets.map((bucket, index) => {
                const quality =
                  qualityByBucketId.get(bucket.id) ||
                  qualityByBucketId.get(bucket.bucket_key) ||
                  qualityByBucketId.get(bucket.title) ||
                  {};
                const qualityInfo = isRecord(quality.quality) ? quality.quality : {};
                const warnings = Array.isArray(qualityInfo.warnings) ? qualityInfo.warnings : [];
                return (
                  <div className="knowledge-detail-bucket" key={bucket.id}>
                    <div className="knowledge-quality-item-head">
                      <div>
                        <strong>{bucket.title || `知识桶 ${index + 1}`}</strong>
                        <span>{bucket.bucket_key}</span>
                      </div>
                      <Space size={6}>
                        {bucketStatusTag(bucket)}
                        <Button size="small" icon={<EditOutlined />} onClick={() => void onEditBucket(bucket)}>
                          编辑
                        </Button>
                      </Space>
                    </div>
                    <Typography.Paragraph>{bucket.summary}</Typography.Paragraph>
                    <Space size={6} wrap>
                      <Tag color={qualityInfo.status === 'warning' ? 'gold' : 'green'}>
                        {qualityInfo.status === 'warning' ? '待补充' : '达标'}
                      </Tag>
                      <Tag>{bucketSourceSections(bucket).length} 章节</Tag>
                      <Tag>{bucket.chunk_count} 证据片段</Tag>
                      {warnings.slice(0, 2).map((warning) => (
                        <Tag color="gold" key={String(warning)}>
                          {String(warning)}
                        </Tag>
                      ))}
                    </Space>
                    <KnowledgeBucketLinks bucket={bucket} />
                  </div>
                );
              })
            )}
          </div>
        )}

        {detailView === 'evidence' && (
          <div className="knowledge-evidence-summary">
            <div className="knowledge-evidence-stat">
              <strong>{chunkCount}</strong>
              <span>证据片段</span>
              <small>按完整段落和句子边界切分，只有可读内容才在详情中展示。</small>
            </div>
            <div className="knowledge-evidence-bucket-map">
              {evidenceBuckets.length === 0 ? (
                chunkCount > 0 ? (
                  <div className="knowledge-evidence-map-item">
                    <Typography.Text strong>已入库证据片段</Typography.Text>
                    <Typography.Text type="secondary">
                      共 {chunkCount} 个证据片段，当前暂无可展示的桶级代表片段。
                    </Typography.Text>
                  </div>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无证据片段映射" />
                )
              ) : (
                evidenceBuckets.map((bucket) => {
                  return (
                    <div className="knowledge-evidence-map-item" key={bucket.id}>
                      <Typography.Text strong>{bucket.title}</Typography.Text>
                      <KnowledgeBucketLinks bucket={bucket} evidenceOnly />
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

function KnowledgeBucketLinks({ bucket, evidenceOnly = false }: { bucket: KnowledgeBucketRead; evidenceOnly?: boolean }) {
  const sourceSections = bucketSourceSections(bucket);
  const representativeChunks = bucketRepresentativeChunks(bucket);
  return (
    <div className="knowledge-bucket-link-grid">
      {!evidenceOnly && (
        <>
          <Typography.Text type="secondary">覆盖章节</Typography.Text>
          <div>
            {sourceSections.length === 0 ? (
              <Tag>暂无来源章节</Tag>
            ) : (
              sourceSections.map((section) => <Tag key={String(section)}>{String(section)}</Tag>)
            )}
          </div>
        </>
      )}
      <Typography.Text type="secondary">{evidenceOnly ? '证据片段' : '代表片段'}</Typography.Text>
      <div className="knowledge-evidence-token-list">
        {representativeChunks.length === 0 ? (
          bucket.chunk_count > 0 ? <Tag>{bucket.chunk_count} 个证据片段</Tag> : <Tag>暂无可读代表片段</Tag>
        ) : (
          representativeChunks.map((chunkId) => <Tag key={String(chunkId)}>{String(chunkId)}</Tag>)
        )}
      </div>
    </div>
  );
}

function knowledgeDetailTitle(view: KnowledgeDetailView | null) {
  if (view === 'document') return '文档详情';
  if (view === 'sections') return '知识结构';
  if (view === 'buckets') return '知识桶';
  if (view === 'evidence') return '证据片段';
  return '知识详情';
}

function sectionKey(section: Record<string, unknown>, index: number) {
  return String(section.section_id || section.path || section.title || `section-${index}`);
}

function sectionTitle(section: Record<string, unknown>, index: number) {
  return String(section.path || section.title || `章节 ${index + 1}`);
}

function sectionSummary(section: Record<string, unknown>) {
  return String(section.summary || section.preview || '');
}

function sectionLevel(section: Record<string, unknown>) {
  return Math.max(1, Number(section.level || section.depth || 1));
}

function bucketSourceSections(bucket: KnowledgeBucketRead) {
  const bucketMeta = bucket.metadata || {};
  if (Array.isArray(bucketMeta.section_paths)) return bucketMeta.section_paths;
  if (Array.isArray(bucketMeta.section_ids)) return bucketMeta.section_ids;
  return [];
}

function bucketRepresentativeChunks(bucket: KnowledgeBucketRead) {
  const representativeChunks = Array.isArray(bucket.metadata?.representative_chunk_ids)
    ? bucket.metadata.representative_chunk_ids
    : [];
  return representativeChunks
    .map((chunkId) => String(chunkId || '').trim())
    .filter((chunkId) => chunkId.length > 0 && !/^k?chunk_[a-f0-9]{8,}$/i.test(chunkId))
    .slice(0, 12);
}

function previewRepresentativeChunkIds(buckets: KnowledgeBucketRead[]) {
  const ids: string[] = [];
  buckets.forEach((bucket) => {
    ids.push(...bucketRepresentativeChunks(bucket));
  });
  return Array.from(new Set(ids)).slice(0, 3);
}

function previewEvidenceItems(buckets: KnowledgeBucketRead[], chunkCount: number, limit: number) {
  const bucketItems = buckets
    .filter((bucket) => bucket.chunk_count > 0)
    .slice(0, limit)
    .map((bucket) => {
      const sourceSections = bucketSourceSections(bucket)
        .map((section) => String(section))
        .filter(Boolean)
        .slice(0, 2);
      return {
        key: bucket.id,
        title: bucket.title || bucket.bucket_key || '证据片段',
        summary: sourceSections.length
          ? `${bucket.chunk_count} 个证据片段，覆盖 ${sourceSections.join(' / ')}`
          : `${bucket.chunk_count} 个证据片段，已完成桶级映射。`,
      };
    });
  if (bucketItems.length > 0) return bucketItems;

  const representativeChunkIds = previewRepresentativeChunkIds(buckets);
  if (representativeChunkIds.length > 0) {
    return representativeChunkIds.map((chunkId) => ({
      key: chunkId,
      title: chunkId,
      summary: '代表片段，可在详情中查看来源映射。',
    }));
  }

  if (chunkCount > 0) {
    return [
      {
        key: 'chunk-total',
        title: '已入库证据片段',
        summary: `共 ${chunkCount} 个证据片段，当前暂无可展示的桶级代表片段。`,
      },
    ];
  }

  return [];
}

function KnowledgeSearchDebug({
  result,
  loading,
  compact = false,
}: {
  result: KnowledgeSearchResponse | null;
  loading: boolean;
  compact?: boolean;
}) {
  if (loading) {
    return <Typography.Text type="secondary">正在按文档、知识桶、章节和证据片段逐级检索...</Typography.Text>;
  }
  if (!result) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未运行检索" />;
  }
  return (
    <div className={`knowledge-search-debug${compact ? ' is-compact' : ''}`}>
      <div className="knowledge-route-trace">
        {(result.route_trace || result.trace || []).map((item, index) => (
          <div className="knowledge-route-step" key={`${String(item.phase || 'phase')}-${index}`}>
            <span>{index + 1}</span>
            <div>
              <strong>{routePhaseLabel(String(item.phase || ''))}</strong>
              <small>{String(item.message || '')}</small>
            </div>
          </div>
        ))}
      </div>
      <Collapse
        size="small"
        items={[
          {
            key: 'documents',
            label: `文档 ${result.selected_documents.length}`,
            children: <pre className="knowledge-json">{JSON.stringify(result.selected_documents, null, 2)}</pre>,
          },
          {
            key: 'sections',
            label: `展开章节 ${result.expanded_sections.length}`,
            children: <pre className="knowledge-json">{JSON.stringify(result.expanded_sections, null, 2)}</pre>,
          },
          {
            key: 'evidence',
            label: `证据包 ${result.evidence_pack.length}`,
            children: (
              <div className="knowledge-evidence-list">
                {result.evidence_pack.map((item) => (
                  <div className="knowledge-evidence-item" key={item.chunk_id}>
                    <Typography.Text strong>{item.section_path || item.source_path || item.chunk_id}</Typography.Text>
                    <Typography.Paragraph>{item.excerpt}</Typography.Paragraph>
                    <Typography.Text type="secondary">{item.confidence_reason}</Typography.Text>
                  </div>
                ))}
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}

function DiscoveryColumn({
  title,
  description,
  items,
  readonly = false,
  onConfirm,
  onReject,
}: {
  title: string;
  description: string;
  items: KnowledgeDiscoveryRead[];
  readonly?: boolean;
  onConfirm: (item: KnowledgeDiscoveryRead) => Promise<void>;
  onReject: (item: KnowledgeDiscoveryRead) => Promise<void>;
}) {
  return (
    <div className="knowledge-discovery-column">
      <div className="knowledge-section-heading">
        <div>
          <strong>{title}</strong>
          <span>{description}</span>
        </div>
        <Tag>{items.length}</Tag>
      </div>
      {items.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无内容" />
      ) : (
        <Space direction="vertical" size={12} className="knowledge-discovery-list">
          {items.map((item) => (
            <div className={`knowledge-discovery ${item.suggestion_type}`} key={item.id}>
              <div className="knowledge-discovery-header">
                <Space size={8} wrap>
                  <Typography.Text strong>{item.title}</Typography.Text>
                  <Tag>{typeLabel(item.suggestion_type)}</Tag>
                  {statusTag(item.status)}
                </Space>
                {!readonly && item.status === 'pending' && (
                  <Space size={8}>
                    <Button size="small" shape="circle" icon={<CheckOutlined />} onClick={() => void onConfirm(item)} />
                    <Button size="small" shape="circle" icon={<CloseOutlined />} onClick={() => void onReject(item)} />
                  </Space>
                )}
              </div>
              {item.reason && <Typography.Paragraph type="secondary">{item.reason}</Typography.Paragraph>}
              <Collapse
                ghost
                items={[
                  {
                    key: 'payload',
                    label: '查看详情',
                    children: <pre className="knowledge-json">{JSON.stringify(item.payload, null, 2)}</pre>,
                  },
                ]}
              />
            </div>
          ))}
        </Space>
      )}
    </div>
  );
}

function routePhaseLabel(phase: string) {
  const map: Record<string, string> = {
    document_route: '选择知识库文档',
    document_route_fallback: '文档路由兜底',
    bucket_route: '展开知识桶',
    bucket_route_fallback: '知识桶路由兜底',
    section_expand: '读取章节',
    read_chunks: '读取片段',
    evidence_pack: '整理证据包',
    no_documents: '没有文档',
    no_buckets: '没有知识桶',
  };
  return map[phase] || phase || '检索阶段';
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead) {
  return (
    item.name === '默认知识库' &&
    item.document_count === 0 &&
    item.bucket_count === 0 &&
    item.chunk_count === 0
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function statusTag(status: string) {
  const map: Record<string, { color: string; label: string }> = {
    active: { color: 'green', label: '已上线' },
    published: { color: 'green', label: '已发布' },
    archived: { color: 'default', label: '已下线' },
    draft: { color: 'default', label: '草稿' },
    succeeded: { color: 'green', label: '已完成' },
    ready: { color: 'green', label: '达标' },
    confirmed: { color: 'green', label: '已确认' },
    failed: { color: 'red', label: '失败' },
    pending: { color: 'gold', label: '待处理' },
    running: { color: 'processing', label: '处理中' },
    queued: { color: 'gold', label: '排队中' },
  };
  const item = map[status] || { color: 'gold', label: status };
  return <Tag color={item.color}>{item.label}</Tag>;
}

function bucketStatusTag(bucket: KnowledgeBucketRead) {
  if (bucket.status === 'ready') return <Tag color="green">达标</Tag>;
  return <Tag color="gold">待补足</Tag>;
}

function typeLabel(type: string) {
  if (type === 'skill') return '技能';
  if (type === 'tool') return '工具';
  return '提示';
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.onload = () => {
      const result = String(reader.result || '');
      resolve(result.includes(',') ? result.split(',').pop() || '' : result);
    };
    reader.readAsDataURL(file);
  });
}
