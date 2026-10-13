// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '@/auth';
import { I18nProvider } from '@/i18n';

import OpenPlatformPage from './OpenPlatformPage';

/**
 * 开放广场「技能广场」tab（/enterprise/platform/general-skills）的标签分类下拉过滤。
 */

const admin: EnterpriseAuthUser = {
  id: 'user-admin',
  username: 'admin',
  tenant_id: 'tenant_demo',
  role: 'admin',
};

const overallAgent = {
  id: 'agent_tenant_demo_overall',
  tenant_id: 'tenant_demo',
  name: '整体智能体',
  is_overall: true,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function skillRow(slug: string, name: string, category: string | null) {
  return {
    id: `genskill_${slug}`,
    tenant_id: 'tenant_demo',
    slug,
    name,
    description: `${name}的说明`,
    category,
    homepage: '',
    skill_markdown: '# x',
    skill_files: [],
    skill_directories: [],
    metadata: { owner_user_id: 'user-admin' },
    status: 'published',
    capability_scope: 'general',
    download_count: 3,
    permissions: {},
    runtime_config: {},
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  };
}

const SKILLS = [
  skillRow('doc-writer', '公文助手', '办公类'),
  skillRow('expense-bot', '报销助手', '财务类'),
  skillRow('code-review', '代码审查', '编程类'),
  skillRow('misc-tool', '随手工具', null),
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function renderPlatform() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
      if (url.includes('/api/enterprise/general-skills')) return jsonResponse(SKILLS);
      return jsonResponse({});
    }),
  );

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/platform/general-skills']}>
        <Routes>
          <Route
            path="/enterprise/platform/:kind"
            element={<OpenPlatformPage currentUser={admin} isAdmin />}
          />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('开放广场 技能广场 tab 标签分类', () => {
  it('技能 tab 显示标签分类下拉，选项为全部分类 + 四个分类', async () => {
    const user = userEvent.setup();
    renderPlatform();
    await screen.findAllByText(/公文助手/);

    const trigger = screen.getByRole('combobox', { name: '标签分类筛选' });
    expect(trigger.textContent).toContain('全部分类');

    await user.click(trigger);
    for (const label of ['全部分类', '办公类', '财务类', '人力资源类', '编程类']) {
      expect(await screen.findByRole('option', { name: label })).toBeTruthy();
    }
  });

  it('按分类过滤后只保留该分类的技能', async () => {
    const user = userEvent.setup();
    renderPlatform();
    await screen.findAllByText(/公文助手/);

    await user.click(screen.getByRole('combobox', { name: '标签分类筛选' }));
    await user.click(await screen.findByRole('option', { name: '财务类' }));

    await waitFor(() => {
      expect(screen.queryAllByText(/公文助手/).length).toBe(0);
    });
    expect(screen.queryAllByText(/随手工具/).length).toBe(0);
    expect(screen.getAllByText(/报销助手/).length).toBeGreaterThan(0);
  });
});