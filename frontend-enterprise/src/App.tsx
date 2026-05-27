import {
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  MessageOutlined,
  ProfileOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, ConfigProvider, Layout, Menu, Typography } from 'antd';
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import DashboardPage from './pages/DashboardPage';
import DistillPage from './pages/DistillPage';
import MemoriesPage from './pages/MemoriesPage';
import ModelsPage from './pages/ModelsPage';
import PersonaPage from './pages/PersonaPage';
import SkillsPage from './pages/SkillsPage';
import ToolsPage from './pages/ToolsPage';

const { Header, Sider, Content } = Layout;

function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const selected = location.pathname === '/enterprise' ? '/enterprise/dashboard' : location.pathname;
  return (
    <Layout className="app-shell">
      <Sider width={232} theme="light" className="sidebar">
        <div className="brand">
            <span className="brand-mark">SA</span>
            <div>
            <div className="brand-title">Skill Agent</div>
            <div className="brand-subtitle">Control Desk</div>
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
            <Typography.Text strong>Enterprise Console</Typography.Text>
            <div className="topbar-subtitle">Skill, tool, model and persona configuration</div>
          </div>
          <div className="topbar-actions">
            <Button icon={<UserOutlined />} onClick={() => navigate('/enterprise/persona')}>人设</Button>
          </div>
        </Header>
        <Content className="content">
          <Routes>
            <Route path="/enterprise" element={<Navigate to="/enterprise/dashboard" replace />} />
            <Route path="/enterprise/dashboard" element={<DashboardPage />} />
            <Route path="/enterprise/memories" element={<MemoriesPage />} />
            <Route path="/enterprise/skills" element={<SkillsPage />} />
            <Route path="/enterprise/skills/distill" element={<DistillPage />} />
            <Route path="/enterprise/models" element={<ModelsPage />} />
            <Route path="/enterprise/tools" element={<ToolsPage />} />
            <Route path="/enterprise/persona" element={<PersonaPage />} />
            <Route path="*" element={<Navigate to="/enterprise/dashboard" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: '#171717',
          borderRadius: 8,
          colorText: '#171717',
          colorTextSecondary: '#737373',
          colorBorder: '#e5e5e5',
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
        },
      }}
    >
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </ConfigProvider>
  );
}
