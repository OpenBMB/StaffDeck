import { ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { getAuthSession } from './api/client';
import ChatWindowPage from './pages/ChatWindowPage';
import LoginPage from './pages/LoginPage';
import SessionListPage from './pages/SessionListPage';
import { useThemeController } from './theme';

function RequireAuth({ children }: { children: JSX.Element }) {
  return getAuthSession() ? children : <Navigate to="/login" replace />;
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
          colorPrimary: '#0f766e',
          borderRadius: 8,
          colorBgBase: isDark ? '#111315' : '#fbfaf6',
          colorBgContainer: isDark ? '#181b1a' : '#ffffff',
          colorText: isDark ? '#e8e2d8' : '#20201d',
          colorTextSecondary: isDark ? '#a7aaa5' : '#6d726e',
          colorBorder: isDark ? '#303634' : '#ded7cc',
          fontFamily:
            '"Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif',
        },
      }}
    >
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/chat" element={<RequireAuth><SessionListPage /></RequireAuth>} />
          <Route path="/chat/:sessionId" element={<RequireAuth><ChatWindowPage /></RequireAuth>} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
}
