import {
  ApiOutlined,
  CommentOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  MessageOutlined,
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
import { EMPLOYEE_TEMPLATES, employeeDisplayName, employeeMetadataFromTemplate, employeeProfile } from './employee';
import AgentsPage from './pages/AgentsPage';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import FeedbackPage from './pages/FeedbackPage';
import GeneralSkillsPage from './pages/GeneralSkillsPage';
import KnowledgeManagePage, { KnowledgeAddPage } from './pages/KnowledgePage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import SkillsPage from './pages/SkillsPage';
import ToolsPage from './pages/ToolsPage';
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

const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: '',
  description: '',
  roleKey: EMPLOYEE_TEMPLATES[0].key,
  sourceMode: 'copy',
  copyFromAgentId: '',
};

function Shell({ effectiveTheme }: { effectiveTheme: EffectiveTheme }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [agentForm, setAgentForm] = useState<AgentCreateFormState>(EMPTY_AGENT_FORM);
  const selected = location.pathname === '/enterprise'
    ? '/enterprise/dashboard'
    : location.pathname.startsWith('/enterprise/knowledge')
      ? '/enterprise/knowledge'
      : location.pathname;
  const isDistillRoute = location.pathname === '/enterprise/skills/distill';
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

  function loadAgents() {
    return api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        setAgents(rows);
        setSelectedAgentId((current) => {
          if (current && rows.some((item) => item.id === current)) return current;
          const next = rows.find((item) => item.is_overall)?.id || rows[0]?.id || '';
          if (next) window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
          return next;
        });
      })
      .catch(() => setAgents([]));
  }

  function changeAgentScope(agentId: string) {
    setSelectedAgentId(agentId);
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agentId);
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId } }));
  }

  const selectedAgent = agents.find((item) => item.id === selectedAgentId);

  function openCreateAgentModal() {
    setAgentForm({
      ...EMPTY_AGENT_FORM,
      copyFromAgentId: selectedAgentId || agents.find((item) => item.is_overall)?.id || '',
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
          onClick={(item) => navigate(item.key)}
          items={[
            {
              key: 'workspace',
              type: 'group',
              label: '员工运营',
              children: [
                { key: '/enterprise/dashboard', icon: <DashboardOutlined />, label: '看板' },
                { key: '/enterprise/agents', icon: <TeamOutlined />, label: '员工名册' },
                { key: '/enterprise/memories', icon: <DatabaseOutlined />, label: '成长轨迹' },
                { key: '/enterprise/feedback', icon: <CommentOutlined />, label: '对话日志' },
              ],
            },
            {
              key: 'knowledge',
              type: 'group',
              label: '能力建设',
              children: [
                { key: '/enterprise/knowledge', icon: <FileSearchOutlined />, label: '业务资料库' },
                { key: '/enterprise/general-skills', icon: <SolutionOutlined />, label: '已掌握技能' },
                { key: '/enterprise/skills', icon: <ProfileOutlined />, label: 'SOP管理' },
                { key: '/enterprise/skills/distill', icon: <MessageOutlined />, label: 'SOP学习' },
                { key: '/enterprise/tools', icon: <ToolOutlined />, label: '工具箱' },
              ],
            },
            {
              key: 'governance',
              type: 'group',
              label: '治理设置',
              children: [
                { key: '/enterprise/models', icon: <ApiOutlined />, label: '模型配置' },
              ],
            },
          ]}
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
              options={agents.map((agent) => ({
                value: agent.id,
                label: agent.is_overall ? '组织资源库' : `${employeeDisplayName(agent)} · ${employeeProfile(agent).roleName}`,
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
              {selectedAgent?.is_overall ? '组织资源库' : `${employeeProfile(selectedAgent).roleName} · ${selectedAgent?.description || '员工工作域'}`}
            </div>
          </div>
          <div className="topbar-actions">
            <ThemeToggleButton />
          </div>
        </Header>
        <Content className="content">
          <div className={isDistillRoute ? 'persistent-distill active' : 'persistent-distill hidden'}>
            <DistillPage active={isDistillRoute} searchParamsOverride={distillSearchParams} />
          </div>
          {!isDistillRoute && (
            <Routes>
              <Route path="/enterprise" element={<Navigate to="/enterprise/dashboard" replace />} />
              <Route path="/enterprise/dashboard" element={<DashboardPage />} />
              <Route path="/enterprise/agents" element={<AgentsPage />} />
              <Route path="/enterprise/memories" element={<MemoriesPage />} />
              <Route path="/enterprise/knowledge" element={<KnowledgeManagePage />} />
              <Route path="/enterprise/knowledge/new" element={<KnowledgeAddPage />} />
              <Route path="/enterprise/feedback" element={<FeedbackPage />} />
              <Route path="/enterprise/skills" element={<SkillsPage />} />
              <Route path="/enterprise/general-skills" element={<GeneralSkillsPage />} />
              <Route path="/enterprise/models" element={<ModelsPage />} />
              <Route path="/enterprise/tools" element={<ToolsPage />} />
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
                { label: '继承组织资源', value: 'copy' },
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
                placeholder="选择组织资源库或已有员工"
                options={agents.map((agent) => ({
                  value: agent.id,
                  label: agent.is_overall ? '组织资源库' : `${employeeDisplayName(agent)} · ${employeeProfile(agent).roleName}`,
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

export default function App() {
  const { effectiveTheme } = useThemeController();
  const isDark = effectiveTheme === 'dark';

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
        <Shell effectiveTheme={effectiveTheme} />
      </BrowserRouter>
    </ConfigProvider>
  );
}
