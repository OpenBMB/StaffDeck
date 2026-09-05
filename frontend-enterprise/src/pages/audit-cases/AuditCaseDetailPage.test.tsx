// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import AuditCaseDetailPage from './AuditCaseDetailPage';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

const baseCase = {
  id: 'case-1',
  tenant_id: 'tenant_demo',
  agent_id: null as string | null,
  knowledge_scope_mode: 'custom' as 'agent_default' | 'custom',
  owner_user_id: 'user-admin',
  member_user_ids: [],
  organization_name: '示例企业',
  report_type: '再认证',
  management_systems: ['能源管理体系'],
  status: 'collecting',
  knowledge_base_version_ids: [] as string[],
  active_report_version_id: null,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function renderAt(path: string) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/enterprise/audit-cases/:caseId" element={<AuditCaseDetailPage />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

function stubFetch(caseData = baseCase) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/management-options')) {
      return jsonResponse({ knowledge_versions: [], supported_extensions: ['.docx'], max_material_bytes: 50 * 1024 * 1024 });
    }
    if (url.includes('/api/auth/users')) {
      return jsonResponse([{ id: 'user-member', username: 'member', display_name: '成员账号', role: 'member', source: 'web' }]);
    }
    if (url.includes('/materials')) return jsonResponse([]);
    if (url.includes('/coverage')) return jsonResponse({
      current_material_count: 0,
      successful_material_count: 0,
      failed_material_count: 0,
      total_chunk_count: 0,
      successful_chunk_count: 0,
      file_coverage: 0,
      chunk_coverage: 0,
      element_coverage: 0,
      publish_allowed: false,
      blockers: ['暂无材料'],
      files_total: 0,
      files_succeeded: 0,
      chunks_total: 0,
      chunks_succeeded: 0,
      elements_total: 0,
      elements_resolved: 0,
    });
    if (url.includes('/events')) return jsonResponse([]);
    if (init?.method === 'PUT' && url.includes('/members')) return jsonResponse({ ...caseData, member_user_ids: ['user-member'] });
    if (init?.method === 'PATCH') return jsonResponse(caseData);
    return jsonResponse(caseData);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AuditCaseDetailPage', () => {
  it('loads detail data and saves members as a complete list', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch();
    renderAt('/enterprise/audit-cases/case-1');

    expect(await screen.findByText('示例企业')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: '项目成员' }));
    await user.click(screen.getByRole('checkbox', { name: '成员账号' }));
    await user.click(screen.getByRole('button', { name: '保存成员' }));

    expect(fetchMock.mock.calls.some(([, init]) => (
      init?.method === 'PUT' && String(init.body).includes('user-member')
    ))).toBe(true);
  });

  it('does not allow an archived project to submit edits', async () => {
    stubFetch({ ...baseCase, id: 'case-archived', status: 'archived' });
    renderAt('/enterprise/audit-cases/case-archived');

    expect(await screen.findByText('已归档')).toBeTruthy();
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps the frozen knowledge snapshot collapsed and does not rewrite it on an ordinary save', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch({
      ...baseCase,
      agent_id: 'agent-energy',
      knowledge_scope_mode: 'agent_default',
      knowledge_base_version_ids: ['kbver-energy'],
    });
    renderAt('/enterprise/audit-cases/case-1');

    expect(await screen.findByText('已冻结 1 个知识库版本')).toBeTruthy();
    expect(screen.queryByRole('searchbox', { name: '搜索知识库' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '保存' }));

    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH');
    expect(patchCall).toBeTruthy();
    expect(String(patchCall?.[1]?.body)).not.toContain('knowledge_base_version_ids');
  });
});
