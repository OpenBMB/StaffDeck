// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '@/auth';
import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui';
import GeneralSkillsPage, { GeneralSkillNewPage } from './GeneralSkillsPage';

/**
 * 技能广场的标签分类：列表按分类下拉过滤 + 分类徽标/未分类占位。
 */

const overallAgent = {
  id: 'agent_overall',
  tenant_id: 'tenant_demo',
  name: '整体智能体',
  is_overall: true,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const member: EnterpriseAuthUser = {
  id: 'user_member',
  tenant_id: 'tenant_demo',
  username: 'member',
  role: 'admin',
};

function skillRow(overrides: Record<string, unknown>) {
  return {
    id: `genskill_${overrides.slug}`,
    tenant_id: 'tenant_demo',
    slug: overrides.slug,
    name: overrides.name,
    description: '',
    homepage: '',
    category: null,
    skill_markdown: '# 技能\n',
    skill_files: [],
    skill_directories: [],
    metadata: { owner_user_id: 'user_member' },
    status: 'published',
    capability_scope: 'general',
    download_count: 0,
    permissions: {},
    runtime_config: {},
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...overrides,
  };
}

const ROWS = [
  skillRow({ slug: 'office-skill', name: '公文助手', category: '办公类' }),
  skillRow({ slug: 'finance-skill', name: '报销助手', category: '财务类' }),
  skillRow({ slug: 'uncategorized', name: '随手技能' }),
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function renderPage() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
      if (url.includes('/api/enterprise/general-skills')) return jsonResponse(ROWS);
      return jsonResponse({});
    }),
  );

  render(
    <I18nProvider>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/enterprise/general-skills']}>
          <GeneralSkillsPage currentUser={member} />
        </MemoryRouter>
      </TooltipProvider>
    </I18nProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
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

describe('技能广场 标签分类', () => {  it('列表展示分类徽标，未分类的技能显示占位文案', async () => {
    renderPage();
    await screen.findAllByText('公文助手');

    expect(screen.getAllByText('办公类').length).toBeGreaterThan(0);
    expect(screen.getAllByText('财务类').length).toBeGreaterThan(0);
    expect(screen.getAllByText('未分类').length).toBeGreaterThan(0);
  });

  it('按分类下拉过滤后只保留该分类的技能', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByText('公文助手');

    await user.click(screen.getByRole('combobox', { name: '标签分类筛选' }));
    await user.click(await screen.findByRole('option', { name: '财务类' }));

    await waitFor(() => {
      expect(screen.queryAllByText('公文助手').length).toBe(0);
    });
    expect(screen.queryAllByText('随手技能').length).toBe(0);
    expect(screen.getAllByText('报销助手').length).toBeGreaterThan(0);
  });

  it('新建技能时选的分类随保存请求上送', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
      if (url.includes('/api/enterprise/general-skills/import')) {
        return jsonResponse(skillRow({ slug: 'new-skill', name: '分类新技能', category: '编程类' }));
      }
      if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
      void init;
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={['/enterprise/general-skills/new']}>
            <GeneralSkillNewPage currentUser={member} />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    const nameInput = await screen.findByLabelText('技能名称');
    await user.type(nameInput, '分类新技能');
    await user.click(screen.getByRole('combobox', { name: '标签分类' }));
    await user.click(await screen.findByRole('option', { name: '编程类' }));
    await user.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => {
      const importCall = fetchMock.mock.calls.find((call) => String(call[0]).includes('/general-skills/import'));
      expect(importCall).toBeTruthy();
      const body = JSON.parse(String((importCall?.[1] as RequestInit | undefined)?.body));
      expect(body.category).toBe('编程类');
    });
  });
});