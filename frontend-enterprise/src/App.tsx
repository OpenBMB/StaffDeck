import {
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DislikeOutlined,
  FileSearchOutlined,
  MessageOutlined,
  ProfileOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, ConfigProvider, Layout, Menu, Typography, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useMemo, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import FeedbackPage from './pages/FeedbackPage';
import GeneralSkillsPage from './pages/GeneralSkillsPage';
import KnowledgePage from './pages/KnowledgePage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import PersonaPage from './pages/PersonaPage';
import SkillsPage from './pages/SkillsPage';
import ToolsPage from './pages/ToolsPage';
import { ThemeToggleButton, useThemeController, type EffectiveTheme } from './theme';

const { Header, Sider, Content } = Layout;

function Shell({ effectiveTheme }: { effectiveTheme: EffectiveTheme }) {
  const navigate = useNavigate();
  const location = useLocation();
  const selected = location.pathname === '/enterprise' ? '/enterprise/dashboard' : location.pathname;
  const isDistillRoute = location.pathname === '/enterprise/skills/distill';
  const [lastDistillSearch, setLastDistillSearch] = useState(() => (isDistillRoute ? location.search : ''));
  const distillSearch = isDistillRoute ? location.search : lastDistillSearch;
  const distillSearchParams = useMemo(() => new URLSearchParams(distillSearch), [distillSearch]);

  useEffect(() => {
    if (isDistillRoute) {
      setLastDistillSearch(location.search);
    }
  }, [isDistillRoute, location.search]);

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
        <div className="nav-label">Workspace</div>
        <Menu
          className="nav-menu"
          mode="inline"
          selectedKeys={[selected]}
          onClick={(item) => navigate(item.key)}
          items={[
            { key: '/enterprise/dashboard', icon: <DashboardOutlined />, label: 'Dashboard' },
            { key: '/enterprise/memories', icon: <DatabaseOutlined />, label: 'Memory 查询' },
            { key: '/enterprise/knowledge', icon: <FileSearchOutlined />, label: '知识库' },
            { key: '/enterprise/feedback', icon: <DislikeOutlined />, label: '负反馈会话' },
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
        <div className="sidebar-footer">
          <span className="status-dot" />
          <span>local runtime</span>
        </div>
      </Sider>
      <Layout>
        <Header className="topbar">
          <div>
            <Typography.Text strong>Skill Studio</Typography.Text>
            <div className="topbar-subtitle">Skill, tool, memory and persona workspace</div>
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
              <Route path="/enterprise/knowledge" element={<KnowledgePage />} />
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
