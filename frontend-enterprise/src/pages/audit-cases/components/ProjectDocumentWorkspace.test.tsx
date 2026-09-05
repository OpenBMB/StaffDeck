// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import { ProjectDocumentWorkspace } from './ProjectDocumentWorkspace';

const baseDocument = {
  id: 'document-1',
  audit_case_id: 'case-1',
  document_key: 'audit-plan',
  title: '审核计划',
  document_type: 'audit_plan',
  zone: 'workspace',
  status: 'active',
  active_version_id: 'version-1',
  source_material_id: null,
  archive_reason: null,
  created_by_user_id: 'owner-1',
  updated_by_user_id: 'owner-1',
  created_at: '2026-09-05T00:00:00Z',
  updated_at: '2026-09-05T00:00:00Z',
  active_version: {
    id: 'version-1',
    document_id: 'document-1',
    version: 1,
    content_format: 'markdown',
    content: '# 原始草稿\n',
    content_sha256: 'sha-1',
    characters: 7,
    change_note: '创建',
    created_by_user_id: 'owner-1',
    created_at: '2026-09-05T00:00:00Z',
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 409 ? 'Conflict' : 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

function renderWorkspace(documents = [baseDocument], disabled = false) {
  return render(
    <I18nProvider>
      <ProjectDocumentWorkspace
        caseId="case-1"
        documents={documents}
        disabled={disabled}
        onChanged={vi.fn()}
      />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ProjectDocumentWorkspace', () => {
  it('renders documents by zone and saves the next immutable version', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/documents/document-1') && !url.includes('/versions')) {
        return jsonResponse({ document: baseDocument, versions: [baseDocument.active_version] });
      }
      if (init?.method === 'POST' && url.includes('/versions')) {
        return jsonResponse({ ...baseDocument.active_version, id: 'version-2', version: 2, content: '第二版内容' });
      }
      return jsonResponse([baseDocument]);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWorkspace();
    expect(screen.getAllByText('审核计划').length).toBeGreaterThanOrEqual(1);
    await user.click(screen.getByRole('button', { name: '保存新版本' }));

    const versionCall = fetchMock.mock.calls.find(([input, init]) => (
      String(input).includes('/versions') && init?.method === 'POST'
    ));
    expect(versionCall).toBeTruthy();
    expect(String(versionCall?.[1]?.body)).toContain('"expected_version":1');
    expect(String(versionCall?.[1]?.body)).toContain('原始草稿');
    expect(await screen.findByText(/版本 2/)).toBeTruthy();
  });

  it('keeps editor content after a stale version conflict', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/documents/document-1') && !url.includes('/versions')) {
        return jsonResponse({ document: baseDocument, versions: [baseDocument.active_version] });
      }
      if (init?.method === 'POST' && url.includes('/versions')) {
        return jsonResponse({ detail: 'DOCUMENT_VERSION_CONFLICT' }, 409);
      }
      return jsonResponse([baseDocument]);
    }));

    renderWorkspace();
    const editor = await screen.findByRole('textbox', { name: '文档内容' });
    await user.clear(editor);
    await user.type(editor, '用户正在编辑的内容');
    await user.click(screen.getByRole('button', { name: '保存新版本' }));

    expect((await screen.findByRole('alert')).textContent).toContain('文档版本已变化');
    expect((editor as HTMLTextAreaElement).value).toBe('用户正在编辑的内容');
  });

  it('creates a work document from the project workspace', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/documents') && !url.includes('/versions')) {
        return jsonResponse(baseDocument, 201);
      }
      if (url.includes('/documents/document-1') && !url.includes('/versions')) {
        return jsonResponse({ document: baseDocument, versions: [baseDocument.active_version] });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderWorkspace([]);
    await user.type(screen.getByRole('textbox', { name: '文档标识' }), 'audit-plan');
    await user.type(screen.getByRole('textbox', { name: '文档标题' }), '审核计划');
    await user.type(screen.getByRole('textbox', { name: '初始内容' }), '# 初始审核计划');
    await user.click(screen.getByRole('button', { name: '创建文档' }));

    const createCall = fetchMock.mock.calls.find(([input, init]) => (
      String(input).includes('/documents?') && init?.method === 'POST'
    ));
    expect(createCall).toBeTruthy();
    expect(String(createCall?.[1]?.body)).toContain('"document_key":"audit-plan"');
    expect(String(createCall?.[1]?.body)).toContain('初始审核计划');
    expect((await screen.findAllByText('审核计划')).length).toBeGreaterThanOrEqual(1);
  });

  it('disables editing for archived project or archived document', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ document: { ...baseDocument, status: 'archived' }, versions: [baseDocument.active_version] })));
    renderWorkspace([{ ...baseDocument, status: 'archived' }], true);

    expect((await screen.findByRole('textbox', { name: '文档内容' }) as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '保存新版本' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '冻结文档' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '创建文档' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
