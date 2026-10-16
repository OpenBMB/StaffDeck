// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import { findAgentCatalogEntry } from '@/lib/agentCatalog';
import type { ModelConfigRead } from '@/types';

import AgentAppPage from './AgentAppPage';

const user: EnterpriseAuthUser = {
  id: 'user-1',
  username: 'member',
  tenant_id: 'tenant_demo',
  role: 'member',
};

const adminUser: EnterpriseAuthUser = { ...user, id: 'user-admin', username: 'admin', role: 'admin' };

function modelConfig(overrides: Partial<ModelConfigRead> = {}): ModelConfigRead {
  return {
    id: 'model-1',
    tenant_id: 'tenant_demo',
    name: '我的 flash',
    provider: 'openai_compatible',
    api_protocol: 'openai_chat_completions',
    base_url: 'https://example.com',
    api_key_masked: 'sk-***',
    model: 'deepseek-v4-flash',
    temperature: 0.2,
    max_output_tokens: 8192,
    extra_body: {},
    custom_headers: {},
    protocol_options: {},
    legacy_unmapped_options: {},
    trust_status: 'verified',
    verification_attempt_status: 'idle',
    config_revision: 1,
    security_revision: 1,
    is_default: true,
    is_intent_recognition: false,
    enabled: true,
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides,
  } as ModelConfigRead;
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function renderPage(entryId = 'slides-maker', currentUser: EnterpriseAuthUser = user) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[`/enterprise/agent-apps/${entryId}`]}>
        <Routes>
          <Route path="/enterprise/agent-apps/:entryId" element={<AgentAppPage currentUser={currentUser} />} />
          {/* adminOnly 深链守卫会把普通成员弹回广场，这里给个落点方便断言 */}
          <Route path="/enterprise/platform/agent-apps" element={<div>Agent 广场列表</div>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AgentAppPage', () => {
  it('未配置模型时禁用生成并给出模型配置入口', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    renderPage();
    const button = await screen.findByRole('button', { name: /生成幻灯片/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(await screen.findByText(/你还没有配置模型/)).toBeTruthy();
  });

  it('只列出用户自己已启用的模型，默认选中默认模型', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      modelConfig({ id: 'model-1', name: '默认模型', is_default: true }),
      modelConfig({ id: 'model-2', name: '备用模型', is_default: false }),
      modelConfig({ id: 'model-3', name: '停用模型', enabled: false }),
    ])));
    renderPage();
    // 触发器上显示的是默认模型名
    expect(await screen.findByText('默认模型')).toBeTruthy();
    const user1 = userEvent.setup();
    await user1.click(screen.getByRole('button', { name: '选择模型' }));
    expect(await screen.findByText('备用模型')).toBeTruthy();
    expect(screen.queryByText('停用模型')).toBeNull();
  });

  it('填好正文口述后生成幻灯片，并切到预览与源码两个视图', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      if (url.includes('/slides:generate')) {
        const body = JSON.parse(String(init?.body || '{}'));
        expect(body.model_config_id).toBe('model-1');
        expect(body.skeleton_pages).toEqual(['cover']);
        return jsonResponse({
          deck_title: '库存收尾',
          page_label: '库存收尾',
          pages: [
            { layout: 'cover', title: '库存收尾', subtitle: '', label: '', bullets: [], cards: [] },
            { layout: 'cards', title: '两条主线', subtitle: '', label: '', bullets: [], cards: [] },
          ],
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPage();

    const user1 = userEvent.setup();
    const textarea = await screen.findByPlaceholderText(/把汇报正文整段贴进来即可/);
    // 受控 textarea 用一次 change 写入：userEvent 逐字输入在 jsdom 里要跑 24 次渲染，纯属浪费
    fireEvent.change(textarea, { target: { value: '本月完成库存收尾，两条主线：合同转签与开票回收。' } });
    await user1.click(screen.getByRole('checkbox', { name: '首页' }));

    const button = screen.getByRole('button', { name: /生成幻灯片/ });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    await user1.click(button);

    expect(await screen.findByText('2 页 | cov · art')).toBeTruthy();
    // 预览渲染出真正的 slide 结构
    expect(document.querySelectorAll('.sd-slide').length).toBe(2);

    await user1.click(screen.getByRole('tab', { name: 'HTML 源码' }));
    const source = document.querySelector('pre');
    expect(source?.textContent).toContain('<!doctype html>');
  });

  it('空正文时按钮不可点', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([modelConfig()])));
    renderPage();
    const button = await screen.findByRole('button', { name: /生成幻灯片/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it('「导出 PPTX」与「生成幻灯片」在底部同一行：导出在左、生成在右', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([modelConfig()])));
    renderPage();

    const exportBtn = await screen.findByRole('button', { name: /导出 PPTX/ });
    const generateBtn = screen.getByRole('button', { name: /生成幻灯片/ });
    // 同一个父容器 = 同一行
    expect(exportBtn.parentElement).toBe(generateBtn.parentElement);
    expect(exportBtn.parentElement?.className).toContain('grid-cols-2');
    // DOM 顺序即视觉顺序：导出在左
    expect(exportBtn.parentElement?.children[0]).toBe(exportBtn);
    expect(exportBtn.parentElement?.children[1]).toBe(generateBtn);
    // 模型下拉单独占一行（不再是和按钮 50/50 的那一格）
    const modelBtn = screen.getByRole('button', { name: '选择模型' });
    expect(modelBtn.parentElement).not.toBe(exportBtn.parentElement);
    expect(modelBtn.className).toContain('w-full');
  });

  it('卡片顶部不再重复名称/作者，返回入口移到右侧', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([modelConfig()])));
    renderPage();
    await screen.findByRole('button', { name: /生成幻灯片/ });

    // 名称/作者/更新时间不再在卡片里重复出现
    const entry = findAgentCatalogEntry('slides-maker')!;
    expect(screen.queryByText(new RegExp(`${entry.author} · 更新于`))).toBeNull();
    expect(screen.queryByText(/更新于/)).toBeNull();

    const back = screen.getByRole('button', { name: /返回 Agent 广场/ });
    expect(back.parentElement?.className).toContain('justify-end');
  });

  it('「页数」是总页数：实时拆成「内容 + 骨架」，并原样下发 page_count', async () => {
    const bodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      if (url.includes('/slides:generate')) {
        bodies.push(JSON.parse(String(init?.body || '{}')));
        return jsonResponse({ deck_title: 'T', page_label: 'T', pages: [] });
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPage();

    const user1 = userEvent.setup();
    const textarea = await screen.findByPlaceholderText(/把汇报正文整段贴进来即可/);
    fireEvent.change(textarea, { target: { value: '正文' } });

    // 默认 5 页、没勾骨架页
    expect(screen.getByText('共 5 页 ＝ 内容 5 页 ＋ 骨架 0 页')).toBeTruthy();

    await user1.click(screen.getByRole('checkbox', { name: '首页' }));
    await user1.click(screen.getByRole('checkbox', { name: '结尾页' }));
    expect(screen.getByText('共 5 页 ＝ 内容 3 页 ＋ 骨架 2 页')).toBeTruthy();

    await user1.click(screen.getByRole('button', { name: /生成幻灯片/ }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    // 下发的还是「总页数」，换算留给服务端
    expect(bodies[0].page_count).toBe(5);
    expect(bodies[0].skeleton_pages).toEqual(['cover', 'end']);
  });

  it('骨架页会把「页数」下限抬到「骨架页数 + 1」，保证还有内容页', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([modelConfig()])));
    renderPage();

    const user1 = userEvent.setup();
    const input = await screen.findByRole('spinbutton');
    fireEvent.change(input, { target: { value: '2' } });
    expect((input as HTMLInputElement).value).toBe('2');

    await user1.click(screen.getByRole('checkbox', { name: '首页' }));
    await user1.click(screen.getByRole('checkbox', { name: '目录页' }));
    await user1.click(screen.getByRole('checkbox', { name: '结尾页' }));
    // 3 张骨架页 → 至少 4 页（3 + 1 张内容页）
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('4'));
    expect(screen.getByText('共 4 页 ＝ 内容 1 页 ＋ 骨架 3 页')).toBeTruthy();
  });

  it('预览里的文字可以直接改：改动回填 deck、进源码、并标记「已编辑」', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      if (url.includes('/slides:generate')) {
        return jsonResponse({
          deck_title: '库存收尾',
          page_label: '库存收尾',
          pages: [
            {
              layout: 'bullets',
              title: '两条主线',
              subtitle: '',
              label: '库存收尾',
              bullets: ['原来的要点'],
              cards: [],
            },
          ],
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPage();

    const user1 = userEvent.setup();
    const textarea = await screen.findByPlaceholderText(/把汇报正文整段贴进来即可/);
    fireEvent.change(textarea, { target: { value: '正文' } });
    await user1.click(screen.getByRole('button', { name: /生成幻灯片/ }));
    await screen.findByText('1 页 | txt');

    expect(screen.queryByText('已编辑')).toBeNull();

    // 鼠标改标题：contenteditable 元素改完失焦即提交（React 的 onBlur 走 focusout）
    const title = document.querySelector('[data-sd-path="p0.title"]') as HTMLElement;
    expect(title).not.toBeNull();
    title.textContent = '改过的标题';
    fireEvent.focusOut(title);
    expect(await screen.findByText('已编辑')).toBeTruthy();

    // HTML 源码用的是同一份 deck，所以改动能看到
    await user1.click(screen.getByRole('tab', { name: 'HTML 源码' }));
    expect(document.querySelector('pre')?.textContent).toContain('改过的标题');
    // 预览 HTML 不再回退成旧文案（切回预览会用最新 deck 重建）
    await user1.click(screen.getByRole('tab', { name: '幻灯片预览' }));
    expect(document.querySelector('[data-sd-path="p0.title"]')?.textContent).toBe('改过的标题');
  });

  it('「汇报样式」勾选项已下线：生成请求不再携带 style_prompt', async () => {
    const bodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      if (url.includes('/slides:generate')) {
        bodies.push(JSON.parse(String(init?.body || '{}')));
        return jsonResponse({ deck_title: 'T', page_label: 'T', pages: [] });
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPage();

    const user1 = userEvent.setup();
    const textarea = await screen.findByPlaceholderText(/把汇报正文整段贴进来即可/);

    fireEvent.change(textarea, { target: { value: '第一版正文' } });
    await user1.click(screen.getByRole('button', { name: /生成幻灯片/ }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).not.toHaveProperty('style_prompt');
    expect(screen.queryByRole('checkbox', { name: '旅文汇报样式' })).toBeNull();
    expect(screen.queryByText('汇报样式')).toBeNull();
  });

  it('导出 PPTX：未生成置灰，生成后可用并触发下载', async () => {
    const createObjectURL = vi.fn(() => 'blob:pptx');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    const exportBodies: Record<string, unknown>[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      if (url.includes('/slides:generate')) {
        return jsonResponse({
          deck_title: '库存收尾',
          page_label: '库存收尾',
          pages: [{ layout: 'bullets', title: '正文', subtitle: '', label: '', bullets: ['a'], cards: [] }],
        });
      }
      if (url.includes('/slides:export')) {
        exportBodies.push(JSON.parse(String(init?.body || '{}')));
        return {
          ok: true,
          status: 200,
          statusText: 'OK',
          blob: async () => new Blob(['pptx'], { type: 'application/octet-stream' }),
        } as unknown as Response;
      }
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPage();

    const user1 = userEvent.setup();
    const exportButton = await screen.findByRole('button', { name: /导出 PPTX/ });
    expect((exportButton as HTMLButtonElement).disabled).toBe(true);

    const textarea = await screen.findByPlaceholderText(/把汇报正文整段贴进来即可/);
    fireEvent.change(textarea, { target: { value: '本月完成库存收尾' } });
    await user1.click(screen.getByRole('button', { name: /生成幻灯片/ }));
    await screen.findByText('1 页 | txt');

    await waitFor(() => expect((exportButton as HTMLButtonElement).disabled).toBe(false));
    await user1.click(exportButton);

    await waitFor(() => expect(exportBodies).toHaveLength(1));
    // 导出的是当前展示的那一份 deck
    expect((exportBodies[0].deck as { pages: unknown[] }).pages).toHaveLength(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
  });

  it('决策助手走 decision 能力分发，渲染出原页面而不依赖旧路由', async () => {
    // DecisionAssistantPage 挂载即探测 laya health；统一给个可达响应
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/laya/health')) {
        return jsonResponse({ reachable: true, status: 'ok' });
      }
      return jsonResponse({});
    }));
    renderPage('decision-assistant');

    // 页面标题（AppHeader，与 PPT Studio 统一口径）与「API 接入」入口都在
    expect(await screen.findByText('Agent 广场 · 决策助手')).toBeTruthy();
    expect(await screen.findByRole('button', { name: /API 接入/ })).toBeTruthy();
    expect(await screen.findByText('← 返回 Agent 广场')).toBeTruthy();
  });

  it('AI CodeReviewer 走 code-review 能力分发，外壳与其他 Agent 统一', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    renderPage('code-reviewer', adminUser);

    // 页面标题（AppHeader，与其他 Agent 统一口径）与右上角返回入口都在
    expect(await screen.findByText('Agent 广场 · AI CodeReviewer')).toBeTruthy();
    expect(await screen.findByText('← 返回 Agent 广场')).toBeTruthy();
  });

  it('AI CodeReviewer 是 adminOnly：普通成员深链直进会被弹回广场', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    renderPage('code-reviewer');

    expect(await screen.findByText('Agent 广场列表')).toBeTruthy();
    expect(screen.queryByText('Agent 广场 · AI CodeReviewer')).toBeNull();
  });

  it('OA 浏览器代填走 oa-assistant 能力分发：给出可拖拽的 javascript 书签', async () => {
    const jsonCapableResponse = (body: unknown): Response =>
      ({
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => body,
        text: async () => JSON.stringify(body ?? {}),
      }) as unknown as Response;
    vi.stubGlobal('fetch', vi.fn(async () => jsonCapableResponse({
      scenarios: [{ id: 'communication', name: '通讯费用', enabled: true, reason_prefill: '{last_month}月通讯费报销' }],
    })));
    renderPage('oa-assistant');

    expect(await screen.findByText('Agent 广场 · AI 单据助手')).toBeTruthy();
    expect(await screen.findByText('AI 单据助手')).toBeTruthy();
    // 书签本体是 javascript: loader，且指向后端下发的 panel.js
    await waitFor(() => {
      const link = document.querySelector('a[title="拖我到书签栏"]') as HTMLAnchorElement | null;
      expect(link?.getAttribute('href')?.startsWith('javascript:')).toBe(true);
      expect(link?.getAttribute('href')).toContain('/api/enterprise/oa-assistant/panel.js');
    });
  });

  it('未知 entry 退化为提示页而不是白屏', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    renderPage('not-in-catalog');
    expect(await screen.findByText('该 Agent 暂时无法打开')).toBeTruthy();
    expect(screen.getByText(/清单里没有这个 Agent/)).toBeTruthy();
  });
});
