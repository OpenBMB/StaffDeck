// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { listAgentCatalog } from '@/lib/agentCatalog';
import { NewsPanel, QuickAccessTiles } from './PlazaWidgets';

//: 默认磁贴 = 清单里全部在线 Agent 应用；数量随清单变化，不能写死
//: （新增一个 Agent 就让 5/6 这类断言全崩，改一次清单要改三处测试）。
const CATALOG_APP_COUNT = listAgentCatalog(true).length;

// 组件内通过 api client 拉新闻与数字员工候选——测试里 mock 掉，不真发请求。
const apiGet = vi.fn();
vi.mock('@/api/client', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args), post: vi.fn().mockResolvedValue({}) },
  TENANT_ID: 'tenant_demo',
}));

const NEWS_PAYLOAD = {
  date: '2026-10-09',
  provenance: 'rss',
  items: {
    ai: [
      { category: 'ai', title: 'AI 新闻一', summary: 's', source: 'A', url: 'https://a.com/1', published_at: '10-09' },
      { category: 'ai', title: 'AI 新闻二', summary: 's', source: 'B', url: 'https://a.com/2', published_at: '10-09' },
      { category: 'ai', title: 'AI 新闻三', summary: 's', source: 'C', url: 'https://a.com/3', published_at: '10-09' },
    ],
    travel: [
      { category: 'travel', title: '旅文新闻一', summary: 's', source: 'D', url: 'https://t.com/1', published_at: '10-09' },
      { category: 'travel', title: '旅文新闻二', summary: 's', source: 'E', url: 'https://t.com/2', published_at: '10-09' },
      { category: 'travel', title: '旅文新闻三', summary: 's', source: 'F', url: 'https://t.com/3', published_at: '10-09' },
    ],
  },
};

const AGENTS = [
  { id: 'agent_sales', name: '销售助手', description: '跟进商机', is_overall: false },
  // 整体宿主不是可添加的岗位员工，应被过滤掉
  { id: 'agent_overall', name: '整体智能体', description: '', is_overall: true },
];

function renderWidgets() {
  return render(
    <MemoryRouter>
      <QuickAccessTiles />
      <NewsPanel />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  // 按 URL 分流：数字员工候选 / 新闻，避免两个请求互相拿到对方的 payload
  apiGet.mockImplementation((url: unknown) =>
    Promise.resolve(String(url).includes('/api/enterprise/agents') ? AGENTS : NEWS_PAYLOAD),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('PlazaWidgets（「全部」tab 右列组件）', () => {
  it('快捷磁贴默认只读：渲染默认 Agent 应用磁贴 + 虚线添加格；新闻两组各 3 条', async () => {
    renderWidgets();
    expect(await screen.findByText('AI 新闻一')).toBeTruthy();
    expect(screen.getByText('旅文新闻三')).toBeTruthy();
    // 默认磁贴来自本地清单 Agent 应用
    expect(screen.getByText('AI PPT Studio')).toBeTruthy();
    // 虚线添加格在浏览态也可见（demo 同款），点「管理 →」才进编辑
    expect(screen.getByText('+ 添加快捷方式')).toBeTruthy();
    expect(screen.queryByText('管理 →')).toBeTruthy();
    // 只读模式没有保存/取消按钮与删除按钮
    expect(screen.queryByText('保存')).toBeNull();
    expect(screen.queryAllByLabelText(/移除/)).toHaveLength(0);
    // 新闻 + 数字员工候选两个请求都发出
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2));
  });

  it('编辑→保存：可移除磁贴，保存写 localStorage 并回到只读', async () => {
    renderWidgets();
    await screen.findByText('AI 新闻一');
    fireEvent.click(screen.getByText('管理 →'));
    // 编辑模式出现重置/取消/保存按钮和每个磁贴的删除按钮
    expect(screen.getByText('保存')).toBeTruthy();
    const delButtons = screen.getAllByLabelText(/移除/);
    expect(delButtons.length).toBe(CATALOG_APP_COUNT);
    fireEvent.click(delButtons[0]);
    expect(screen.getAllByLabelText(/移除/)).toHaveLength(CATALOG_APP_COUNT - 1);

    fireEvent.click(screen.getByText('保存'));
    expect(screen.queryByText('保存')).toBeNull();
    const stored = JSON.parse(window.localStorage.getItem('ultrarag_plaza_home_layout_v1') || 'null');
    expect(stored.chips).toHaveLength(CATALOG_APP_COUNT - 1);
    // 保存后回到只读：删除按钮消失
    expect(screen.queryAllByLabelText(/移除/)).toHaveLength(0);
  });

  it('编辑→取消：恢复进入编辑前的布局，不写 localStorage', async () => {
    renderWidgets();
    await screen.findByText('AI 新闻一');
    fireEvent.click(screen.getByText('管理 →'));
    fireEvent.click(screen.getAllByLabelText(/移除/)[0]);
    fireEvent.click(screen.getByText('取消'));
    expect(screen.getAllByText(/AI PPT Studio/).length).toBeGreaterThan(0);
    expect(window.localStorage.getItem('ultrarag_plaza_home_layout_v1')).toBeNull();
  });

  it('浏览态添加快捷方式：立即落盘，重新挂载（≈刷新）后仍在', async () => {
    renderWidgets();
    await screen.findByText('AI 新闻一');

    fireEvent.click(screen.getByText('+ 添加快捷方式'));
    // 数字员工候选来自接口；整体宿主（is_overall）不出现
    const employee = await screen.findByText('销售助手');
    expect(screen.queryByText('整体智能体')).toBeNull();
    fireEvent.click(employee.closest('button') as HTMLElement);

    // 关键：浏览态没有「保存」按钮，添加即写 localStorage（此前只改 state，刷新即丢）
    const stored = JSON.parse(window.localStorage.getItem('ultrarag_plaza_home_layout_v1') || 'null');
    expect(stored?.chips).toHaveLength(CATALOG_APP_COUNT + 1);
    expect(stored.chips.some((chip: { kind: string; agentId?: string }) =>
      chip.kind === 'employee' && chip.agentId === 'agent_sales')).toBe(true);
    expect(screen.getAllByText('销售助手').length).toBeGreaterThan(0);

    // 模拟刷新：卸载后重新挂载，从 localStorage 恢复
    cleanup();
    renderWidgets();
    await screen.findByText('AI 新闻一');
    expect(screen.getAllByText('销售助手').length).toBeGreaterThan(0);
  });

  it('编辑态添加快捷方式：先暂存，点「保存」才落盘（取消则丢弃）', async () => {
    renderWidgets();
    await screen.findByText('AI 新闻一');
    fireEvent.click(screen.getByText('管理 →'));

    fireEvent.click(screen.getByText('+ 添加快捷方式'));
    fireEvent.click(await screen.findByText('销售助手'));
    // 编辑态仍走暂存语义：此时还没写盘
    expect(window.localStorage.getItem('ultrarag_plaza_home_layout_v1')).toBeNull();

    fireEvent.click(screen.getByText('保存'));
    const stored = JSON.parse(window.localStorage.getItem('ultrarag_plaza_home_layout_v1') || 'null');
    expect(stored?.chips).toHaveLength(CATALOG_APP_COUNT + 1);
  });
});
