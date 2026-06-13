import {
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DislikeOutlined,
  FileAddOutlined,
  FileSearchOutlined,
  MessageOutlined,
  PlusOutlined,
  ProfileOutlined,
  RobotOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, ConfigProvider, Input, Layout, Menu, Modal, Radio, Select, Typography, message, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from './api/client';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import FeedbackPage from './pages/FeedbackPage';
import GeneralSkillsPage from './pages/GeneralSkillsPage';
import KnowledgeManagePage, { KnowledgeAddPage } from './pages/KnowledgePage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import PersonaPage from './pages/PersonaPage';
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
  sourceMode: AgentCreateMode;
  copyFromAgentId: string;
};

const EMPTY_AGENT_FORM: AgentCreateFormState = {
  name: '',
  description: '',
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
    : location.pathname.startsWith('/enterprise/knowledge/new')
      ? '/enterprise/knowledge/new'
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
      message.error('请填写智能体名称');
      return;
    }
    const created = await api.post<AgentProfileRead>('/api/enterprise/agents', {
      tenant_id: TENANT_ID,
      name,
      description: agentForm.description || undefined,
      source_mode: agentForm.sourceMode,
      copy_from_agent_id: agentForm.sourceMode === 'copy' ? agentForm.copyFromAgentId || undefined : undefined,
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
            <div className="brand-subtitle">Skill Studio</div>
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
              label: '工作区',
              children: [
                { key: '/enterprise/dashboard', icon: <DashboardOutlined />, label: '看板' },
                { key: '/enterprise/memories', icon: <DatabaseOutlined />, label: '记忆查询' },
                { key: '/enterprise/feedback', icon: <DislikeOutlined />, label: '负反馈会话' },
              ],
            },
            {
              key: 'knowledge',
              type: 'group',
              label: '知识',
              children: [
                { key: '/enterprise/knowledge', icon: <FileSearchOutlined />, label: '知识管理' },
                { key: '/enterprise/knowledge/new', icon: <FileAddOutlined />, label: '新增知识' },
              ],
            },
            {
              key: 'skills',
              type: 'group',
              label: '技能',
              children: [
                { key: '/enterprise/skills', icon: <ProfileOutlined />, label: '技能管理' },
                { key: '/enterprise/skills/distill', icon: <MessageOutlined />, label: '技能改写' },
                { key: '/enterprise/tools', icon: <ToolOutlined />, label: '工具配置' },
              ],
            },
            { key: '/enterprise/models', icon: <ApiOutlined />, label: '模型配置' },
          ]}
        />
        <div className="agent-dock">
          <button
            type="button"
            className="agent-dock-mark"
            title="新增智能体"
            aria-label="新增智能体"
            onClick={openCreateAgentModal}
          >
            <RobotOutlined />
          </button>
          <div className="agent-dock-main">
            <div className="agent-dock-label">智能体</div>
            <Select
              className="agent-dock-select"
              value={selectedAgentId || undefined}
              placeholder="选择智能体"
              popupMatchSelectWidth={260}
              options={agents.map((agent) => ({
                value: agent.id,
                label: agent.is_overall ? `整体 · ${agent.name}` : agent.name,
              }))}
              onChange={changeAgentScope}
              popupRender={(menu) => (
                <>
                  {menu}
                  <div className="agent-dock-dropdown-footer" onMouseDown={(event) => event.preventDefault()}>
                    <Button type="text" block icon={<PlusOutlined />} onClick={openCreateAgentModal}>
                      新增智能体
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
            <Typography.Text strong>{selectedAgent?.name || '智能体'}</Typography.Text>
            <div className="topbar-subtitle">
              {selectedAgent?.is_overall ? '整体资源池' : selectedAgent?.description || '分支工作域'}
            </div>
          </div>
          <div className="topbar-actions">
            <ThemeToggleButton />
            <Button icon={<UserOutlined />} onClick={() => navigate('/enterprise/persona')}>人设</Button>
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
              <Route path="/enterprise/memories" element={<MemoriesPage />} />
              <Route path="/enterprise/knowledge" element={<KnowledgeManagePage />} />
              <Route path="/enterprise/knowledge/new" element={<KnowledgeAddPage />} />
              <Route path="/enterprise/feedback" element={<FeedbackPage />} />
              <Route path="/enterprise/skills" element={<SkillsPage />} />
              <Route path="/enterprise/general-skills" element={<GeneralSkillsPage />} />
              <Route path="/enterprise/models" element={<ModelsPage />} />
              <Route path="/enterprise/tools" element={<ToolsPage />} />
              <Route path="/enterprise/persona" element={<PersonaPage />} />
              <Route path="*" element={<Navigate to="/enterprise/dashboard" replace />} />
            </Routes>
          )}
        </Content>
      </Layout>
      <Modal
        title="新增智能体"
        open={agentCreateOpen}
        onCancel={() => setAgentCreateOpen(false)}
        onOk={saveAgentCreateModal}
        okText="保存"
        cancelText="取消"
      >
        <div className="agent-editor-form">
          <label>
            创建方式
            <Radio.Group
              className="agent-create-mode"
              value={agentForm.sourceMode}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, sourceMode: event.target.value }))}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '复制已有智能体', value: 'copy' },
                { label: '空白智能体', value: 'blank' },
              ]}
            />
          </label>
          {agentForm.sourceMode === 'copy' && (
            <label>
              复制来源
              <Select
                value={agentForm.copyFromAgentId || undefined}
                placeholder="选择一个已有智能体"
                options={agents.map((agent) => ({
                  value: agent.id,
                  label: agent.is_overall ? `整体 · ${agent.name}` : agent.name,
                }))}
                onChange={(value) => setAgentForm((prev) => ({ ...prev, copyFromAgentId: value }))}
              />
            </label>
          )}
          {agentForm.sourceMode === 'blank' && (
            <div className="agent-definition-note">空白智能体不会继承技能、知识库、通用技能、人设或模型绑定。</div>
          )}
          <label>
            名称
            <Input value={agentForm.name} onChange={(event) => setAgentForm((prev) => ({ ...prev, name: event.target.value }))} />
          </label>
          <label>
            描述
            <Input.TextArea
              rows={3}
              value={agentForm.description}
              onChange={(event) => setAgentForm((prev) => ({ ...prev, description: event.target.value }))}
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
