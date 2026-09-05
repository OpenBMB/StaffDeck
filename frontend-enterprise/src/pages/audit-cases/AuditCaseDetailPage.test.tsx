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

const publishedRuleSet = {
  id: 'rule-set-1',
  tenant_id: 'tenant_demo',
  key: 'iso-50001',
  name: '能源管理体系规则',
  description: '',
  management_systems: ['能源管理体系'],
  audit_types: ['再认证'],
  business_domain: '能源管理',
  status: 'active',
};

const publishedRuleVersion = {
  id: 'rule-version-1',
  tenant_id: 'tenant_demo',
  rule_set_id: publishedRuleSet.id,
  version: 1,
  status: 'published',
  content_sha256: 'sha-rule-version-1',
  published_by_user_id: 'user-admin',
  published_at: '2026-09-01T00:00:00Z',
};

const currentRuleBinding = {
  id: 'binding-1',
  tenant_id: 'tenant_demo',
  audit_case_id: 'case-archived',
  rule_set_id: publishedRuleSet.id,
  rule_set_version_id: publishedRuleVersion.id,
  selection_source: 'manual',
  status: 'active',
  priority: 1,
  bound_by_user_id: 'user-admin',
  supersedes_binding_id: null,
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
    if (url.includes('/api/rule-sets?')) return jsonResponse([publishedRuleSet]);
    if (url.includes(`/api/rule-sets/${publishedRuleSet.id}/versions`)) return jsonResponse([publishedRuleVersion]);
    if (url.includes('/rule-bindings')) {
      return jsonResponse(caseData.status === 'archived' ? [currentRuleBinding] : []);
    }
    if (url.includes('/reports')) return jsonResponse([]);
    if (url.includes('/documents')) return jsonResponse([]);
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

  it('renders the rule binding panel when the rules tab is selected', async () => {
    const user = userEvent.setup();
    stubFetch();
    renderAt('/enterprise/audit-cases/case-1');

    expect(await screen.findByText('示例企业')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: '规则绑定' }));

    expect(await screen.findByText('规则版本候选')).toBeTruthy();
    expect(screen.getByLabelText('能源管理体系规则 · 版本 1')).toBeTruthy();
  });

  it('renders the project document workspace and loads project documents', async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch();
    renderAt('/enterprise/audit-cases/case-1');

    expect(await screen.findByText('示例企业')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: '项目文件库' }));

    expect(await screen.findByText('文档分区')).toBeTruthy();
    expect(screen.getByRole('button', { name: '创建文档' })).toBeTruthy();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/documents'))).toBe(true);
  });

  it('renders the evidence and report panel from the project detail tab', async () => {
    const user = userEvent.setup();
    stubFetch();
    renderAt('/enterprise/audit-cases/case-1');

    expect(await screen.findByText('示例企业')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: '证据与报告' }));
    expect(await screen.findByText('证据处理')).toBeTruthy();
  });

  it('keeps evidence/report actions disabled for archived projects', async () => {
    const user = userEvent.setup();
    stubFetch({ ...baseCase, id: 'case-archived', status: 'archived' });
    renderAt('/enterprise/audit-cases/case-archived');

    expect(await screen.findByText('已归档')).toBeTruthy();
    await user.click(screen.getByRole('tab', { name: '证据与报告' }));
    expect((await screen.findByRole('button', { name: '处理证据' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('does not allow an archived project to submit edits', async () => {
    const user = userEvent.setup();
    stubFetch({ ...baseCase, id: 'case-archived', status: 'archived' });
    renderAt('/enterprise/audit-cases/case-archived');

    expect(await screen.findByText('已归档')).toBeTruthy();
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole('tab', { name: '规则绑定' }));

    expect(await screen.findByText('项目已归档，规则绑定不可修改')).toBeTruthy();
    expect((screen.getByLabelText('能源管理体系规则 · 版本 1') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '预览迁移影响' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '确认迁移' }) as HTMLButtonElement).disabled).toBe(true);
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
