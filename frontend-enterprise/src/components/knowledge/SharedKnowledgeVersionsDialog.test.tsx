// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { KnowledgeBaseRead, KnowledgeBaseVersionRead } from '@/types';

import { SharedKnowledgeVersionsDialog } from './SharedKnowledgeVersionsDialog';

const knowledgeBase: KnowledgeBaseRead = {
  id: 'kb-shared',
  tenant_id: 'tenant_demo',
  name: '团队选题库',
  status: 'active',
  mode: 'shared',
  published_version_id: 'kbver-release',
  published_version: '1.0.0',
  document_count: 1,
  bucket_count: 2,
  chunk_count: 3,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const versions: KnowledgeBaseVersionRead[] = [
  {
    id: 'kbver-draft',
    tenant_id: 'tenant_demo',
    knowledge_base_id: 'kb-shared',
    version: '1.1.0',
    name: '团队选题库',
    status: 'active',
    publication_state: 'draft',
    parent_version_id: 'kbver-release',
    source_team_id: 'team-content',
    created_by_user_id: 'user-admin',
    change_reason: '补充本周选题',
    is_published_head: false,
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
  },
  {
    id: 'kbver-rejected',
    tenant_id: 'tenant_demo',
    knowledge_base_id: 'kb-shared',
    version: '1.2.0',
    name: '团队选题库',
    status: 'active',
    publication_state: 'rejected',
    parent_version_id: 'kbver-release',
    source_team_id: 'team-content',
    change_reason: '来源不足',
    is_published_head: false,
    created_at: '2026-08-03T00:00:00Z',
    updated_at: '2026-08-03T00:00:00Z',
  },
  {
    id: 'kbver-release',
    tenant_id: 'tenant_demo',
    knowledge_base_id: 'kb-shared',
    version: '1.0.0',
    name: '团队选题库',
    status: 'active',
    publication_state: 'released',
    change_reason: '初始发布',
    is_published_head: true,
    published_at: '2026-08-01T00:00:00Z',
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

const auditPage = {
  items: [
    {
      id: 'audit-publish',
      knowledge_base_id: 'kb-shared',
      team_id: 'team-content',
      team_name: '内容团队',
      knowledge_base_version_id: 'kbver-draft',
      knowledge_base_version: '1.1.0',
      actor_type: 'agent',
      actor_id: 'agent-publisher',
      actor_name: '发布员工',
      action: 'version_published',
      reason: '通过审校后发布',
      details: {
        previous_version_id: 'kbver-release',
        published_version_id: 'kbver-draft',
        source_task_id: 'task-publish',
      },
      created_at: '2026-08-03T08:00:00Z',
    },
    {
      id: 'audit-grant',
      knowledge_base_id: 'kb-shared',
      team_id: 'team-content',
      team_name: '内容团队',
      knowledge_base_version_id: null,
      knowledge_base_version: null,
      actor_type: 'user',
      actor_id: 'user-admin',
      actor_name: '知识管理员',
      action: 'grant_changed',
      reason: '允许负责人发布',
      details: {
        agent_id: 'agent-publisher',
        previous_permission: 'editor',
        current_permission: 'publisher',
      },
      created_at: '2026-08-02T08:00:00Z',
    },
  ],
  total: 2,
  offset: 0,
  limit: 20,
  has_more: false,
};

function jsonResponse(body: unknown): Response {
  /** 构造成功的知识版本接口响应。 */
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function conflictResponse(): Response {
  /** 构造发布 CAS 冲突响应。 */
  return {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    text: async () => JSON.stringify({
      detail: {
        code: 'KNOWLEDGE_PUBLISH_CONFLICT',
        message: '正式版本已变化',
      },
    }),
  } as Response;
}

function renderDialog() {
  /** 用一个可管理团队渲染共享版本对话框。 */
  return render(
    <I18nProvider>
      <SharedKnowledgeVersionsDialog
        open
        knowledgeBase={knowledgeBase}
        teamOptions={[{ id: 'team-content', name: '内容团队' }]}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />
    </I18nProvider>,
  );
}

beforeAll(() => {
  // Radix Dialog 在 jsdom 中需要这些浏览器 API。
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('SharedKnowledgeVersionsDialog', () => {
  it('renders lifecycle history and creates a draft from the current global release', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') return jsonResponse(versions[0]);
      if (String(input).includes('/audit-events')) return jsonResponse(auditPage);
      return jsonResponse(versions);
    });
    vi.stubGlobal('fetch', fetchMock);
    renderDialog();

    const dialog = await screen.findByRole('dialog', { name: /共享版本：团队选题库/ });
    expect(within(dialog).getByText('正式版本')).toBeTruthy();
    expect(within(dialog).getByText('草稿')).toBeTruthy();
    expect(within(dialog).getByText('已驳回')).toBeTruthy();
    expect(within(dialog).getByText('当前正式')).toBeTruthy();

    await user.type(within(dialog).getByLabelText('变更原因'), '准备新一轮选题');
    await user.click(within(dialog).getByRole('button', { name: '创建草稿' }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([input, init]) => (
        String(input).endsWith('/knowledge-bases/kb-shared/drafts')
        && init?.method === 'POST'
      ));
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        tenant_id: 'tenant_demo',
        team_id: 'team-content',
        change_reason: '准备新一轮选题',
        expected_published_version_id: 'kbver-release',
      });
    });
  });

  it('shows a safe conflict message and reloads history after stale publication', async () => {
    const user = userEvent.setup();
    let versionReads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST' && String(input).endsWith('/versions/kbver-draft/publish')) {
        return conflictResponse();
      }
      if (String(input).includes('/audit-events')) return jsonResponse(auditPage);
      if (String(input).includes('/knowledge-bases/kb-shared/versions?')) versionReads += 1;
      return jsonResponse(versions);
    });
    vi.stubGlobal('fetch', fetchMock);
    renderDialog();

    const dialog = await screen.findByRole('dialog', { name: /共享版本：团队选题库/ });
    await user.type(within(dialog).getByLabelText('变更原因'), '确认发布');
    await user.click(within(dialog).getByRole('button', { name: '发布 1.1.0' }));

    expect((await within(dialog).findByRole('alert')).textContent).toContain(
      '正式版本已变化，请基于最新版本重新操作。',
    );
    await waitFor(() => expect(versionReads).toBeGreaterThanOrEqual(2));
  });

  it('disables publish and rollback when the server returns no published head', async () => {
    const user = userEvent.setup();
    const headlessVersions = versions.map((version) => ({
      ...version,
      is_published_head: false,
    }));
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => (
      String(input).includes('/audit-events')
        ? jsonResponse(auditPage)
        : jsonResponse(headlessVersions)
    )));
    renderDialog();

    const dialog = await screen.findByRole('dialog', { name: /共享版本：团队选题库/ });
    await user.type(within(dialog).getByLabelText('变更原因'), '等待正式版本恢复');

    expect((within(dialog).getByRole('button', { name: '发布 1.1.0' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByRole('button', { name: '回滚到 1.0.0' }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByRole('button', { name: '驳回 1.1.0' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('renders audit provenance, permission transitions, and server-side filters', async () => {
    /** 审计页必须说明谁、在哪个团队、因何修改了哪个版本或权限状态。 */
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/audit-events')) return jsonResponse(auditPage);
      return jsonResponse(versions);
    });
    vi.stubGlobal('fetch', fetchMock);
    renderDialog();

    const dialog = await screen.findByRole('dialog', { name: /共享版本：团队选题库/ });
    await user.click(within(dialog).getByRole('tab', { name: '审计历史' }));

    expect(await within(dialog).findByText('通过审校后发布')).toBeTruthy();
    expect(within(dialog).getByText(/发布员工.*Agent/)).toBeTruthy();
    expect(within(dialog).getAllByText('内容团队').length).toBeGreaterThanOrEqual(1);
    expect(within(dialog).getByText('v1.0.0 → v1.1.0')).toBeTruthy();
    expect(within(dialog).getByText('来源任务：task-publish')).toBeTruthy();
    expect(within(dialog).getByText('可编辑 → 可发布')).toBeTruthy();

    await user.selectOptions(within(dialog).getByLabelText('审计动作'), 'version_published');
    await user.selectOptions(within(dialog).getByLabelText('操作者类型'), 'agent');
    await user.selectOptions(within(dialog).getByLabelText('审计团队'), 'team-content');

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => {
        const url = String(input);
        return url.includes('/audit-events?')
          && url.includes('action=version_published')
          && url.includes('actor_type=agent')
          && url.includes('team_id=team-content');
      })).toBe(true);
    });
  });
});
