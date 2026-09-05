// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import AuditCasesPage from './AuditCasesPage';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AuditCasesPage', () => {
  it('shows the create action and management rows for an administrator', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/management-options')) {
        return jsonResponse({
          knowledge_versions: [],
          supported_extensions: ['.docx'],
          max_material_bytes: 50 * 1024 * 1024,
        });
      }
      if (url.includes('/api/auth/users')) return jsonResponse([]);
      return jsonResponse({
        items: [{
          id: 'case-1',
          tenant_id: 'tenant_demo',
          owner_user_id: 'user-admin',
          member_user_ids: [],
          organization_name: '示例企业',
          report_type: '再认证',
          management_systems: ['能源管理体系'],
          status: 'collecting',
          knowledge_base_version_ids: [],
          active_report_version_id: null,
          created_at: '2026-08-01T00:00:00Z',
          updated_at: '2026-08-01T00:00:00Z',
          material_total: 4,
          material_ready: 3,
          material_failed: 1,
          file_coverage: 0.75,
          chunk_coverage: 0.8,
        }],
        total: 1,
      });
    }));

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/enterprise/audit-cases']}>
          <AuditCasesPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByRole('button', { name: '新建认证项目' })).toBeTruthy();
    expect(await screen.findByText('示例企业')).toBeTruthy();
    expect(screen.getByText('材料 4', { exact: false })).toBeTruthy();
  });

  it('shows a clear empty state when no project exists', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/management-options')) {
        return jsonResponse({ knowledge_versions: [], supported_extensions: [], max_material_bytes: 1 });
      }
      if (String(input).includes('/api/auth/users')) return jsonResponse([]);
      return jsonResponse({ items: [], total: 0 });
    }));

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/enterprise/audit-cases']}>
          <AuditCasesPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText('暂无认证项目')).toBeTruthy();
    expect(screen.getByText('创建第一个认证项目')).toBeTruthy();
  });

  it('validates required fields without losing the creation draft', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/management-options')) {
        return jsonResponse({ knowledge_versions: [], supported_extensions: [], max_material_bytes: 1 });
      }
      if (String(input).includes('/api/auth/users')) return jsonResponse([]);
      return jsonResponse({ items: [], total: 0 });
    }));

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/enterprise/audit-cases']}>
          <AuditCasesPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    await user.click(await screen.findByRole('button', { name: '新建认证项目' }));
    await user.click(screen.getByRole('button', { name: '创建项目' }));
    expect((await screen.findByRole('alert')).textContent).toContain('请填写企业名称和审核类型');
  });
});
