import { EyeOutlined, MessageOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Card, Descriptions, Drawer, Empty, Segmented, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useMemo, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import type {
  EnterpriseChatSessionRead,
  EnterpriseSessionDetailRead,
  FeedbackAnalysisRead,
  FeedbackMessageRead,
  FeedbackSessionDetailRead,
  FeedbackSessionRead,
  FeedbackSummaryRead,
} from '../types';

type LogFilter = 'all' | 'up' | 'down' | 'unrated' | 'ability' | 'tool' | 'knowledge' | 'sop';

type ConversationLogRow = EnterpriseChatSessionRead & {
  downFeedback?: FeedbackSessionRead;
  upFeedback?: FeedbackSessionRead;
};

type ConversationDetail = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
  events: EnterpriseSessionDetailRead['events'];
};

const FILTER_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '好评', value: 'up' },
  { label: '差评', value: 'down' },
  { label: '未评价', value: 'unrated' },
  { label: '能力缺口', value: 'ability' },
  { label: '工具缺口', value: 'tool' },
  { label: '资料缺口', value: 'knowledge' },
  { label: 'SOP 缺口', value: 'sop' },
];

export default function FeedbackPage() {
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [downRows, setDownRows] = useState<FeedbackSessionRead[]>([]);
  const [upRows, setUpRows] = useState<FeedbackSessionRead[]>([]);
  const [summary, setSummary] = useState<FeedbackSummaryRead | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [filter, setFilter] = useState<LogFilter>('all');
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reanalyzingId, setReanalyzingId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [sessionResult, downResult, upResult, summaryResult] = await Promise.all([
        api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}`),
        api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=down`),
        api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=up`),
        api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}`),
      ]);
      setSessions(sessionResult);
      setDownRows(downResult);
      setUpRows(upResult);
      setSummary(summaryResult);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '查询对话日志失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const rows = useMemo<ConversationLogRow[]>(() => {
    const downBySession = new Map(downRows.map((item) => [item.session_id, item]));
    const upBySession = new Map(upRows.map((item) => [item.session_id, item]));
    return sessions.map((session) => ({
      ...session,
      downFeedback: downBySession.get(session.id),
      upFeedback: upBySession.get(session.id),
    }));
  }, [downRows, sessions, upRows]);

  const filteredRows = useMemo(() => rows.filter((row) => {
    if (filter === 'all') return true;
    if (filter === 'up') return Boolean(row.upFeedback);
    if (filter === 'down') return Boolean(row.downFeedback);
    if (filter === 'unrated') return !row.upFeedback && !row.downFeedback;
    if (filter === 'ability') return row.downFeedback?.primary_bucket === 'model_issue';
    if (filter === 'tool') return row.downFeedback?.primary_bucket === 'tool_or_system_issue';
    if (filter === 'sop') return row.downFeedback?.primary_bucket === 'skill_issue';
    if (filter === 'knowledge') return row.downFeedback?.primary_bucket === 'unknown';
    return true;
  }), [filter, rows]);

  const openDetail = async (row: ConversationLogRow) => {
    setDetailLoading(true);
    try {
      const sessionDetail = await api.get<EnterpriseSessionDetailRead>(
        `/api/enterprise/sessions/${row.id}?tenant_id=${TENANT_ID}`,
      );
      let feedbackDetail: FeedbackSessionDetailRead | null = null;
      if (row.downFeedback || row.upFeedback) {
        try {
          feedbackDetail = await api.get<FeedbackSessionDetailRead>(
            `/api/enterprise/feedback/sessions/${row.id}?tenant_id=${TENANT_ID}`,
          );
        } catch {
          feedbackDetail = null;
        }
      }
      setDetail({
        session: feedbackDetail?.session || sessionDetail.session,
        messages: feedbackDetail?.messages || sessionDetail.messages,
        feedback: feedbackDetail?.feedback || [],
        events: sessionDetail.events || [],
      });
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载对话详情失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const reloadCurrentDetail = async () => {
    const sessionId = String(detail?.session?.id || detail?.session?.session_id || '');
    if (!sessionId) return;
    const row = rows.find((item) => item.id === sessionId);
    if (row) await openDetail(row);
  };

  const reanalyzeFeedback = async (feedbackId: string) => {
    setReanalyzingId(feedbackId);
    try {
      await api.post(`/api/enterprise/feedback/${feedbackId}/reanalyze?tenant_id=${TENANT_ID}`);
      message.success('已重新提交后台分析');
      await reloadCurrentDetail();
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '重新分析失败');
    } finally {
      setReanalyzingId(null);
    }
  };

  const columns: ColumnsType<ConversationLogRow> = [
    {
      title: '对话任务',
      dataIndex: 'id',
      width: 240,
      ellipsis: true,
      render: (_, row) => row.title || row.summary || row.last_agent_question || row.id,
    },
    {
      title: '接单员工',
      dataIndex: 'agent_id',
      width: 180,
      ellipsis: true,
      render: (value) => value || '-',
    },
    {
      title: '状态',
      width: 150,
      render: (_, row) => (
        <Space size={4} wrap>
          {row.upFeedback && <Tag color="green">好评</Tag>}
          {row.downFeedback && <Tag color="red">差评</Tag>}
          {!row.upFeedback && !row.downFeedback && <Tag>未评价</Tag>}
        </Space>
      ),
    },
    {
      title: '质检归因',
      width: 160,
      render: (_, row) => row.downFeedback
        ? <FeedbackBucketTag label={row.downFeedback.primary_bucket_label} bucket={row.downFeedback.primary_bucket} />
        : <Tag>暂无缺口</Tag>,
    },
    {
      title: '最近内容',
      ellipsis: true,
      render: (_, row) => (
        <span className="muted-cell">
          {row.downFeedback?.latest_message || row.upFeedback?.latest_message || row.summary || row.last_agent_question || '-'}
        </span>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (value) => new Date(value).toLocaleString(),
    },
    {
      title: '操作',
      width: 110,
      fixed: 'right',
      render: (_, row) => (
        <Button icon={<EyeOutlined />} onClick={() => openDetail(row)} loading={detailLoading}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <>
      <div className="page-title">
        <Typography.Title level={3}>对话日志</Typography.Title>
      </div>
      <Card
        className="data-card conversation-log-card"
        title={<><MessageOutlined /> 对话任务与质检复盘</>}
        extra={<Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>刷新</Button>}
      >
        {summary && (
          <div className="feedback-summary-panel">
            <div className="feedback-summary-text">{summary.summary}</div>
            <Space wrap>
              <Tag>对话 {sessions.length}</Tag>
              <Tag>反馈 {summary.total_feedback}</Tag>
              <Tag color="green">好评 {summary.up_count}</Tag>
              <Tag color="red">差评 {summary.down_count}</Tag>
              {summary.bucket_counts.map((item) => (
                <Tag key={item.bucket} color={bucketColor(item.bucket)}>
                  {item.label} {item.count}
                </Tag>
              ))}
            </Space>
          </div>
        )}
        <Segmented
          className="conversation-log-filter"
          value={filter}
          options={FILTER_OPTIONS}
          onChange={(value) => setFilter(value as LogFilter)}
        />
        <Table
          rowKey="id"
          columns={columns}
          dataSource={filteredRows}
          loading={loading}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: <Empty description="暂无对话日志" /> }}
          scroll={{ x: 1120 }}
        />
      </Card>
      <Drawer
        title="对话日志详情"
        open={Boolean(detail)}
        width={920}
        onClose={() => setDetail(null)}
        destroyOnClose
      >
        {detail ? (
          <div className="feedback-detail">
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="任务 ID">{String(detail.session.session_id || detail.session.id || '-')}</Descriptions.Item>
              <Descriptions.Item label="接单员工">{String(detail.session.agent_id || '-')}</Descriptions.Item>
              <Descriptions.Item label="用户">{displayUser(detail.session)}</Descriptions.Item>
              <Descriptions.Item label="状态">{String(detail.session.status || '-')}</Descriptions.Item>
              <Descriptions.Item label="反馈">
                <Space wrap>
                  <Tag color="green">好评 {detail.feedback.filter((item) => item.rating === 'up').length}</Tag>
                  <Tag color="red">差评 {detail.feedback.filter((item) => item.rating === 'down').length}</Tag>
                  {detail.feedback
                    .filter((item) => item.rating === 'down')
                    .map((item) => item.analysis as FeedbackAnalysisRead | undefined)
                    .filter(Boolean)
                    .map((analysis, index) => (
                      <FeedbackBucketTag
                        key={`${analysis?.bucket || 'unknown'}_${index}`}
                        label={analysis?.bucket_label}
                        bucket={analysis?.bucket}
                      />
                    ))}
                </Space>
              </Descriptions.Item>
            </Descriptions>
            <div className="feedback-conversation">
              {detail.messages.map((item) => (
                <FeedbackMessage
                  key={item.id}
                  item={item}
                  onReanalyze={reanalyzeFeedback}
                  reanalyzing={Boolean(item.feedback_id && item.feedback_id === reanalyzingId)}
                />
              ))}
            </div>
            {detail.events.length > 0 && (
              <div className="conversation-event-log">
                <Typography.Title level={5}>执行记录</Typography.Title>
                {detail.events.slice(-12).map((event) => (
                  <div key={event.id} className="conversation-event-item">
                    <strong>{event.event_type}</strong>
                    <span>{new Date(event.created_at).toLocaleString()}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : null}
      </Drawer>
    </>
  );
}

function FeedbackMessage({
  item,
  onReanalyze,
  reanalyzing,
}: {
  item: FeedbackMessageRead;
  onReanalyze: (feedbackId: string) => void;
  reanalyzing: boolean;
}) {
  const isUser = item.role === 'user';
  const isAssistant = item.role === 'assistant';
  const analysisFailed = item.feedback_analysis?.status === 'failed';
  return (
    <div className={`feedback-message-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="feedback-message-bubble">
        <div className="feedback-message-meta">
          <span>{isUser ? '用户' : isAssistant ? '员工' : item.role}</span>
          <span>{new Date(item.created_at).toLocaleString()}</span>
          {item.feedback_rating === 'down' && <Tag color="red">差评</Tag>}
          {item.feedback_rating === 'up' && <Tag color="green">好评</Tag>}
          {item.feedback_analysis && (
            analysisFailed
              ? <Tag color="red">分析失败</Tag>
              : <FeedbackBucketTag label={item.feedback_analysis.bucket_label} bucket={item.feedback_analysis.bucket} />
          )}
        </div>
        <Typography.Paragraph className="feedback-message-content">
          {item.content}
        </Typography.Paragraph>
        {item.feedback_analysis && item.feedback_rating === 'down' && (
          <div className="feedback-analysis-box">
            <div>
              <strong>质检状态：</strong>{analysisStatusLabel(item.feedback_analysis.status)}
              {item.feedback_analysis.status !== 'failed' && typeof item.feedback_analysis.confidence === 'number' && (
                <span> · 置信度 {(item.feedback_analysis.confidence * 100).toFixed(0)}%</span>
              )}
            </div>
            {item.feedback_analysis.summary && <div><strong>改进项：</strong>{item.feedback_analysis.summary}</div>}
            {item.feedback_analysis.reason && <div><strong>原因：</strong>{item.feedback_analysis.reason}</div>}
            {item.feedback_analysis.status === 'failed' && item.feedback_id && (
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={reanalyzing}
                onClick={() => onReanalyze(item.feedback_id as string)}
              >
                重新分析
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function displayUser(session: Record<string, unknown>): string {
  return String(session.display_name || session.username || session.user_id || '-');
}

function FeedbackBucketTag({ label, bucket }: { label?: string; bucket?: string }) {
  if (!label && !bucket) return <Tag>待分析</Tag>;
  return <Tag color={bucketColor(bucket)}>{label || bucket}</Tag>;
}

function bucketColor(bucket?: string): string {
  if (bucket === 'model_issue') return 'volcano';
  if (bucket === 'skill_issue') return 'orange';
  if (bucket === 'tool_or_system_issue') return 'purple';
  if (bucket === 'user_random_or_unclear') return 'default';
  if (bucket === 'positive_or_resolved') return 'green';
  if (bucket === 'needs_model_analysis') return 'blue';
  return 'default';
}

function analysisStatusLabel(status?: string): string {
  if (status === 'pending') return '等待分析';
  if (status === 'analyzed') return '已分析';
  if (status === 'failed') return '分析失败';
  if (status === 'needs_model') return '待配置模型';
  return status || '未知';
}
