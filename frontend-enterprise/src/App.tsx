import {
  ApiOutlined,
  CommentOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  IdcardOutlined,
  LogoutOutlined,
  PlusOutlined,
  ProfileOutlined,
  RobotOutlined,
  SolutionOutlined,
  TeamOutlined,
  ToolOutlined,
} from '@ant-design/icons';
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
import { EMPLOYEE_TEMPLATES, employeeDisplayName, employeeMetadataFromTemplate, employeeProfile } from './employee';
import AccountsPage from './pages/AccountsPage';
import AgentsPage from './pages/AgentsPage';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import FeedbackPage from './pages/FeedbackPage';
import GeneralSkillsPage, { GeneralSkillEditPage, GeneralSkillNewPage } from './pages/GeneralSkillsPage';
import KnowledgeManagePage, { KnowledgeAddPage } from './pages/KnowledgePage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import SkillsPage from './pages/SkillsPage';
import ToolsPage, { ToolEditPage, ToolNewPage, ToolTestPage } from './pages/ToolsPage';
import { ThemeToggleButton, useThemeController, type EffectiveTheme } from './theme';
import type { AgentProfileRead } from './types';

const { Header, Sider, Content } = Layout;
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type AgentCreateMode = 'copy' | 'blank';

type AgentCreateFormState = {
  name: string;
  description: string;
  roleKey: string;
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
  roleKey: EMPLOYEE_TEMPLATES[0].key,
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
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] = useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  const isAdmin = isEnterpriseAdmin(auth.user);
  const accountRoleLabel = isAdmin ? '管理员' : '员工账号';
  const isDistillRoute = location.pathname === '/enterprise/skills/distill';
  const selected = location.pathname === '/enterprise'
    ? '/enterprise/dashboard'
    : location.pathname === '/enterprise/platform'
      ? '/enterprise/platform'
      : location.pathname.startsWith('/enterprise/knowledge')
        ? '/enterprise/knowledge'
        : location.pathname.startsWith('/enterprise/general-skills')
          ? '/enterprise/general-skills'
          : location.pathname.startsWith('/enterprise/tools')
            ? '/enterprise/tools'
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
          const next = isAdmin
            ? selectableRows.find((item) => item.is_overall)?.id || selectableRows[0]?.id || ''
            : selectableRows.find((item) => !item.is_overall && isEmployeeOwnedBy(item, auth.user))?.id
              || selectableRows.find((item) => !item.is_overall)?.id
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
    return isEmployeeOwnedBy(agent, auth.user) || isGalleryEmployee(agent);
  }

  function changeAgentScope(agentId: string) {
    setSelectedAgentId(agentId);
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId } }));
  }

  const selectedAgent = agents.find((item) => item.id === selectedAgentId);
  const scopeAgents = agents.filter(canUseAgentScope);
  const sourceAgents = isAdmin ? scopeAgents : scopeAgents.filter((item) => !item.is_overall);
  const isOverallScope = Boolean(selectedAgent?.is_overall);
  const navItems = [
    ...(!isOverallScope ? [{ key: '/enterprise/agents', icon: <TeamOutlined />, label: '员工名册' }] : []),
    ...(!isOverallScope
      ? [
          {
            key: 'employees',
            type: 'group' as const,
            label: '数字员工平台',
            children: [
              { key: '/enterprise/dashboard', icon: <DashboardOutlined />, label: '员工信息' },
              { key: '/enterprise/memories', icon: <DatabaseOutlined />, label: '员工记忆' },
              { key: '/enterprise/feedback', icon: <CommentOutlined />, label: '对话日志' },
            ],
          },
        ]
      : []),
    {
      key: 'employee-capabilities',
      type: 'group' as const,
      label: isOverallScope ? '开放广场平台' : '员工能力',
      children: [
        ...(isOverallScope ? [{ key: '/enterprise/agents', icon: <TeamOutlined />, label: '数字员工广场' }] : []),
        { key: '/enterprise/knowledge', icon: <FileSearchOutlined />, label: isOverallScope ? '业务资料广场' : '业务资料' },
        { key: '/enterprise/general-skills', icon: <SolutionOutlined />, label: isOverallScope ? '通用技能广场' : '已掌握技能' },
        { key: '/enterprise/skills', icon: <ProfileOutlined />, label: isOverallScope ? 'SOP广场' : 'SOP管理' },
        { key: '/enterprise/tools', icon: <ToolOutlined />, label: isOverallScope ? '工具广场' : '工具箱' },
      ],
    },
    ...(isAdmin
      ? [
          {
            key: 'employee-accounts',
            type: 'group' as const,
            label: '员工平台',
            children: [
              { key: '/enterprise/accounts', icon: <IdcardOutlined />, label: '员工账号' },
              { key: '/enterprise/models', icon: <ApiOutlined />, label: '模型配置' },
            ],
          },
        ]
      : []),
  ];

  function handleMenuClick(key: string) {
    if (isAdmin && key === '/enterprise/platform') {
      const overallAgent = agents.find((item) => item.is_overall);
      if (overallAgent && overallAgent.id !== selectedAgentId) {
        changeAgentScope(overallAgent.id);
      }
    }
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
      message.error('请填写员工姓名');
      return;
    }
    const template = EMPLOYEE_TEMPLATES.find((item) => item.key === agentForm.roleKey) || EMPLOYEE_TEMPLATES[0];
    const created = await api.post<AgentProfileRead>('/api/enterprise/agents', {
      tenant_id: TENANT_ID,
      name,
      description: agentForm.description || template.description,
      source_mode: agentForm.sourceMode,
      copy_from_agent_id: agentForm.sourceMode === 'copy' ? agentForm.copyFromAgentId || undefined : undefined,
      metadata: employeeMetadataFromTemplate(agentForm.roleKey, {
        system_prompt_summary: agentForm.description || template.description,
        owner_user_id: auth.user.id,
        owner_username: auth.user.username,
        owner_display_name: auth.user.display_name || auth.user.username,
      }),
    });
    await loadAgents();
    changeAgentScope(created.id);
    setAgentCreateOpen(false);
  }

  return (
    <Layout className="app-shell">
      <Sider width={232} theme={effectiveTheme} className="sidebar">
        <div className="brand">
          <span className="brand-mark">UR</span>
          <div>
            <div className="brand-title">UltraRAG4</div>
            <div className="brand-subtitle">数字员工运营台</div>
          </div>
        </div>
        <Menu
          className="nav-menu"
          mode="inline"
          selectedKeys={[selected]}
          onClick={(item) => handleMenuClick(String(item.key))}
          items={navItems}
        />
        <div className="agent-dock">
          <button
            type="button"
            className="agent-dock-mark"
            title="新员工入职"
            aria-label="新员工入职"
            onClick={openCreateAgentModal}
          >
            <RobotOutlined />
          </button>
          <div className="agent-dock-main">
            <div className="agent-dock-label">当前员工</div>
            <Select
              className="agent-dock-select"
              value={selectedAgentId || undefined}
              placeholder="选择员工"
              popupMatchSelectWidth={260}
              options={scopeAgents.map((agent) => ({
                value: agent.id,
                label: agent.is_overall ? '开放广场平台' : `${employeeDisplayName(agent)} · ${employeeProfile(agent).roleName}`,
              }))}
              onChange={changeAgentScope}
              popupRender={(menu) => (
                <>
                  {menu}
                  <div className="agent-dock-dropdown-footer" onMouseDown={(event) => event.preventDefault()}>
                    <Button type="text" block icon={<PlusOutlined />} onClick={openCreateAgentModal}>
                      新员工入职
                    </Button>
                  </div>
                </>
              )}
            />
          </div>
        </div>
      </Sider>
      <Layout>
        <Header className="topbar">
          <div className="topbar-scope">
            <Typography.Text strong>{employeeDisplayName(selectedAgent)}</Typography.Text>
            <div className="topbar-subtitle">
              {selectedAgent?.is_overall ? '开放广场平台' : `${employeeProfile(selectedAgent).roleName} · ${selectedAgent?.description || '员工工作域'}`}
            </div>
          </div>
          <div className="topbar-actions">
            <span className="account-chip">{accountRoleLabel}</span>
            <ThemeToggleButton />
            <Button icon={<LogoutOutlined />} onClick={onLogout} aria-label="退出登录" />
          </div>
        </Header>
        <Content className="content">
          <div className={isDistillRoute ? 'persistent-distill active' : 'persistent-distill hidden'}>
            <DistillPage active={isDistillRoute} searchParamsOverride={distillSearchParams} />
          </div>
          {!isDistillRoute && (
            <Routes>
              <Route path="/enterprise" element={<Navigate to="/enterprise/dashboard" replace />} />
              <Route path="/enterprise/platform" element={<DashboardPage currentUser={auth.user} isAdmin={isAdmin} forceOverall />} />
              <Route path="/enterprise/dashboard" element={<DashboardPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/agents" element={<AgentsPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/memories" element={<MemoriesPage />} />
              <Route path="/enterprise/knowledge" element={<KnowledgeManagePage />} />
              <Route path="/enterprise/knowledge/new" element={<KnowledgeAddPage />} />
              <Route path="/enterprise/feedback" element={<FeedbackPage />} />
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
        title="新员工入职"
        open={agentCreateOpen}
        onCancel={() => setAgentCreateOpen(false)}
        onOk={saveAgentCreateModal}
        okText="保存"
        cancelText="取消"
      >
        <div className="agent-editor-form">
          <label>
            入职方式
            <Radio.Group
              className="agent-create-mode"
              value={agentForm.sourceMode}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, sourceMode: event.target.value }))}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: isAdmin ? '继承开放广场平台' : '从员工广场学习', value: 'copy' },
                { label: '空白入职', value: 'blank' },
              ]}
            />
          </label>
          <label>
            岗位模板
            <Select
              value={agentForm.roleKey}
              options={EMPLOYEE_TEMPLATES.map((template) => ({
                value: template.key,
                label: `${template.avatarText} · ${template.roleName}`,
              }))}
              onChange={(value) => setAgentForm((prev) => {
                const template = EMPLOYEE_TEMPLATES.find((item) => item.key === value);
                return {
                  ...prev,
                  roleKey: value,
                  description: prev.description || template?.description || '',
                };
              })}
            />
          </label>
          {agentForm.sourceMode === 'copy' && (
            <label>
              学习来源
              <Select
                value={agentForm.copyFromAgentId || undefined}
                placeholder={isAdmin ? '选择开放广场平台或已有员工' : '选择个人员工或员工广场员工'}
                options={sourceAgents.map((agent) => ({
                  value: agent.id,
                  label: agent.is_overall
                    ? '开放广场平台'
                    : `${employeeDisplayName(agent)} · ${employeeProfile(agent).roleName}${isGalleryEmployee(agent) ? ' · 员工广场' : ''}`,
                }))}
                onChange={(value) => setAgentForm((prev) => ({ ...prev, copyFromAgentId: value }))}
              />
            </label>
          )}
          {agentForm.sourceMode === 'blank' && (
            <div className="agent-definition-note">空白入职不会继承业务资料、SOP、技能、岗位人设或模型绑定。</div>
          )}
          <label>
            员工姓名
            <Input value={agentForm.name} onChange={(event) => setAgentForm((prev) => ({ ...prev, name: event.target.value }))} />
          </label>
          <label>
            岗位人设摘要
            <Input.TextArea
              rows={3}
              value={agentForm.description}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, description: event.target.value }))}
              placeholder="概括这个员工的岗位边界、服务风格和执行重点"
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
  const [form, setForm] = useState<LoginFormState>({ username: 'admin', password: 'admin' });
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
        display_name: username === 'admin' ? '管理员' : undefined,
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
        <span className="brand-mark">UR</span>
        <div>
          <Typography.Title level={2}>UltraRAG4 数字员工运营台</Typography.Title>
          <Typography.Paragraph type="secondary">
            使用管理员账号进入开放广场平台和员工平台，员工账号进入自己的员工工作域。
          </Typography.Paragraph>
        </div>
        <div className="enterprise-login-form">
          <label>
            账号
            <Input
              value={form.username}
              onChange={(event) => setForm((prev) => ({ ...prev, username: event.target.value }))}
              onPressEnter={login}
            />
          </label>
          <label>
            密码
            <Input.Password
              value={form.password}
              onChange={(event) => setForm((prev) => ({ ...prev, password: event.target.value }))}
              onPressEnter={login}
            />
          </label>
          <Button type="primary" size="large" loading={loading} onClick={login}>
            登录
          </Button>
        </div>
        <div className="enterprise-login-hint">管理员账号：admin / admin</div>
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
          colorPrimary: isDark ? '#e4b976' : '#04756f',
          borderRadius: 8,
          colorBgBase: isDark ? '#0f172a' : '#fbfaf6',
          colorBgContainer: isDark ? '#111827' : '#ffffff',
          colorBgElevated: isDark ? '#1e293b' : '#ffffff',
          colorFillSecondary: isDark ? 'rgba(148, 163, 184, 0.16)' : '#f5f1eb',
          colorText: isDark ? '#f8fafc' : '#1d1d1b',
          colorTextSecondary: isDark ? '#94a3b8' : '#737373',
          colorBorder: isDark ? 'rgba(148, 163, 184, 0.24)' : '#e7e1d8',
          fontFamily:
            '"Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif',
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
