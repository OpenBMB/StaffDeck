// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { KnowledgeBaseRead, KnowledgeBaseVersionRead, TeamRead } from '@/types';

import { SharedKnowledgeConversionDialog } from './SharedKnowledgeConversionDialog';

const sourceKnowledgeBase: KnowledgeBaseRead = {
  id: 'kb-dedicated',
  tenant_id: 'tenant_demo',
  name: '个人素材库',
  description: '长期积累的个人内容资料',
  status: 'active',
  mode: 'dedicated',
  version: '1.2.0',
  branch_head_version: '1.2.0',
  document_count: 3,
  bucket_count: 8,
  chunk_count: 21,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-02T00:00:00Z',
};

const sourceVersions: KnowledgeBaseVersionRead[] = [
  {
    id: 'kbver-head',
    tenant_id: 'tenant_demo',
    knowledge_base_id: sourceKnowledgeBase.id,
    version: '1.2.0',
    name: sourceKnowledgeBase.name,
    status: 'active',
    is_head: true,
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
  },
  {
    id: 'kbver-old',
    tenant_id: 'tenant_demo',
    knowledge_base_id: sourceKnowledgeBase.id,
    version: '1.1.0',
    name: sourceKnowledgeBase.name,
    status: 'active',
    is_head: false,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

const teams: TeamRead[] = [
  {
    id: 'team-content',
    tenant_id: 'tenant_demo',
    name: '内容团队',
    owner_user_id: 'user-admin',
    config: {},
    status: 'active',
    members: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
  {
    id: 'team-growth',
    tenant_id: 'tenant_demo',
    name: '增长团队',
    owner_user_id: 'user-admin',
    config: {},
    status: 'active',
    members: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

const conversionResponse = {
  source_knowledge_base_id: sourceKnowledgeBase.id,
  source_version_id: 'kbver-head',
  new_knowledge_base: {
    ...sourceKnowledgeBase,
    id: 'kb-shared-new',
    name: '团队内容中台',
    mode: 'shared' as const,
    published_version_id: 'kbver-release',
    published_version: '1.0.0',
    bound_team_count: 1,
  },
  released_version: {
    ...sourceVersions[0],
    id: 'kbver-release',
    knowledge_base_id: 'kb-shared-new',
    version: '1.0.0',
    publication_state: 'released' as const,
    is_head: false,
    is_published_head: true,
  },
  binding_ids: ['teamkb-content'],
  default_for_team_id: 'team-content',
  source_archived: true,
  audit_event_id: 'audit-converted',
};

function jsonResponse(body: unknown): Response {
  /** 构造转换向导测试使用的成功响应。 */
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function errorResponse(): Response {
  /** 构造转换校验失败响应，验证来源保留提示不会被后端消息覆盖。 */
  return {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    text: async () => JSON.stringify({
      detail: {
        code: 'KNOWLEDGE_CONVERSION_VALIDATION_FAILED',
        message: '目标资产数量校验失败',
      },
    }),
  } as Response;
}

function renderDialog(onConverted = vi.fn()) {
  /** 以一个活动员工专用分支渲染转换向导。 */
  const onClose = vi.fn();
  render(
    <I18nProvider>
      <SharedKnowledgeConversionDialog
        open
        knowledgeBase={sourceKnowledgeBase}
        agentId="agent-source"
        onClose={onClose}
        onConverted={onConverted}
      />
    </I18nProvider>,
  );
  return { onClose, onConverted };
}

beforeAll(() => {
  // Radix Dialog 和 Checkbox 在 jsdom 中需要这些浏览器 API。
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
});

afterEach(() => {
  /** 隔离每个向导用例的 DOM、fetch 与 spy。 */
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('SharedKnowledgeConversionDialog', () => {
  it('previews the source and submits selected version, teams, and default team', async () => {
    /** 成功转换必须发送完整契约，并回传新共享谱系供页面定位。 */
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') return jsonResponse(conversionResponse);
      if (url.includes('/versions?')) return jsonResponse(sourceVersions);
      if (url.includes('/teams?')) return jsonResponse(teams);
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const onConverted = vi.fn();
    renderDialog(onConverted);

    const dialog = await screen.findByRole('dialog', { name: /转换为共享知识库：个人素材库/ });
    const sourcePreview = within(dialog).getByLabelText('转换来源预览');
    expect(sourcePreview.textContent).toContain('3个文档');
    expect(sourcePreview.textContent).toContain('8个知识节点');
    expect(sourcePreview.textContent).toContain('21个引用片段');
    expect(within(dialog).getByText(/只归档当前员工的专用实例/)).toBeTruthy();

    expect((within(dialog).getByLabelText('来源版本') as HTMLSelectElement).value).toBe('kbver-head');
    await user.clear(within(dialog).getByLabelText('共享知识库名称'));
    await user.type(within(dialog).getByLabelText('共享知识库名称'), '团队内容中台');
    await user.clear(within(dialog).getByLabelText('共享知识库描述'));
    await user.type(within(dialog).getByLabelText('共享知识库描述'), '统一沉淀团队内容方法');
    await user.type(within(dialog).getByLabelText('转换原因'), '将成熟内容方法开放给团队');
    await user.click(within(dialog).getByRole('checkbox', { name: '绑定 内容团队' }));
    await user.selectOptions(within(dialog).getByLabelText('默认写入团队'), 'team-content');
    await user.click(within(dialog).getByRole('button', { name: '确认转换' }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => (
        String(input).endsWith('/knowledge-bases/kb-dedicated/convert-to-shared')
        && init?.method === 'POST'
      ));
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        tenant_id: 'tenant_demo',
        agent_id: 'agent-source',
        source_version_id: 'kbver-head',
        name: '团队内容中台',
        description: '统一沉淀团队内容方法',
        change_reason: '将成熟内容方法开放给团队',
        team_bindings: ['team-content'],
        default_for_team_id: 'team-content',
      });
      expect(onConverted).toHaveBeenCalledWith(conversionResponse);
    });
  });

  it('states that the dedicated source remains usable when conversion fails', async () => {
    /** 失败不能含糊提示，必须明确来源未归档且不会暴露半成品共享库。 */
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST') return errorResponse();
      if (url.includes('/versions?')) return jsonResponse(sourceVersions);
      if (url.includes('/teams?')) return jsonResponse(teams);
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const onConverted = vi.fn();
    renderDialog(onConverted);

    const dialog = await screen.findByRole('dialog', { name: /转换为共享知识库：个人素材库/ });
    await user.type(within(dialog).getByLabelText('转换原因'), '验证转换失败保护');
    await user.click(within(dialog).getByRole('button', { name: '确认转换' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('转换失败');
    expect(alert.textContent).toContain('来源专用知识库仍保持可用，尚未归档');
    expect(alert.textContent).toContain('不会显示未完成的共享知识库');
    expect(onConverted).not.toHaveBeenCalled();
  });
});
