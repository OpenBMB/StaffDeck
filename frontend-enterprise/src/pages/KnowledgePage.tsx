import {
  CheckOutlined,
  CloseOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  InboxOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Card, Col, Collapse, Progress, Row, Space, Table, Tag, Typography, Upload, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useMemo, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import type {
  KnowledgeBucketRead,
  KnowledgeDiscoveryRead,
  KnowledgeDocumentRead,
  KnowledgeIngestJobRead,
} from '../types';

const { Dragger } = Upload;

export default function KnowledgePage() {
  const [documents, setDocuments] = useState<KnowledgeDocumentRead[]>([]);
  const [discoveries, setDiscoveries] = useState<KnowledgeDiscoveryRead[]>([]);
  const [jobs, setJobs] = useState<Record<string, KnowledgeIngestJobRead>>({});
  const [selectedDocument, setSelectedDocument] = useState<KnowledgeDocumentRead | null>(null);
  const [buckets, setBuckets] = useState<KnowledgeBucketRead[]>([]);
  const [loading, setLoading] = useState(false);
  const activeJobs = useMemo(
    () => Object.values(jobs).filter((job) => ['queued', 'running'].includes(job.status)),
    [jobs],
  );

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (activeJobs.length === 0) return;
    const timer = window.setInterval(() => {
      activeJobs.forEach((job) => {
        void api
          .get<KnowledgeIngestJobRead>(`/api/enterprise/knowledge/jobs/${job.id}?tenant_id=${TENANT_ID}`)
          .then((next) => {
            setJobs((prev) => ({ ...prev, [next.id]: next }));
            if (!['queued', 'running'].includes(next.status)) void refresh();
          })
          .catch(() => undefined);
      });
    }, 1400);
    return () => window.clearInterval(timer);
  }, [activeJobs]);

  async function refresh() {
    setLoading(true);
    try {
      const [docRows, discoveryRows] = await Promise.all([
        api.get<KnowledgeDocumentRead[]>(`/api/enterprise/knowledge/documents?tenant_id=${TENANT_ID}`),
        api.get<KnowledgeDiscoveryRead[]>(`/api/enterprise/knowledge/discoveries?tenant_id=${TENANT_ID}`),
      ]);
      setDocuments(docRows);
      setDiscoveries(discoveryRows);
      if (selectedDocument) {
        const current = docRows.find((item) => item.id === selectedDocument.id) || null;
        setSelectedDocument(current);
        if (current) void loadBuckets(current);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '刷新知识库失败');
    } finally {
      setLoading(false);
    }
  }

  async function uploadFile(file: File) {
    try {
      const contentBase64 = await fileToBase64(file);
      const job = await api.post<KnowledgeIngestJobRead>('/api/enterprise/knowledge/documents', {
        tenant_id: TENANT_ID,
        filename: file.name,
        title: file.name.replace(/\.[^.]+$/, ''),
        content_base64: contentBase64,
      });
      setJobs((prev) => ({ ...prev, [job.id]: job }));
      message.success('已创建知识入库任务');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '上传失败');
    }
  }

  async function loadBuckets(document: KnowledgeDocumentRead) {
    setSelectedDocument(document);
    try {
      const rows = await api.get<KnowledgeBucketRead[]>(
        `/api/enterprise/knowledge/documents/${document.id}/buckets?tenant_id=${TENANT_ID}`,
      );
      setBuckets(rows);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载知识桶失败');
    }
  }

  async function confirmDiscovery(item: KnowledgeDiscoveryRead) {
    try {
      await api.post(`/api/enterprise/knowledge/discoveries/${item.id}/confirm?tenant_id=${TENANT_ID}`);
      message.success('已确认建议');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '确认失败');
    }
  }

  async function rejectDiscovery(item: KnowledgeDiscoveryRead) {
    try {
      await api.post(`/api/enterprise/knowledge/discoveries/${item.id}/reject?tenant_id=${TENANT_ID}`);
      message.success('已拒绝建议');
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '拒绝失败');
    }
  }

  const documentColumns: ColumnsType<KnowledgeDocumentRead> = [
    {
      title: '文档',
      dataIndex: 'title',
      render: (_value, row) => (
        <button type="button" className="link-button" onClick={() => loadBuckets(row)}>
          {row.title || row.filename}
        </button>
      ),
    },
    { title: '格式', dataIndex: 'file_type', width: 96, render: (value) => <Tag>{value}</Tag> },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => statusTag(value) },
    { title: '桶', dataIndex: 'bucket_count', width: 80 },
    { title: '片段', dataIndex: 'chunk_count', width: 80 },
    { title: '更新时间', dataIndex: 'updated_at', width: 150, render: (value) => String(value).slice(0, 10) },
  ];

  return (
    <div className="knowledge-page">
      <div className="page-heading">
        <div>
          <Typography.Title level={3}>知识库</Typography.Title>
          <Typography.Text type="secondary">上传文档，分桶切片，并让模型发现可确认的技能和工具建议。</Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => refresh()} loading={loading}>
          刷新
        </Button>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={15}>
          <Card className="knowledge-card" title="知识输入">
            <Dragger
              multiple
              showUploadList={false}
              beforeUpload={(file) => {
                void uploadFile(file);
                return false;
              }}
              accept=".doc,.docx,.txt,.md,.markdown,.html,.htm,.pdf"
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">拖拽文档到这里，或点击选择文件</p>
              <p className="ant-upload-hint">支持 doc/docx/txt/md/html/pdf；旧版 doc 会提示转换为 docx。</p>
            </Dragger>

            {Object.values(jobs).length > 0 && (
              <div className="knowledge-jobs">
                {Object.values(jobs).map((job) => (
                  <div className="knowledge-job" key={job.id}>
                    <div>
                      <Typography.Text strong>{job.filename}</Typography.Text>
                      <Typography.Text type="secondary"> {job.stage}</Typography.Text>
                    </div>
                    <Progress percent={Math.round(job.progress * 100)} status={job.status === 'failed' ? 'exception' : undefined} />
                    {job.error && <Typography.Text type="danger">{job.error}</Typography.Text>}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card className="knowledge-card" title="文档" extra={<DatabaseOutlined />}>
            <Table
              rowKey="id"
              columns={documentColumns}
              dataSource={documents}
              loading={loading}
              pagination={{ pageSize: 8 }}
            />
          </Card>
        </Col>

        <Col xs={24} lg={9}>
          <Card
            className="knowledge-card knowledge-bucket-card"
            title={selectedDocument ? `知识桶：${selectedDocument.title || selectedDocument.filename}` : '知识桶'}
            extra={<FileSearchOutlined />}
          >
            {selectedDocument ? (
              <Collapse
                items={buckets.map((bucket) => ({
                  key: bucket.id,
                  label: bucket.title,
                  children: (
                    <div className="knowledge-bucket-body">
                      <Typography.Paragraph>{bucket.summary}</Typography.Paragraph>
                      <Tag>{bucket.bucket_key}</Tag>
                      <Tag>{bucket.token_estimate} tokens</Tag>
                    </div>
                  ),
                }))}
              />
            ) : (
              <Typography.Text type="secondary">选择一个文档查看知识桶。</Typography.Text>
            )}
          </Card>

          <Card className="knowledge-card" title="自发现建议">
            <Space direction="vertical" size={12} className="knowledge-discovery-list">
              {discoveries.length === 0 && <Typography.Text type="secondary">暂无建议</Typography.Text>}
              {discoveries.map((item) => (
                <div className={`knowledge-discovery ${item.suggestion_type}`} key={item.id}>
                  <div className="knowledge-discovery-header">
                    <Space>
                      <Typography.Text strong>{item.title}</Typography.Text>
                      <Tag>{typeLabel(item.suggestion_type)}</Tag>
                      {statusTag(item.status)}
                    </Space>
                    {item.status === 'pending' && (
                      <Space>
                        <Button size="small" shape="circle" icon={<CheckOutlined />} onClick={() => confirmDiscovery(item)} />
                        <Button size="small" shape="circle" icon={<CloseOutlined />} onClick={() => rejectDiscovery(item)} />
                      </Space>
                    )}
                  </div>
                  {item.reason && <Typography.Paragraph type="secondary">{item.reason}</Typography.Paragraph>}
                  <details>
                    <summary>查看 payload</summary>
                    <pre className="knowledge-json">{JSON.stringify(item.payload, null, 2)}</pre>
                  </details>
                </div>
              ))}
            </Space>
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function statusTag(status: string) {
  const color = status === 'succeeded' || status === 'ready' || status === 'confirmed' ? 'green' : status === 'failed' ? 'red' : 'gold';
  return <Tag color={color}>{status}</Tag>;
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
