import { DislikeOutlined, EyeOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Card, Descriptions, Drawer, Empty, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import type { FeedbackMessageRead, FeedbackSessionDetailRead, FeedbackSessionRead } from '../types';

export default function FeedbackPage() {
  const [rows, setRows] = useState<FeedbackSessionRead[]>([]);
  const [detail, setDetail] = useState<FeedbackSessionDetailRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const result = await api.get<FeedbackSessionRead[]>(
        `/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=down`,
      );
      setRows(result);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '查询失败');
    } finally {
      setLoading(false);
    }
  };

  const openDetail = async (row: FeedbackSessionRead) => {
    setDetailLoading(true);
    try {
      const result = await api.get<FeedbackSessionDetailRead>(
        `/api/enterprise/feedback/sessions/${row.session_id}?tenant_id=${TENANT_ID}`,
      );
      setDetail(result);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载详情失败');
    } finally {
      setDetailLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const columns: ColumnsType<FeedbackSessionRead> = [
    {
      title: '会话',
      dataIndex: 'session_id',
      width: 230,
      ellipsis: true,
      render: (value, row) => row.title || value,
    },
    {
      title: '用户',
      width: 180,
      render: (_, row) => row.display_name || row.username || row.user_id || '-',
    },
    { title: '点踩数', dataIndex: 'feedback_count', width: 90 },
    {
      title: '最近点踩回复',
      dataIndex: 'latest_message',
      ellipsis: true,
      render: (value) => <span className="muted-cell">{value || '-'}</span>,
    },
    {
      title: '最近点踩时间',
      dataIndex: 'latest_feedback_at',
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
        <Typography.Title level={3}>负反馈会话</Typography.Title>
      </div>
      <Card
        className="data-card"
        title={<><DislikeOutlined /> 用户点踩汇总</>}
        extra={<Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>}
      >
        <Table
          rowKey="session_id"
          columns={columns}
          dataSource={rows}
          loading={loading}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: <Empty description="暂无点踩会话" /> }}
          scroll={{ x: 1080 }}
        />
      </Card>
      <Drawer
        title="点踩会话详情"
        open={Boolean(detail)}
        width={860}
        onClose={() => setDetail(null)}
        destroyOnClose
      >
        {detail ? (
          <div className="feedback-detail">
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="会话 ID">{String(detail.session.session_id || detail.session.id || '-')}</Descriptions.Item>
              <Descriptions.Item label="用户">{displayUser(detail.session)}</Descriptions.Item>
              <Descriptions.Item label="状态">{String(detail.session.status || '-')}</Descriptions.Item>
              <Descriptions.Item label="点踩数">
                {detail.feedback.filter((item) => item.rating === 'down').length}
              </Descriptions.Item>
            </Descriptions>
            <div className="feedback-conversation">
              {detail.messages.map((item) => (
                <FeedbackMessage key={item.id} item={item} />
              ))}
            </div>
          </div>
        ) : null}
      </Drawer>
    </>
  );
}

function FeedbackMessage({ item }: { item: FeedbackMessageRead }) {
  const isUser = item.role === 'user';
  const isAssistant = item.role === 'assistant';
  return (
    <div className={`feedback-message-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="feedback-message-bubble">
        <div className="feedback-message-meta">
          <span>{isUser ? '用户' : isAssistant ? '助手' : item.role}</span>
          <span>{new Date(item.created_at).toLocaleString()}</span>
          {item.feedback_rating === 'down' && <Tag color="red">点踩</Tag>}
          {item.feedback_rating === 'up' && <Tag color="green">点赞</Tag>}
        </div>
        <Typography.Paragraph className="feedback-message-content">
          {item.content}
        </Typography.Paragraph>
      </div>
    </div>
  );
}

function displayUser(session: Record<string, unknown>): string {
  return String(session.display_name || session.username || session.user_id || '-');
}
