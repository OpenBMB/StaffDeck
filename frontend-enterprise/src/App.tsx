import { ConfigProvider, Input, Layout, Modal, Radio, Select, message, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from './api/client';
import {
  clearEnterpriseAuthSession,
  getEnterpriseAuthSession,
  isEmployeeOwnedBy,
  isEnterpriseAdmin,
  isGalleryEmployee,
  type EnterpriseAuthSession,
} from './auth';
import AppSidebar from './components/AppSidebar';
import StaffdeckIcon from './components/StaffdeckIcon';
import { SidebarProvider } from '@/components/ui/sidebar';
import { EnterpriseRoute } from './enums/routes';
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
import LoginPage from './pages/LoginPage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import OpenPlatformPage from './pages/OpenPlatformPage';
import SkillsPage from './pages/SkillsPage';
import ScheduledTasksPage, { ScheduledTaskEditPage, ScheduledTaskNewPage } from './pages/ScheduledTasksPage';
import ToolsPage, { ToolEditPage, ToolNewPage, ToolTestPage } from './pages/ToolsPage';
import { useIsMobile } from './hooks/use-mobile';
import { Toaster } from '@/components/ui/sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { AgentProfileRead } from './types';

const { Header, Sider, Content } = Layout;
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const ENTERPRISE_SIDEBAR_STORAGE_KEY = 'ultrarag_enterprise_sidebar_expanded';
type AgentCreateMode = 'copy' | 'blank';

type AgentCreateFormState = {
  name: string;
  description: string;
  roleName: string;
  sourceMode: AgentCreateMode;
  copyFromAgentId: string;
};

const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: '',
  description: '',
  roleName: '',
  sourceMode: 'copy',
  copyFromAgentId: '',
};

function Shell({
  auth,
  onLogout,
}: {
  auth: EnterpriseAuthSession;
  onLogout: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    const stored = window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY);
    return stored == null ? true : stored === '1';
  });
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] = useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  const isMobile = useIsMobile();
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
  const isAgentRosterRoute = location.pathname.startsWith('/enterprise/agents');
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

  // Auto-collapse the sidebar on small screens; restore the saved preference on desktop.
  useEffect(() => {
    if (isMobile) {
      setSidebarExpanded(false);
    } else {
      const stored = window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY);
      setSidebarExpanded(stored == null ? true : stored === '1');
    }
  }, [isMobile]);

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
      setSelectedAgentId(nextAgentId);
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

  function handleSidebarOpenChange(open: boolean) {
    setSidebarExpanded(open);
    window.localStorage.setItem(ENTERPRISE_SIDEBAR_STORAGE_KEY, open ? '1' : '0');
  }

  const selectedAgent = agents.find((item) => item.id === selectedAgentId);
  const sidebarAgent = selectedAgent;
  const scopeAgents = agents.filter(canUseAgentScope);
  const sourceAgents = isAdmin ? scopeAgents : scopeAgents.filter((item) => !item.is_overall);
  const selectedAgentName = selectedAgent ? employeeDisplayName(selectedAgent) : '未选择';
  const selectedAgentCaption = selectedAgent
    ? selectedAgent.is_overall
      ? '开放广场'
      : employeeProfile(selectedAgent).roleName
    : '-';
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
    <SidebarProvider
      open={sidebarExpanded}
      onOpenChange={handleSidebarOpenChange}
      style={{ '--sidebar-width': '220px', '--sidebar-width-icon': '72px' } as CSSProperties}
      className={`app-shell ${sidebarExpanded ? 'sidebar-expanded' : 'sidebar-collapsed'} ${isAgentRosterRoute ? 'is-agent-roster' : ''}`}
    >
      <AppSidebar
        selected={selected}
        onNavigate={navigate}
        isAdmin={isAdmin}
        sidebarAgent={sidebarAgent}
        scopeAgents={scopeAgents}
        selectedAgentId={selectedAgentId}
        onSelectAgent={(agentId) => {
          if (agentId !== selectedAgentId) changeAgentScope(agentId);
          navigate(EnterpriseRoute.Dashboard);
        }}
        onOpenChat={() => {
          window.location.href = '/chat/';
        }}
      />
      <Layout className="min-w-0">
        <Content className={`content ${selected === '/enterprise/dashboard' ? 'sd1-dashboard-content' : ''} ${selected !== '/enterprise/dashboard' && !isDistillRoute ? 'sd1-management-content' : ''}`}>
          <div className={isDistillRoute ? 'persistent-distill active' : 'persistent-distill hidden'}>
            <DistillPage active={isDistillRoute} searchParamsOverride={distillSearchParams} />
          </div>
          {!isDistillRoute && (
            <Routes>
              <Route path="/enterprise" element={<Navigate to="/enterprise/dashboard" replace />} />
              <Route path="/enterprise/platform" element={<OpenPlatformPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/platform/:kind" element={<OpenPlatformPage currentUser={auth.user} isAdmin={isAdmin} />} />
              <Route path="/enterprise/dashboard" element={<DashboardPage currentUser={auth.user} isAdmin={isAdmin} onLogout={onLogout} />} />
              <Route path="/enterprise/agents" element={<AgentsPage currentUser={auth.user} isAdmin={isAdmin} onCreateAgent={openCreateAgentModal} onLogout={onLogout} />} />
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
              <Route path="/enterprise/accounts" element={isAdmin ? <AccountsPage /> : <Navigate to={EnterpriseRoute.Dashboard} replace />} />
              <Route path="/enterprise/models" element={isAdmin ? <ModelsPage /> : <Navigate to={EnterpriseRoute.Dashboard} replace />} />
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
    </SidebarProvider>
  );
}

export default function App() {
  const isDark = false;
  const [auth, setAuth] = useState<EnterpriseAuthSession | null>(() => getEnterpriseAuthSession());

  // Force light theme app-wide (theme switching has been removed).
  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove('dark');
    root.classList.add('light');
    root.setAttribute('data-theme', 'light');
    root.setAttribute('data-theme-mode', 'light');
    root.style.colorScheme = 'light';
    window.localStorage.setItem('ultrarag_theme_mode', 'light');
  }, []);

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
      <TooltipProvider>
        <BrowserRouter>
          {auth ? (
            <Shell auth={auth} onLogout={logout} />
          ) : (
            <LoginPage onLogin={setAuth} />
          )}
        </BrowserRouter>
        <Toaster richColors closeButton position="top-right" />
      </TooltipProvider>
    </ConfigProvider>
  );
}
