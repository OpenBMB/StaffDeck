import { DesktopOutlined, MoonOutlined, SunOutlined } from '@ant-design/icons';
import { Button, Tooltip } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';

export type ThemeMode = 'system' | 'light' | 'dark';
export type EffectiveTheme = 'light' | 'dark';

const STORAGE_KEY = 'ultrarag_theme_mode';
const ORDER: ThemeMode[] = ['system', 'light', 'dark'];

function getStoredTheme(): ThemeMode {
  if (typeof window === 'undefined') return 'system';
  const value = window.localStorage.getItem(STORAGE_KEY);
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
}

function systemTheme(): EffectiveTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function resolveTheme(mode: ThemeMode): EffectiveTheme {
  return mode === 'system' ? systemTheme() : mode;
}

function applyTheme(mode: ThemeMode, effective: EffectiveTheme) {
  const root = document.documentElement;
  root.classList.remove('light', 'dark');
  root.classList.add(effective);
  root.setAttribute('data-theme', effective);
  root.setAttribute('data-theme-mode', mode);
  root.style.colorScheme = effective;
}

export function useThemeController() {
  const [mode, setModeState] = useState<ThemeMode>(getStoredTheme);
  const [effectiveTheme, setEffectiveTheme] = useState<EffectiveTheme>(() => resolveTheme(getStoredTheme()));

  const setMode = useCallback((next: ThemeMode) => {
    window.localStorage.setItem(STORAGE_KEY, next);
    setModeState(next);
    const effective = resolveTheme(next);
    setEffectiveTheme(effective);
    applyTheme(next, effective);
  }, []);

  useEffect(() => {
    const effective = resolveTheme(mode);
    setEffectiveTheme(effective);
    applyTheme(mode, effective);

    if (mode !== 'system' || typeof window.matchMedia !== 'function') return undefined;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      const next = resolveTheme('system');
      setEffectiveTheme(next);
      applyTheme('system', next);
    };
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [mode]);

  const cycleMode = useCallback(() => {
    const index = ORDER.indexOf(mode);
    setMode(ORDER[(index + 1) % ORDER.length]);
  }, [mode, setMode]);

  return { mode, effectiveTheme, setMode, cycleMode };
}

export function ThemeToggleButton() {
  const { mode, effectiveTheme, cycleMode } = useThemeController();
  const icon = useMemo(() => {
    if (mode === 'system') return <DesktopOutlined />;
    return effectiveTheme === 'dark' ? <MoonOutlined /> : <SunOutlined />;
  }, [effectiveTheme, mode]);
  const label = mode === 'system' ? `跟随系统（当前${effectiveTheme === 'dark' ? '深色' : '浅色'}）` : mode === 'dark' ? '深色主题' : '浅色主题';

  return (
    <Tooltip title={`${label}，点击切换`}>
      <Button
        type="text"
        className="theme-toggle-button"
        icon={icon}
        aria-label="切换主题"
        onClick={cycleMode}
      />
    </Tooltip>
  );
}
