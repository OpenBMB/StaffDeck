import { Button, Empty, Input, Select, message } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, clearAuthSession, getAuthSession } from '../api/client';
import EmployeeAvatarMark from '../components/EmployeeAvatarMark';
import StaffdeckIcon from '../components/StaffdeckIcon';
import {
  agentResourceCount,
  employeeDisplayName,
  employeeProfile,
  isEmployeeOwnedBy,
  isGalleryEmployee,
  staffdeckDisplayText,
  visibleChatEmployees,
} from '../employee';
import { ThemeToggleButton } from '../theme';
import type { AgentProfileRead, ChatSession } from '../types';

function tabForGalleryPath(pathname: string): 'all' | 'mine' | 'gallery' {
  if (pathname.includes('/gallery') || pathname.includes('/employees')) return 'all';
  return 'mine';
}

export default function EmployeeGalleryPage() {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem('skill_agent_selected_agent') || '');
  const [employeeTab, setEmployeeTab] = useState<'all' | 'mine' | 'gallery'>(() => tabForGalleryPath(window.location.pathname));
  const [searchText, setSearchText] = useState('');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => (
    window.localStorage.getItem('skill_agent_sidebar_collapsed') === 'true'
  ));
  const [auth] = useState(() => getAuthSession());
  const autoCreateRef = useRef('');
  const navigate = useNavigate();
  const location = useLocation();
  const tenantId = auth?.user.tenant_id || 'tenant_demo';
  const launchParams = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const launchAgentId = launchParams.get('agent_id') || '';
  const shouldAutoCreate = launchParams.get('create') === '1';
  const availableAgents = visibleChatEmployees(agents, auth?.user);
  const personalAgents = availableAgents.filter((agent) => !isGalleryEmployee(agent) || isEmployeeOwnedBy(agent, auth?.user));
  const personalAgentIds = new Set(personalAgents.map((agent) => agent.id));
  const galleryAgents = availableAgents.filter((agent) => isGalleryEmployee(agent) && !personalAgentIds.has(agent.id));
  const tabAgents = employeeTab === 'all' ? availableAgents : employeeTab === 'mine' ? personalAgents : galleryAgents;
  const visibleEmployeeCards = tabAgents.filter((agent) => {
    const query = searchText.trim().toLowerCase();
    if (!query) return true;
    const profile = employeeProfile(agent);
    return [
      employeeDisplayName(agent),
      profile.roleName,
      agent.description || '',
      ...profile.expertiseTags,
    ].join(' ').toLowerCase().includes(query);
  });
  const sidebarEmployeeOptions = [
    { value: 'all', label: `全部员工 · ${availableAgents.length}` },
    { value: 'mine', label: `我的员工 · ${personalAgents.length}` },
    { value: 'gallery', label: `广场员工 · ${galleryAgents.length}` },
  ];
  const sidebarAgents = visibleEmployeeCards.length ? visibleEmployeeCards : availableAgents;

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/chat/agents?tenant_id=${tenantId}`)
      .then((rows) => {
        const employeeRows = visibleChatEmployees(rows, auth?.user);
        setAgents(employeeRows);
        setSelectedAgentId((current) => {
          if (current && employeeRows.some((item) => item.id === current)) return current;
          const next = employeeRows[0]?.id || '';
          if (next) window.localStorage.setItem('skill_agent_selected_agent', next);
          return next;
        });
      })
      .catch(() => setAgents([]));
  }, [auth?.user, tenantId]);

  useEffect(() => {
    setEmployeeTab(tabForGalleryPath(location.pathname));
  }, [location.pathname]);

  useEffect(() => {
    if (!launchAgentId) return;
    if (!availableAgents.some((agent) => agent.id === launchAgentId)) return;
    setSelectedAgentId(launchAgentId);
    window.localStorage.setItem('skill_agent_selected_agent', launchAgentId);
    if (!shouldAutoCreate || autoCreateRef.current === launchAgentId) return;
    autoCreateRef.current = launchAgentId;
    void createSessionForAgent(launchAgentId);
  }, [availableAgents, launchAgentId, shouldAutoCreate]);

  function toggleSidebar() {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem('skill_agent_sidebar_collapsed', String(next));
      return next;
    });
  }

  async function createSessionForAgent(agentId: string) {
    if (!agentId) {
      message.warning('请先选择数字员工');
      return;
    }
    const session = await api.post<ChatSession>('/api/chat/sessions', { tenant_id: tenantId, agent_id: agentId });
    setSelectedAgentId(agentId);
    window.localStorage.setItem('skill_agent_selected_agent', agentId);
    navigate(`/${session.id}`);
  }

  const renderEmployeeCards = (rows: AgentProfileRead[], emptyText: string) => {
    if (!rows.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />;
    }
    return rows.map((agent) => {
      const profile = employeeProfile(agent);
      const sopCount = agentResourceCount(agent, 'skill');
      const skillCount = agentResourceCount(agent, 'general_skill');
      const knowledgeCount = agentResourceCount(agent, 'knowledge_base');
      const updatedAt = agent.updated_at ? new Date(agent.updated_at) : null;
      const updatedLabel = updatedAt && !Number.isNaN(updatedAt.getTime())
        ? `${updatedAt.getMonth() + 1}/${updatedAt.getDate()} 更新`
        : isGalleryEmployee(agent) ? '广场' : '我的';
      return (
        <button
          key={agent.id}
          type="button"
          className={`employee-gallery-page-card ${selectedAgentId === agent.id ? 'selected' : ''}`}
          onClick={() => void createSessionForAgent(agent.id)}
        >
          <EmployeeAvatarMark profile={profile} className="employee-gallery-page-avatar" />
          <span className="employee-gallery-page-copy">
            <span className="employee-gallery-page-card-head">
              <span>
                <span className="employee-gallery-page-name">{employeeDisplayName(agent)}</span>
                <span className="employee-gallery-page-role">{profile.roleName}</span>
              </span>
              <span className="employee-gallery-page-action">
                <StaffdeckIcon name="chat" />
              </span>
            </span>
            <span className="employee-gallery-page-desc">{staffdeckDisplayText(agent.description || '暂无描述')}</span>
            <span className="employee-gallery-page-stats">
              <span><strong>{knowledgeCount}</strong><em>资料</em></span>
              <span><strong>{skillCount}</strong><em>技能</em></span>
              <span><strong>{sopCount}</strong><em>SOP</em></span>
            </span>
            <span className="employee-gallery-page-tags">
              <span>在线</span>
              <span>{isGalleryEmployee(agent) ? '广场' : '我的'}</span>
              <span>{updatedLabel}</span>
            </span>
          </span>
        </button>
      );
    });
  };

  return (
    <div className={`chat-layout employee-gallery-layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="session-pane gallery-sidebar-pane">
        <div className="sidebar-head">
          <Button
            className="icon-button"
            icon={<StaffdeckIcon name={sidebarCollapsed ? 'sidebar-open' : 'sidebar-close'} />}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
            onClick={toggleSidebar}
          />
          <div className="brand-block">
            <span className="brand-mark">SD</span>
            <div>
              <div className="brand-title">Modelbest</div>
              <div className="brand-subtitle">UltraRAG4</div>
            </div>
          </div>
          <div className="sidebar-actions">
            <Button
              className="icon-button sidebar-logout"
              icon={<StaffdeckIcon name="logout" />}
              onClick={() => {
                clearAuthSession();
                navigate('/login', { replace: true });
              }}
            />
          </div>
        </div>
        {!sidebarCollapsed && (
          <div className="sidebar-workspace-panel">
            <button type="button" className="sidebar-gallery-entry active" onClick={() => navigate('/employees')}>
              <span className="sidebar-gallery-entry-icon"><StaffdeckIcon name="globe" /></span>
              <span className="sidebar-gallery-entry-copy">
                <strong>数字员工广场</strong>
                <span>选择数字员工</span>
              </span>
              <StaffdeckIcon name="arrow" />
            </button>
            <div className="session-filter-bar">
              <span className="session-filter-label">员工会话</span>
              <Select
                size="small"
                className="session-filter-select"
                value={employeeTab}
                options={sidebarEmployeeOptions}
                onChange={(value) => setEmployeeTab(value as 'all' | 'mine' | 'gallery')}
              />
            </div>
          </div>
        )}
        <div className="session-list-scroll gallery-agent-list">
          <div className="session-section-label">{sidebarCollapsed ? '会话' : '员工工作'}</div>
          {sidebarAgents.map((agent) => {
              const profile = employeeProfile(agent);
              const online = agent.status === 'active';
              return (
                <div
                  key={agent.id}
                  role="button"
                  tabIndex={0}
                  className={`session-card gallery-agent-card ${selectedAgentId === agent.id ? 'active' : ''}`}
                  onClick={() => {
                    setSelectedAgentId(agent.id);
                    window.localStorage.setItem('skill_agent_selected_agent', agent.id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelectedAgentId(agent.id);
                      window.localStorage.setItem('skill_agent_selected_agent', agent.id);
                    }
                  }}
                >
                  <div className="session-card-content">
                    <span className="session-title-icon session-title-avatar">
                      <EmployeeAvatarMark
                        profile={profile}
                        fallback={employeeDisplayName(agent).slice(0, 1) || '员'}
                        className="session-agent-avatar"
                      />
                    </span>
                    <div className="session-meta">
                      <div className="session-title" title={employeeDisplayName(agent)}>
                        <span className="session-title-text">{employeeDisplayName(agent)}</span>
                      </div>
                      <div className="session-summary" title={profile.roleName}>
                        {profile.roleName}
                        <span className={`gallery-agent-status ${online ? 'online' : ''}`}>{online ? '在线客服' : '离线'}</span>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
        </div>
        <button type="button" className="sidebar-bottom-link" onClick={() => { window.location.href = '/enterprise/dashboard'; }}>
          <StaffdeckIcon name="grid" />
          <span>管理端</span>
          <StaffdeckIcon name="arrow" />
        </button>
      </aside>
      <main className="chat-main employee-gallery-page-main">
        <div className="chat-header">
          <Input
            className="staffdeck-search-input"
            prefix={<StaffdeckIcon name="search" />}
            placeholder="搜索"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
          <div className="chat-header-actions">
            <ThemeToggleButton />
            <Button
              className="icon-button"
              icon={<StaffdeckIcon name="logout" />}
              aria-label="退出聊天"
              onClick={() => { window.location.href = '/enterprise/dashboard'; }}
            />
          </div>
        </div>
        <div className="employee-gallery-page">
          <section className="employee-gallery-tabs" aria-label="数字员工分类">
            {[
              { key: 'all', label: '所有员工', count: availableAgents.length },
              { key: 'mine', label: '我的数字员工', count: personalAgents.length },
              { key: 'gallery', label: '数字员工广场', count: galleryAgents.length },
            ].map((item) => (
              <button
                key={item.key}
                type="button"
                className={employeeTab === item.key ? 'active' : ''}
                onClick={() => setEmployeeTab(item.key as 'all' | 'mine' | 'gallery')}
              >
                {item.label}
                <span>{item.count}</span>
              </button>
            ))}
          </section>

          <section className="employee-gallery-page-section sd1-flat-section">
            <div className="employee-gallery-page-grid">
              {renderEmployeeCards(visibleEmployeeCards, searchText ? '没有匹配的数字员工' : '暂无数字员工')}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
