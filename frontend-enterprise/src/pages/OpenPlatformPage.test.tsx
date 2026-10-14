// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import { findAgentCatalogEntry } from '@/lib/agentCatalog';
import type { AgentProfileRead } from '@/types';

import OpenPlatformPage from './OpenPlatformPage';

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
} as unknown as AgentProfileRead;

const galleryAgent = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  description: '客服数字员工',
  is_overall: false,
  status: 'active',
  metadata: { published_to_gallery: true },
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
} as unknown as AgentProfileRead;

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubPlatformFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/enterprise/agents/') && url.includes('/skills')) return jsonResponse([]);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent, galleryAgent]);
    if (url.includes('/api/enterprise/knowledge-bases')) {
      return jsonResponse([{
        id: 'kb-1',
        tenant_id: 'tenant_demo',
        name: '产品知识库',
        description: '产品资料',
        status: 'active',
        document_count: 3,
        bucket_count: 2,
        chunk_count: 10,
      }]);
    }
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
    if (url.includes('/api/enterprise/tools')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestedPaths(fetchMock: ReturnType<typeof stubPlatformFetch>): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

function renderPlatform(
  initialPath = '/enterprise/platform/agents',
  currentUser = admin,
  isAdmin = true,
) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route
            path="/enterprise/platform/:kind"
            element={<OpenPlatformPage currentUser={currentUser} isAdmin={isAdmin} />}
          />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('OpenPlatformPage 懒加载', () => {
  it('进入页面只拉当前模块，不会把 5 个接口全打一遍', async () => {
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    // 数字员工 tab 渲染出来即代表这批请求已经发完
    expect(await screen.findByText('小艾')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.some((url) => url.includes('/api/enterprise/agents'))).toBe(true);
    expect(paths.some((url) => url.includes('/api/enterprise/knowledge-bases'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
  });

  it('切到知识库 tab 时才拉知识库接口，其余模块仍不拉', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));

    expect(await screen.findByText('产品知识库')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.filter((url) => url.includes('/api/enterprise/knowledge-bases'))).toHaveLength(1);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
    // 广场资源要挂在 overall 宿主下取广场作用域
    expect(paths.some((url) => url.includes('agent_id=agent_tenant_demo_overall'))).toBe(true);
  });

  it('技能广场只要元信息，不带 MB 级的技能文件包', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /技能广场/ }));

    const paths = requestedPaths(fetchMock);
    const skillRequest = paths.find((url) => url.includes('/api/enterprise/general-skills'));
    expect(skillRequest).toBeTruthy();
    expect(skillRequest).toContain('include_files=0');
  });

  it('来回切 tab 不会重复请求已加载过的模块', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));
    await screen.findByText('产品知识库');
    await user.click(screen.getByRole('tab', { name: /数字员工广场/ }));
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));
    await screen.findByText('产品知识库');

    const paths = requestedPaths(fetchMock);
    expect(paths.filter((url) => url.includes('/api/enterprise/agents'))).toHaveLength(1);
    expect(paths.filter((url) => url.includes('/api/enterprise/knowledge-bases'))).toHaveLength(1);
  });

  it('聚合计数接口失败时不显示假角标，加载完成后才出现本地数量', async () => {
    const user = userEvent.setup();
    stubPlatformFetch(); // gallery/counts 未拦截 -> 静默失败；模块 tab 本身不发该请求（已移到「全部」），无假角标
    renderPlatform();

    await screen.findByText('小艾');
    // 数字员工 tab 已加载 -> 本地角标 1；知识库未访问且聚合计数不可用 -> 不显示
    expect(screen.getByRole('tab', { name: /数字员工广场/ }).textContent).toContain('1');
    expect(screen.getByRole('tab', { name: /知识库广场/ }).textContent).not.toContain('1');

    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /知识库广场/ }).textContent).toContain('1');
    });
  });

  it('「全部」tab 落地即拉聚合计数，一次填满所有 tab 角标', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/gallery/counts')) {
        return jsonResponse({ agents: 3, knowledge: 2, general_skills: 5, skills: 4, tools: 6 });
      }
      if (url.includes('/api/enterprise/agents/') && url.includes('/skills')) return jsonResponse([]);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent, galleryAgent]);
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPlatform('/enterprise/platform/all');

    // 未访问的模块也直接显示聚合计数
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /知识库广场/ }).textContent).toContain('2');
    });
    expect(
      screen.getByRole('tab', { name: /技能广场/ }).textContent,
    ).toContain('5');
    expect(screen.getByRole('tab', { name: /SOP 广场/ }).textContent).toContain('4');
    expect(requestedPaths(fetchMock).some((url) => url.includes('/api/enterprise/gallery/counts'))).toBe(true);
  });

  it('深链到某个模块时只拉它自己', async () => {
    const fetchMock = stubPlatformFetch();
    renderPlatform('/enterprise/platform/knowledge');

    expect(await screen.findByText('产品知识库')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.some((url) => url.includes('/api/enterprise/knowledge-bases'))).toBe(true);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
  });
});

describe('Agent 广场', () => {
  // 清单里的名字 / 作者会被平台维护者随时改，断言从清单取值而不是硬编码
  const slidesEntry = findAgentCatalogEntry('slides-maker')!;

  it('把本地清单里的 Agent 渲染成只读卡片', async () => {
    stubPlatformFetch();
    renderPlatform('/enterprise/platform/agent-apps');

    expect(await screen.findByText(slidesEntry.name)).toBeTruthy();
    // 决策助手已并入清单，两张卡片都在
    expect(screen.getByText('决策助手')).toBeTruthy();
    // meta 行是「作者 · 更新于 日期」；两个 Agent 可能同作者，断言用 getAllByText
    const metas = screen.getAllByText(new RegExp(`${slidesEntry.author} · 更新于`));
    expect(metas.length).toBeGreaterThanOrEqual(1);
  });

  it('详情抽屉给「立即使用」，且不暴露任何编辑/删除入口', async () => {
    const user = userEvent.setup();
    stubPlatformFetch();
    renderPlatform('/enterprise/platform/agent-apps');

    await screen.findByText(slidesEntry.name);
    await user.click(screen.getByRole('button', { name: new RegExp(slidesEntry.name) }));

    expect(await screen.findByRole('button', { name: '立即使用' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
    expect(screen.getByText(/只能使用、不能编辑或删除/)).toBeTruthy();
  });

  it('AI CodeReviewer 是 adminOnly 条目：管理员可见，普通成员看不到', async () => {
    stubPlatformFetch();
    renderPlatform('/enterprise/platform/agent-apps');
    expect(await screen.findByText('AI CodeReviewer')).toBeTruthy();
    cleanup();

    const member: EnterpriseAuthUser = { ...admin, id: 'user-member', username: 'member', role: 'member' };
    stubPlatformFetch();
    renderPlatform('/enterprise/platform/agent-apps', member, false);
    expect(await screen.findByText(slidesEntry.name)).toBeTruthy();
    expect(screen.queryByText('AI CodeReviewer')).toBeNull();
  });

  it('免费员工的卡片显示「免费」徽标', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents/') && url.includes('/skills')) return jsonResponse([]);
      if (url.includes('/api/enterprise/agents')) {
        return jsonResponse([overallAgent, { ...galleryAgent, free_model_enabled: true, free_model_remaining: 10 }]);
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPlatform('/enterprise/platform/agents');
    await screen.findByText('小艾');
    expect(await screen.findAllByText('免费')).toHaveLength(1);
  });

  it('Agent 广场是纯本地数据，不请求后端列表接口', async () => {
    const fetchMock = stubPlatformFetch();
    renderPlatform('/enterprise/platform/agent-apps');

    await screen.findByText(slidesEntry.name);

    const paths = requestedPaths(fetchMock);
    expect(paths.some((url) => url.includes('/api/enterprise/agents'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/knowledge-bases'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
  });
});
