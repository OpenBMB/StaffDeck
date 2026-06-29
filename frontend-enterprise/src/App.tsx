import { Button, ConfigProvider, Input, Layout, Menu, Modal, Radio, Select, Typography, message, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from './api/client';
import {
  clearEnterpriseAuthSession,
  getEnterpriseAuthSession,
  isEmployeeOwnedBy,
  isEnterpriseAdmin,
  isGalleryEmployee,
  setEnterpriseAuthSession,
  type EnterpriseAuthSession,
} from './auth';
import EmployeeAvatar from './components/EmployeeAvatar';
import StaffdeckIcon from './components/StaffdeckIcon';
import {
  employeeBlankMetadata,
  employeeDisplayName,
  employeeProfile,
  isDefaultEmployeeAgent,
  preferredEmployeeAgent,
} from './employee';
import AccountsPage from './pages/AccountsPage';
import AgentsPage from './pages/AgentsPage';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import FeedbackPage from './pages/FeedbackPage';
import GeneralSkillsPage, { GeneralSkillEditPage, GeneralSkillNewPage } from './pages/GeneralSkillsPage';
import KnowledgeManagePage, { KnowledgeAddPage } from './pages/KnowledgePage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import OpenPlatformPage from './pages/OpenPlatformPage';
import SkillsPage from './pages/SkillsPage';
import ScheduledTasksPage, { ScheduledTaskEditPage, ScheduledTaskNewPage } from './pages/ScheduledTasksPage';
import ToolsPage, { ToolEditPage, ToolNewPage, ToolTestPage } from './pages/ToolsPage';
import { ThemeToggleButton, useThemeController, type EffectiveTheme } from './theme';
import type { AgentProfileRead } from './types';
import logoMark from './assets/staffdeck/staffdeck-logo-mark.png';

const { Header, Sider, Content } = Layout;
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const ENTERPRISE_SIDEBAR_STORAGE_KEY = 'ultrarag_enterprise_sidebar_expanded';
const ENTERPRISE_SIDEBAR_COLLAPSED_WIDTH = 72;
const ENTERPRISE_SIDEBAR_EXPANDED_WIDTH = 220;

type AgentCreateMode = 'copy' | 'blank';

type AgentCreateFormState = {
  name: string;
  description: string;
  roleName: string;
  sourceMode: AgentCreateMode;
  copyFromAgentId: string;
};

type LoginFormState = {
  username: string;
  password: string;
};

const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: '',
  description: '',
  roleName: '',
  sourceMode: 'copy',
  copyFromAgentId: '',
};

function Shell({
  effectiveTheme,
  auth,
  onLogout,
}: {
  effectiveTheme: EffectiveTheme;
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [sidebarExpanded, setSidebarExpanded] = useState(() => window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY) === '1');
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] = useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  const isAdmin = isEnterpriseAdmin(auth.user);
  const accountRoleLabel = isAdmin ? '管理员' : '';
  const isDistillRoute = location.pathname === '/enterprise/skills/distill';
  const selected = location.pathname === '/enterprise'
    ? '/enterprise/dashboard'
    : location.pathname.startsWith('/enterprise/platform')
      ? '/enterprise/platform'
      : location.pathname.startsWith('/enterprise/knowledge')
        ? '/enterprise/knowledge'
        : location.pathname.startsWith('/enterprise/general-skills')
          ? '/enterprise/general-skills'
          : location.pathname.startsWith('/enterprise/tools')
            ? '/enterprise/tools'
            : location.pathname.startsWith('/enterprise/scheduled-tasks')
              ? '/enterprise/scheduled-tasks'
              : isDistillRoute
                ? '/enterprise/skills'
                : location.pathname;
  const [lastDistillSearch, setLastDistillSearch] = useState(() => (isDistillRoute ? location.search : ''));
  const distillSearch = isDistillRoute ? location.search : lastDistillSearch;
  const distillSearchParams = useMemo(() => new URLSearchParams(distillSearch), [distillSearch]);

  useEffect(() => {
    if (isDistillRoute) {
      setLastDistillSearch(location.search);
    }
  }, [isDistillRoute, location.search]);

  useEffect(() => {
    loadAgents();
  }, []);

  useEffect(() => {
    const onAgentRefresh = () => {
      void loadAgents();
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-refresh', onAgentRefresh);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-refresh', onAgentRefresh);
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId
        || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)
        || '';
      if (nextAgentId) {
        setSelectedAgentId(nextAgentId);
      }
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  function loadAgents() {
    return api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        setAgents(rows);
        const selectableRows = rows.filter((item) => canUseAgentScope(item));
        setSelectedAgentId((current) => {
          if (current && selectableRows.some((item) => item.id === current)) return current;
          const ownedRows = selectableRows.filter((item) => !item.is_overall && isEmployeeOwnedBy(item, auth.user));
          const next = isAdmin
            ? selectableRows.find((item) => item.is_overall)?.id || preferredEmployeeAgent(selectableRows)?.id || ''
            : preferredEmployeeAgent(ownedRows)?.id
              || preferredEmployeeAgent(selectableRows)?.id
              || '';
          if (next) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            if (next !== current) {
              window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            }
          }
          return next;
        });
      })
      .catch(() => setAgents([]));
  }

  function canUseAgentScope(agent: AgentProfileRead): boolean {
    if (isAdmin) return true;
    if (agent.is_overall) return false;
    return isDefaultEmployeeAgent(agent) || isEmployeeOwnedBy(agent, auth.user) || isGalleryEmployee(agent);
  }

  function changeAgentScope(agentId: string) {
    setSelectedAgentId(agentId);
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId } }));
  }

  function toggleSidebar() {
    setSidebarExpanded((current) => {
      const next = !current;
      window.localStorage.setItem(ENTERPRISE_SIDEBAR_STORAGE_KEY, next ? '1' : '0');
      return next;
    });
  }

  const selectedAgent = agents.find((item) => item.id === selectedAgentId);
  const scopeAgents = agents.filter(canUseAgentScope);
  const sourceAgents = isAdmin ? scopeAgents : scopeAgents.filter((item) => !item.is_overall);
  const isOverallScope = Boolean(selectedAgent?.is_overall);
  const selectedAgentName = employeeDisplayName(selectedAgent);
  const selectedAgentCaption = selectedAgent?.is_overall
    ? '开放广场'
    : employeeProfile(selectedAgent).roleName;
  const sidebarWidth = sidebarExpanded ? ENTERPRISE_SIDEBAR_EXPANDED_WIDTH : ENTERPRISE_SIDEBAR_COLLAPSED_WIDTH;
  const navItems = [
    { key: '/enterprise/platform', icon: <StaffdeckIcon name="globe" />, label: '开放广场' },
    ...(!isOverallScope ? [{ key: '/enterprise/agents', icon: <StaffdeckIcon name="user" />, label: '我的数字员工' }] : []),
    ...(!isOverallScope
      ? [
          {
            key: 'employees',
            type: 'group' as const,
            label: '当前数字员工',
            children: [
              { key: '/enterprise/dashboard', icon: <StaffdeckIcon name="file" />, label: '数字员工档案' },
              { key: '/enterprise/scheduled-tasks', icon: <StaffdeckIcon name="clock" />, label: '定时任务' },
              { key: '/enterprise/memories', icon: <StaffdeckIcon name="database" />, label: '员工记忆' },
              { key: '/enterprise/feedback', icon: <StaffdeckIcon name="chat" />, label: '对话日志' },
            ],
          },
        ]
      : []),
    {
      key: 'employee-capabilities',
      type: 'group' as const,
      label: isOverallScope ? '开放广场' : '数字员工能力',
      children: [
        ...(isOverallScope ? [{ key: '/enterprise/agents', icon: <StaffdeckIcon name="user" />, label: '数字员工广场' }] : []),
        { key: '/enterprise/knowledge', icon: <StaffdeckIcon name="file" />, label: isOverallScope ? '知识库广场' : '知识库' },
        { key: '/enterprise/general-skills', icon: <StaffdeckIcon name="spark" />, label: isOverallScope ? '技能广场' : '技能' },
        { key: '/enterprise/skills', icon: <StaffdeckIcon name="filter" />, label: isOverallScope ? 'SOP 广场' : 'SOP' },
        { key: '/enterprise/tools', icon: <StaffdeckIcon name="tool" />, label: isOverallScope ? '工具广场' : '工具' },
      ],
    },
    ...(isAdmin
      ? [
          {
            key: 'employee-accounts',
            type: 'group' as const,
            label: '系统',
            children: [
              { key: '/enterprise/accounts', icon: <StaffdeckIcon name="user" />, label: '账号管理' },
              { key: '/enterprise/models', icon: <StaffdeckIcon name="model" />, label: '模型' },
            ],
          },
        ]
      : []),
  ];

  function handleMenuClick(key: string) {
    navigate(key);
  }

  function openCreateAgentModal() {
    setAgentForm({
      ...EMPTY_AGENT_FORM,
      copyFromAgentId: selectedAgentId || sourceAgents[0]?.id || '',
    });
    setAgentCreateOpen(true);
  }

  async function saveAgentCreateModal() {
    const name = agentForm.name.trim();
    if (!name) {
      message.error('请填写数字员工姓名');
      return;
    }
    const isBlankOnboarding = agentForm.sourceMode === 'blank';
    const sourceAgent = agentForm.copyFromAgentId
      ? sourceAgents.find((item) => item.id === agentForm.copyFromAgentId)
      : undefined;
    const sourceMetadata = !isBlankOnboarding && sourceAgent?.metadata ? sourceAgent.metadata : {};
    const sourceRoleName = sourceAgent && !sourceAgent.is_overall ? employeeProfile(sourceAgent).roleName : '';
    const roleName = agentForm.roleName.trim()
      || (!isBlankOnboarding ? sourceRoleName : '')
      || '待补充职位';
    const description = agentForm.description.trim()
      || (!isBlankOnboarding ? sourceAgent?.description || String(sourceMetadata.system_prompt_summary || '') : '')
      || '';
    const baseMetadata = {
      ...sourceMetadata,
      system_prompt_summary: description,
      owner_user_id: auth.user.id,
      owner_username: auth.user.username,
      owner_display_name: auth.user.display_name || auth.user.username,
      role_key: '',
      role_name: roleName,
      onboarded_at: new Date().toISOString().slice(0, 10),
      blank_onboarding: isBlankOnboarding,
    };
    const created = await api.post<AgentProfileRead>('/api/enterprise/agents', {
      tenant_id: TENANT_ID,
      name,
      description,
      source_mode: agentForm.sourceMode,
      copy_from_agent_id: agentForm.sourceMode === 'copy' ? agentForm.copyFromAgentId || undefined : undefined,
      metadata: isBlankOnboarding ? employeeBlankMetadata(baseMetadata) : baseMetadata,
    });
    await loadAgents();
    changeAgentScope(created.id);
    setAgentCreateOpen(false);
  }

  return (
    <Layout className={`app-shell ${sidebarExpanded ? 'sidebar-expanded' : 'sidebar-collapsed'}`}>
      <Sider width={sidebarWidth} theme={effectiveTheme} className={`sidebar sd1-sidebar ${sidebarExpanded ? 'is-expanded' : 'is-collapsed'}`}>
        <nav className="sd1-rail" aria-label="企业端导航">
          <button type="button" className="sd1-rail-logo" title="开放广场" onClick={() => navigate('/enterprise/platform')}>
            <img src={logoMark} alt="" />
            <span className="sd1-brand-text">
              <small>Modelbest</small>
              <strong>UltraRAG4</strong>
            </span>
          </button>
          <button type="button" className="sd1-rail-top-icon" title="模型" onClick={() => navigate('/enterprise/models')}>
            <StaffdeckIcon name="grid" />
          </button>

          <div className="sd1-rail-primary">
            <button
              type="button"
              className={`sd1-rail-icon ${selected === '/enterprise/platform' ? 'active' : ''}`}
              title="开放广场平台"
            onClick={() => navigate('/enterprise/platform')}
          >
            <StaffdeckIcon name="desktop" />
            <span className="sd1-rail-menu-text">开放广场平台</span>
          </button>
          <button
            type="button"
              className={`sd1-rail-icon ${selected === '/enterprise/agents' ? 'active' : ''}`}
              title="我的数字员工"
            onClick={() => navigate('/enterprise/agents')}
          >
            <StaffdeckIcon name="user" />
            <span className="sd1-rail-menu-text">我的数字员工</span>
          </button>
        </div>

          <div className="sd1-rail-employee">
            <button
              type="button"
              className={`sd1-rail-agent ${selected === '/enterprise/dashboard' ? 'active' : ''}`}
              title={selectedAgentName}
              onClick={() => navigate('/enterprise/dashboard')}
            >
              <EmployeeAvatar agent={selectedAgent} size={32} />
              <span className="sd1-rail-agent-label">
                <span className="sd1-rail-agent-short">{selectedAgent?.is_overall ? '广场' : employeeProfile(selectedAgent).roleName.slice(0, 2)}</span>
                <span className="sd1-rail-agent-name">{selectedAgent?.is_overall ? '开放广场' : selectedAgentName}</span>
                <span className="sd1-rail-agent-role">{selectedAgent?.is_overall ? '平台' : employeeProfile(selectedAgent).roleName}</span>
              </span>
            </button>
            <div className="sd1-rail-divider" />
            <span className="sd1-rail-label">
              <span className="sd1-rail-label-collapsed">资料</span>
              <span className="sd1-rail-label-expanded">基本资料</span>
            </span>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/dashboard' ? 'active' : ''}`} title="员工档案" onClick={() => navigate('/enterprise/dashboard')}>
              <StaffdeckIcon name="file" />
              <span className="sd1-rail-menu-text">员工档案</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/scheduled-tasks' ? 'active' : ''}`} title="定时任务" onClick={() => navigate('/enterprise/scheduled-tasks')}>
              <StaffdeckIcon name="clock" />
              <span className="sd1-rail-menu-text">定时任务</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/memories' ? 'active' : ''}`} title="记忆" onClick={() => navigate('/enterprise/memories')}>
              <StaffdeckIcon name="history" />
              <span className="sd1-rail-menu-text">记忆</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/feedback' ? 'active' : ''}`} title="对话日志" onClick={() => navigate('/enterprise/feedback')}>
              <StaffdeckIcon name="calendar" />
              <span className="sd1-rail-menu-text">对话日志</span>
            </button>
            <span className="sd1-rail-label">
              <span className="sd1-rail-label-collapsed">能力</span>
              <span className="sd1-rail-label-expanded">员工能力</span>
            </span>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/knowledge' ? 'active' : ''}`} title="知识库" onClick={() => navigate('/enterprise/knowledge')}>
              <StaffdeckIcon name="folder" />
              <span className="sd1-rail-menu-text">知识库</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/general-skills' ? 'active' : ''}`} title="技能" onClick={() => navigate('/enterprise/general-skills')}>
              <StaffdeckIcon name="spark" />
              <span className="sd1-rail-menu-text">技能</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/skills' ? 'active' : ''}`} title="SOP" onClick={() => navigate('/enterprise/skills')}>
              <StaffdeckIcon name="filter" />
              <span className="sd1-rail-menu-text">SOP</span>
            </button>
            <button type="button" className={`sd1-rail-icon ${selected === '/enterprise/tools' ? 'active' : ''}`} title="工具" onClick={() => navigate('/enterprise/tools')}>
              <StaffdeckIcon name="tool" />
              <span className="sd1-rail-menu-text">工具</span>
            </button>
          </div>

          <button type="button" className="sd1-rail-chat" title="聊天端" onClick={() => { window.location.href = '/chat/'; }}>
            <StaffdeckIcon name="chat" />
            <span className="sd1-rail-menu-text">聊天端</span>
          </button>
          <button
            type="button"
            className="sd1-rail-toggle"
            title={sidebarExpanded ? '收起边栏' : '展开边栏'}
            aria-label={sidebarExpanded ? '收起边栏' : '展开边栏'}
            aria-pressed={sidebarExpanded}
            onClick={toggleSidebar}
          >
            {sidebarExpanded ? <StaffdeckIcon name="sidebar-close" /> : <img src={logoMark} alt="" />}
          </button>
        </nav>
      </Sider>
      <Layout>
        <Header className="topbar">
          <div className="topbar-scope">
            <Typography.Text className="topbar-agent-name" strong title={selectedAgentName}>
              {selectedAgentName}
            </Typography.Text>
            <div className="topbar-subtitle" title={selectedAgentCaption}>
              {selectedAgentCaption}
            </div>
          </div>
          <div className="topbar-actions">
            {accountRoleLabel && <span className="account-chip">{accountRoleLabel}</span>}
            <ThemeToggleButton />
            <Button icon={<StaffdeckIcon name="logout" />} onClick={onLogout} aria-label="退出登录" />
          </div>
        </Header>
        <Content className={`content ${selected === '/enterprise/dashboard' ? 'sd1-dashboard-content' : ''}`}>
          <div className={isDistillRoute ? 'persistent-distill active' : 'persistent-distill hidden'}>
            <DistillPage active={isDistillRoute} searchParamsOverride={distillSearchParams} />
          </div>
          {!isDistillRoute && (
            <Routes>
              <Route path="/enterprise" element={<Navigate to="/enterprise/dashboard" replace />} />
              <Route path="/enterprise/platform" element={<OpenPlatformPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/platform/:kind" element={<OpenPlatformPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/dashboard" element={<DashboardPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/agents" element={<AgentsPage currentUser={auth.user} isAdmin={isAdmin} onCreateAgent={openCreateAgentModal} />} />
              <Route path="/enterprise/memories" element={<MemoriesPage />} />
              <Route path="/enterprise/knowledge" element={<KnowledgeManagePage />} />
              <Route path="/enterprise/knowledge/new" element={<KnowledgeAddPage />} />
              <Route path="/enterprise/feedback" element={<FeedbackPage />} />
              <Route path="/enterprise/scheduled-tasks" element={<ScheduledTasksPage />} />
              <Route path="/enterprise/scheduled-tasks/new" element={<ScheduledTaskNewPage />} />
              <Route path="/enterprise/scheduled-tasks/:taskId/edit" element={<ScheduledTaskEditPage />} />
              <Route path="/enterprise/skills" element={<SkillsPage />} />
              <Route path="/enterprise/general-skills" element={<GeneralSkillsPage />} />
              <Route path="/enterprise/general-skills/new" element={<GeneralSkillNewPage />} />
              <Route path="/enterprise/general-skills/:slug/edit" element={<GeneralSkillEditPage />} />
              <Route path="/enterprise/accounts" element={<AccountsPage />} />
              <Route path="/enterprise/models" element={<ModelsPage />} />
              <Route path="/enterprise/tools" element={<ToolsPage />} />
              <Route path="/enterprise/tools/new" element={<ToolNewPage />} />
              <Route path="/enterprise/tools/:toolId/edit" element={<ToolEditPage />} />
              <Route path="/enterprise/tools/:toolId/test" element={<ToolTestPage />} />
              <Route path="/enterprise/persona" element={<Navigate to="/enterprise/dashboard" replace />} />
              <Route path="*" element={<Navigate to="/enterprise/dashboard" replace />} />
            </Routes>
          )}
        </Content>
      </Layout>
      <Modal
        title="新建数字员工"
        open={agentCreateOpen}
        onCancel={() => setAgentCreateOpen(false)}
        onOk={saveAgentCreateModal}
        okText="创建"
        cancelText="取消"
      >
        <div className="agent-editor-form">
          <label>
            创建方式
            <Radio.Group
              className="agent-create-mode"
              value={agentForm.sourceMode}
              onChange={(event) => {
                const sourceMode = event.target.value as AgentCreateFormState['sourceMode'];
                setAgentForm((prev) => ({
                  ...prev,
                  sourceMode,
                  copyFromAgentId: sourceMode === 'blank' ? '' : prev.copyFromAgentId,
                }));
              }}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '从广场复制', value: 'copy' },
                { label: '从空白开始', value: 'blank' },
              ]}
            />
          </label>
          <label>
            职位
            <Input
              value={agentForm.roleName}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, roleName: event.target.value }))}
              placeholder="例如 研发员工、财务员工"
            />
          </label>
          {agentForm.sourceMode === 'copy' && (
            <label>
              复制来源
              <Select
                value={agentForm.copyFromAgentId || undefined}
                placeholder="选择复制来源"
                options={sourceAgents.map((agent) => ({
                  value: agent.id,
                  label: agent.is_overall
                    ? '开放广场'
                    : `${employeeDisplayName(agent)} · ${employeeProfile(agent).roleName}${isGalleryEmployee(agent) ? ' · 广场' : ''}`,
                }))}
                onChange={(value) => setAgentForm((prev) => {
                  const nextSource = sourceAgents.find((item) => item.id === value);
                  return {
                    ...prev,
                    copyFromAgentId: value,
                    roleName: prev.roleName || (nextSource && !nextSource.is_overall ? employeeProfile(nextSource).roleName : ''),
                  };
                })}
              />
            </label>
          )}
          {agentForm.sourceMode === 'blank' && (
            <div className="agent-definition-note">从空白开始创建，不继承任何已有配置。</div>
          )}
          <label>
            数字员工姓名
            <Input value={agentForm.name} onChange={(event) => setAgentForm((prev) => ({ ...prev, name: event.target.value }))} />
          </label>
          <label>
            岗位描述
            <Input.TextArea
              rows={3}
              value={agentForm.description}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, description: event.target.value }))}
              placeholder="概括这个数字员工的岗位边界、服务风格和执行重点"
            />
          </label>
        </div>
      </Modal>
    </Layout>
  );
}

function EnterpriseLogin({
  onLogin,
}: {
  onLogin: (session: EnterpriseAuthSession) => void;
}) {
  const [form, setForm] = useState<LoginFormState>({ username: '', password: '' });
  const [loading, setLoading] = useState(false);

  async function login() {
    const username = form.username.trim();
    const password = form.password.trim();
    if (!username || !password) {
      message.error('请填写账号和密码');
      return;
    }
    setLoading(true);
    try {
      const session = await api.post<EnterpriseAuthSession>('/api/auth/login', {
        tenant_id: TENANT_ID,
        username,
        password,
      });
      setEnterpriseAuthSession(session);
      onLogin(session);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '登录失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="enterprise-login-page">
      <section className="enterprise-login-card">
        <span className="brand-mark">SD</span>
        <div>
          <Typography.Text className="brand-title">Modelbest</Typography.Text>
          <Typography.Title level={2}>UltraRAG4 数字员工运营台</Typography.Title>
        </div>
        <div className="enterprise-login-form">
          <label>
            账号
            <Input
              value={form.username}
              autoComplete="off"
              onChange={(event) => setForm((prev) => ({ ...prev, username: event.target.value }))}
              onPressEnter={login}
            />
          </label>
          <label>
            密码
            <Input.Password
              value={form.password}
              autoComplete="new-password"
              onChange={(event) => setForm((prev) => ({ ...prev, password: event.target.value }))}
              onPressEnter={login}
            />
          </label>
          <Button type="primary" size="large" loading={loading} onClick={login}>
            登录
          </Button>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const { effectiveTheme } = useThemeController();
  const isDark = effectiveTheme === 'dark';
  const [auth, setAuth] = useState<EnterpriseAuthSession | null>(() => getEnterpriseAuthSession());

  function logout() {
    clearEnterpriseAuthSession();
    setAuth(null);
  }

  return (
    <ConfigProvider
      locale={zhCN}
      button={{ autoInsertSpace: false }}
      theme={{
        algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: isDark ? '#f4f4f5' : '#111111',
          borderRadius: 6,
          colorBgBase: isDark ? '#111111' : '#f7f8fa',
          colorBgContainer: isDark ? '#111827' : '#ffffff',
          colorBgElevated: isDark ? '#1e293b' : '#ffffff',
          colorFillSecondary: isDark ? 'rgba(255, 255, 255, 0.1)' : '#f3f4f6',
          colorText: isDark ? '#f8fafc' : '#111111',
          colorTextSecondary: isDark ? '#a1a1aa' : '#6b7280',
          colorBorder: isDark ? 'rgba(255, 255, 255, 0.14)' : '#e5e7eb',
          fontFamily:
            '"Inter", "Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif',
        },
      }}
    >
      <BrowserRouter>
        {auth ? (
          <Shell effectiveTheme={effectiveTheme} auth={auth} onLogout={logout} />
        ) : (
          <EnterpriseLogin onLogin={setAuth} />
        )}
      </BrowserRouter>
    </ConfigProvider>
  );
}
