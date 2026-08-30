// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { AgentProfileRead, KnowledgeBaseRead } from '@/types';

import KnowledgeManagePage, { KnowledgeAddPage } from './KnowledgePage';

const currentUser = {
  id: 'user-admin',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin' as const,
};

const dedicatedBase: KnowledgeBaseRead = {
  id: 'kb-dedicated',
  tenant_id: 'tenant_demo',
  name: '个人素材库',
  status: 'active',
  mode: 'dedicated',
  version: '1.0.0',
  document_count: 0,
  bucket_count: 0,
  chunk_count: 0,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const sharedBase: KnowledgeBaseRead = {
  id: 'kb-shared',
  tenant_id: 'tenant_demo',
  name: '团队选题库',
  status: 'active',
  mode: 'shared',
  published_version_id: 'kbver-shared-100',
  published_version: '1.0.0',
  version: '1.0.0',
  bound_team_count: 2,
  document_count: 0,
  bucket_count: 0,
  chunk_count: 0,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const employeeAgent: AgentProfileRead = {
  id: 'agent-source',
  tenant_id: 'tenant_demo',
  name: '内容员工',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  /** 构造页面测试使用的最小 JSON fetch 响应。 */
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubKnowledgeFetch(options?: { agents?: AgentProfileRead[] }) {
  /** 为管理页和新建页提供可按员工作用域调整的只读依赖接口。 */
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/agents')) return jsonResponse(options?.agents || []);
    if (url.includes('/model-configs')) return jsonResponse([]);
    if (url.includes('/knowledge/jobs')) return jsonResponse([]);
    if (url.includes('/knowledge/documents')) return jsonResponse([]);
    if (url.includes('/okf/concepts')) return jsonResponse([]);
    if (url.includes('/audit-events')) {
      return jsonResponse({ items: [], total: 0, offset: 0, limit: 20, has_more: false });
    }
    if (url.includes('/knowledge-bases/kb-shared/teams')) {
      return jsonResponse([{ id: 'team-content', name: '内容团队' }]);
    }
    if (url.includes('/knowledge-bases/kb-shared/versions')) {
      return jsonResponse([{
        id: 'kbver-shared-100',
        tenant_id: 'tenant_demo',
        knowledge_base_id: 'kb-shared',
        version: '1.0.0',
        name: '团队选题库',
        status: 'active',
        publication_state: 'released',
        is_published_head: true,
        created_at: '2026-08-01T00:00:00Z',
        updated_at: '2026-08-01T00:00:00Z',
      }]);
    }
    if (url.includes('/teams/team-content/knowledge-bases')) {
      return jsonResponse([{
        id: 'teamkb-content',
        team_id: 'team-content',
        knowledge_base_id: 'kb-shared',
        knowledge_base_name: '团队选题库',
        status: 'active',
        revision: 1,
        is_default: true,
        grants: [],
        created_at: '2026-08-01T00:00:00Z',
        updated_at: '2026-08-01T00:00:00Z',
      }]);
    }
    if (url.includes('/teams?')) {
      return jsonResponse([{
        id: 'team-content',
        tenant_id: 'tenant_demo',
        name: '内容团队',
        owner_user_id: 'user-admin',
        config: {},
        status: 'active',
        members: [],
        created_at: '2026-08-01T00:00:00Z',
        updated_at: '2026-08-01T00:00:00Z',
      }]);
    }
    if (url.includes('/knowledge-bases') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as { mode?: 'dedicated' | 'shared'; name?: string };
      return jsonResponse({
        ...(body.mode === 'shared' ? sharedBase : dedicatedBase),
        name: body.name,
      });
    }
    if (url.includes('/knowledge-bases')) return jsonResponse([dedicatedBase, sharedBase]);
    return jsonResponse([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationEcho() {
  /** 暴露导航结果，避免测试依赖真实路由页面。 */
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderAddPage() {
  /** 在真实路由边界内渲染知识库新建页。 */
  return render(
    <I18nProvider>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/enterprise/knowledge/new']}>
          <Routes>
            <Route path="/enterprise/knowledge/new" element={<KnowledgeAddPage currentUser={currentUser} />} />
            <Route path="/enterprise/knowledge" element={<LocationEcho />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </I18nProvider>,
  );
}

beforeAll(() => {
  // Radix Select 在 jsdom 中需要 pointer capture API。
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('KnowledgePage shared knowledge', () => {
  it('creates a shared knowledge base from the explicit dedicated/shared choice', async () => {
    const user = userEvent.setup();
    const fetchMock = stubKnowledgeFetch();
    renderAddPage();

    const form = await screen.findByLabelText('新建知识库配置');
    expect(within(form).getByRole('radio', { name: /^专用知识库/ })).toBeTruthy();
    await user.click(within(form).getByRole('radio', { name: /^共享知识库/ }));
    await user.type(within(form).getByLabelText('知识库名称'), '团队内容中台');
    await user.type(within(form).getByLabelText('知识库描述'), '沉淀选题、素材与复盘');
    await user.click(within(form).getByRole('button', { name: '创建知识库' }));

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(([input, init]) => (
        String(input).includes('/knowledge-bases') && init?.method === 'POST'
      ));
      const body = JSON.parse(String(createCall?.[1]?.body)) as Record<string, unknown>;
      expect(body).toMatchObject({
        tenant_id: 'tenant_demo',
        name: '团队内容中台',
        description: '沉淀选题、素材与复盘',
        mode: 'shared',
      });
      expect(body.agent_id).toBeUndefined();
    });
    expect((await screen.findByTestId('location')).textContent).toBe('/enterprise/knowledge');
  });

  it('shows dedicated and shared type badges in the management list', async () => {
    stubKnowledgeFetch();
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <KnowledgeManagePage currentUser={currentUser} />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    const list = await screen.findByLabelText('知识库列表');
    expect(within(list).getAllByText('专用知识库').length).toBeGreaterThanOrEqual(1);
    expect(within(list).getAllByText('共享知识库').length).toBeGreaterThanOrEqual(1);
    expect(within(list).getByText('2 个团队')).toBeTruthy();
  });

  it('offers conversion only for an active dedicated base in an employee scope', async () => {
    /** 验证专用员工分支出现转换入口，而共享库不出现反向转换入口。 */
    const user = userEvent.setup();
    window.localStorage.setItem('ultrarag_enterprise_agent_scope', 'agent-source');
    stubKnowledgeFetch({ agents: [employeeAgent] });
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <KnowledgeManagePage currentUser={currentUser} />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    const knowledgeList = await screen.findByLabelText('知识库列表');
    const dedicatedRow = within(knowledgeList).getByText('个人素材库').closest('tr');
    expect(dedicatedRow).toBeTruthy();
    await user.click(within(dedicatedRow as HTMLTableRowElement).getByRole('button', {
      name: '知识库操作',
    }));
    expect(await screen.findByRole('menuitem', { name: '转换为共享知识库' })).toBeTruthy();

    await user.keyboard('{Escape}');
    const sharedRow = within(knowledgeList).getByText('团队选题库').closest('tr');
    expect(sharedRow).toBeTruthy();
    await user.click(within(sharedRow as HTMLTableRowElement).getByRole('button', {
      name: '知识库操作',
    }));
    expect(await screen.findByRole('menuitem', { name: '版本管理' })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: '转换为共享知识库' })).toBeNull();
  });

  it('opens the shared version dialog with an audit-history entry point', async () => {
    /** 共享库的版本管理入口必须同时承载正式版本和审计复盘视图。 */
    const user = userEvent.setup();
    const fetchMock = stubKnowledgeFetch();
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <KnowledgeManagePage currentUser={currentUser} />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    const knowledgeList = await screen.findByLabelText('知识库列表');
    const sharedRow = within(knowledgeList).getByText('团队选题库').closest('tr');
    expect(sharedRow).toBeTruthy();
    await user.click(within(sharedRow as HTMLTableRowElement).getByRole('button', {
      name: '知识库操作',
    }));
    await user.click(await screen.findByRole('menuitem', { name: '版本管理' }));

    const dialog = await screen.findByRole('dialog', { name: /共享版本：团队选题库/ });
    expect(within(dialog).getByRole('tab', { name: '审计历史' })).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes('/knowledge-bases/kb-shared/teams?')
    ))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('/teams/team-content/knowledge-bases')
    ))).toBe(false);
  });
});
