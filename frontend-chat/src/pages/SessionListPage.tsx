import {
  DeleteOutlined,
  EditOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Empty, Input, Modal, Typography, message } from 'antd';
import type { MouseEvent } from 'react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, clearAuthSession, getAuthSession, isAuthError } from '../api/client';
import { employeeDisplayName, employeeProfile } from '../employee';
import { ThemeToggleButton } from '../theme';
import type { AgentProfileRead, ChatSession } from '../types';

function SessionChatIcon() {
  return (
    <svg className="session-chat-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 4.2c-4.7 0-8.1 3.05-8.1 7.25 0 2.32 1.02 4.32 2.75 5.65l-.55 2.65 3.05-1.45c.9.26 1.9.4 2.95.4 4.7 0 8.1-3.05 8.1-7.25S16.7 4.2 12 4.2Z" />
      <path d="M8.7 11.45h.04M12 11.45h.04M15.3 11.45h.04" />
    </svg>
  );
}

export default function SessionListPage() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem('skill_agent_selected_agent') || '');
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [newSessionAgentId, setNewSessionAgentId] = useState('');
  const [renameSession, setRenameSession] = useState<ChatSession | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const navigate = useNavigate();
  const [auth] = useState(() => getAuthSession());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => (
    window.localStorage.getItem('skill_agent_sidebar_collapsed') === 'true'
  ));
  const tenantId = auth?.user.tenant_id || 'tenant_demo';

  const load = () =>
    api
      .get<ChatSession[]>(`/api/chat/sessions?tenant_id=${tenantId}`)
      .then(setSessions)
      .catch((error) => {
        if (isAuthError(error)) {
          clearAuthSession();
          navigate('/login', { replace: true });
          return;
        }
        message.error(error.message);
      });

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/chat/agents?tenant_id=${tenantId}`)
      .then((rows) => {
        const employeeRows = rows.filter((item) => !item.is_overall);
        setAgents(employeeRows);
        setSelectedAgentId((current) => {
          if (current && employeeRows.some((item) => item.id === current)) return current;
          const next = employeeRows[0]?.id || '';
          if (next) window.localStorage.setItem('skill_agent_selected_agent', next);
          return next;
        });
        setNewSessionAgentId((current) => (
          current && employeeRows.some((item) => item.id === current)
            ? current
            : (employeeRows.find((item) => item.id === selectedAgentId)?.id || employeeRows[0]?.id || '')
        ));
      })
      .catch(() => setAgents([]));
  }, [selectedAgentId, tenantId]);

  function openCreateSession() {
    const fallbackAgentId = selectedAgentId && agents.some((agent) => agent.id === selectedAgentId)
      ? selectedAgentId
      : agents[0]?.id || '';
    setNewSessionAgentId(fallbackAgentId);
    setNewSessionOpen(true);
  }

  async function createSession() {
    const agentId = newSessionAgentId || selectedAgentId || agents[0]?.id || '';
    if (!agentId) {
      message.warning('请先选择接单员工');
      return;
    }
    const session = await api.post<ChatSession>('/api/chat/sessions', { tenant_id: tenantId, agent_id: agentId });
    setSelectedAgentId(agentId);
    window.localStorage.setItem('skill_agent_selected_agent', agentId);
    setNewSessionOpen(false);
    navigate(`/chat/${session.id}`);
  }

  function toggleSidebar() {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem('skill_agent_sidebar_collapsed', String(next));
      return next;
    });
  }

  function openRename(event: MouseEvent<HTMLElement>, session: ChatSession) {
    event.stopPropagation();
    setRenameSession(session);
    setRenameTitle(session.title || session.id);
  }

  async function saveRename() {
    if (!renameSession) return;
    const title = renameTitle.trim();
    if (!title) {
      message.warning('请输入任务名称');
      return;
    }
    const updated = await api.put<ChatSession>(`/api/chat/sessions/${renameSession.id}`, {
      tenant_id: tenantId,
      title,
    });
    setSessions((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    setRenameSession(null);
    setRenameTitle('');
    message.success('已重命名');
  }

  function confirmDelete(event: MouseEvent<HTMLElement>, target: ChatSession) {
    event.stopPropagation();
    Modal.confirm({
      title: '删除任务记录',
      content: `确定删除「${target.title || target.id}」吗？此操作会同时删除该任务的消息记录。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await api.delete(`/api/chat/sessions/${target.id}?tenant_id=${tenantId}`);
        setSessions((items) => items.filter((item) => item.id !== target.id));
        message.success('已删除');
      },
    });
  }

  return (
    <div className={`chat-layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="session-pane">
        <div className="sidebar-head">
          <Button
            className="icon-button"
            icon={sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
            onClick={toggleSidebar}
          />
          <div className="brand-block">
            <span className="brand-mark">UR</span>
            <div>
              <div className="brand-title">UltraRAG4</div>
              <div className="brand-subtitle">{auth?.user.display_name || auth?.user.username}</div>
            </div>
          </div>
          <div className="sidebar-actions">
            <Button className="icon-button" icon={<ReloadOutlined />} onClick={load} />
            <Button className="icon-button primary" icon={<PlusOutlined />} onClick={openCreateSession} />
            <Button
              className="icon-button sidebar-logout"
              icon={<LogoutOutlined />}
              onClick={() => {
                clearAuthSession();
                navigate('/login', { replace: true });
              }}
            />
          </div>
        </div>
        <div className="session-section-label">任务记录</div>
        {sessions.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          sessions.map((session) => {
            const sessionTitle = session.title || session.id;
            const sessionSummary = session.summary || session.last_agent_question || '新任务';
            return (
            <div
              key={session.id}
              role="button"
              tabIndex={0}
              className="session-card"
              onClick={() => navigate(`/chat/${session.id}`)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  navigate(`/chat/${session.id}`);
                }
              }}
            >
              <div className="session-card-content">
                <div className="session-meta">
                  <div className="session-title" title={sessionTitle}>
                    <span className="session-title-icon"><SessionChatIcon /></span>
                    <span className="session-title-text">{sessionTitle}</span>
                  </div>
                  <div className="session-summary" title={sessionSummary}>
                    {sessionSummary}
                  </div>
                </div>
                <div className="session-actions">
                  <Button
                    className="session-action"
                    size="small"
                    type="text"
                    icon={<EditOutlined />}
                    aria-label="重命名任务"
                    onClick={(event) => openRename(event, session)}
                  />
                  <Button
                    className="session-action danger"
                    size="small"
                    type="text"
                    icon={<DeleteOutlined />}
                    aria-label="删除任务"
                    onClick={(event) => confirmDelete(event, session)}
                  />
                </div>
              </div>
            </div>
            );
          })
        )}
      </aside>
      <main className="chat-main">
        <div className="chat-header">
          <div>
            <Typography.Text strong>任务派发台</Typography.Text>
            <div className="header-subtitle">选择历史任务或派发新任务</div>
          </div>
          <div className="chat-header-actions">
            <ThemeToggleButton />
          </div>
        </div>
        <div className="chat-messages">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </div>
      </main>
      <Modal
        className="new-session-agent-modal"
        title="选择接单员工"
        open={newSessionOpen}
        okText="创建任务"
        cancelText="取消"
        okButtonProps={{ disabled: !newSessionAgentId }}
        onOk={createSession}
        onCancel={() => setNewSessionOpen(false)}
      >
        <div className="new-session-agent-copy">
          一个任务只绑定一位接单员工。创建后，该任务不会随默认选择变化。
        </div>
        <div className="new-session-agent-list">
          {agents.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用员工" />
          ) : (
            agents.map((agent) => {
              const profile = employeeProfile(agent);
              return (
                <button
                  key={agent.id}
                  type="button"
                  className={`new-session-agent-card ${newSessionAgentId === agent.id ? 'selected' : ''}`}
                  onClick={() => setNewSessionAgentId(agent.id)}
                >
                  <span className={`new-session-agent-logo tone-${profile.avatarTone}`}>{profile.avatarText}</span>
                  <span className="new-session-agent-info">
                    <span className="new-session-agent-name">{employeeDisplayName(agent)}</span>
                    <span className="new-session-agent-desc">{profile.roleName} · {agent.description || '使用该员工的技能、SOP、业务资料和岗位人设'}</span>
                  </span>
                </button>
              );
            })
          )}
        </div>
      </Modal>
      <Modal
        title="重命名任务"
        open={Boolean(renameSession)}
        okText="保存"
        cancelText="取消"
        onOk={saveRename}
        onCancel={() => {
          setRenameSession(null);
          setRenameTitle('');
        }}
      >
        <Input
          autoFocus
          maxLength={80}
          value={renameTitle}
          onChange={(event) => setRenameTitle(event.target.value)}
          onPressEnter={saveRename}
          placeholder="输入任务名称"
        />
      </Modal>
    </div>
  );
}
