// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createAuditCase,
  listManagedAuditCases,
  loadAuditCaseManagementOptions,
  processAuditCaseMaterial,
  replaceAuditCaseMaterial,
  uploadAuditCaseMaterials,
} from './auditCaseApi';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('audit case management api', () => {
  it('loads a management page with encoded filters', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ items: [], total: 0 }));
    vi.stubGlobal('fetch', fetchMock);

    await listManagedAuditCases({
      query: '示例企业 / A',
      status: 'active',
      offset: 20,
      limit: 20,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/audit-cases/management?tenant_id='),
      expect.objectContaining({ headers: expect.any(Object) }),
    );
    const firstCall = fetchMock.mock.calls[0] as unknown as [RequestInfo | URL];
    const requestUrl = String(firstCall[0]);
    expect(requestUrl).toContain('q=%E7%A4%BA%E4%BE%8B%E4%BC%81%E4%B8%9A+%2F+A');
    expect(requestUrl).toContain('status=active');
    expect(requestUrl).toContain('offset=20');
    expect(requestUrl).toContain('limit=20');
  });

  it('uses the management options endpoint and posts a trimmed project draft', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('management-options')) {
        return jsonResponse({ knowledge_versions: [], supported_extensions: [], max_material_bytes: 1 });
      }
      return jsonResponse({ id: 'case-1' });
    });
    vi.stubGlobal('fetch', fetchMock);

    await loadAuditCaseManagementOptions();
    await createAuditCase({
      tenant_id: 'tenant_demo',
      organization_name: '  示例企业  ',
      report_type: '  再认证  ',
      management_systems: ['能源管理体系'],
      knowledge_base_version_ids: [],
      member_user_ids: [],
    });

    const createCall = fetchMock.mock.calls.find(([input, init]) => (
      String(input).endsWith('/api/audit-cases') && init?.method === 'POST'
    ));
    expect(createCall).toBeTruthy();
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      organization_name: '示例企业',
      report_type: '再认证',
    });
  });

  it('uploads typed materials as multipart and supports single-file process and replace', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ id: 'material-1', material_type: 'audit_record' }));
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['审核记录'], 'record.txt', { type: 'text/plain' });

    await uploadAuditCaseMaterials('case-1', 'audit_record', [file]);
    await processAuditCaseMaterial('case-1', 'material-1');
    await replaceAuditCaseMaterial('case-1', 'material-1', file);

    const uploadCall = fetchMock.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(uploadCall[0])).toContain('/api/audit-cases/case-1/materials?');
    expect(String(uploadCall[0])).toContain('material_type=audit_record');
    expect(uploadCall[1].body).toBeInstanceOf(FormData);
    expect((uploadCall[1].body as FormData).get('files')).toBe(file);
    expect(uploadCall[1].headers).not.toHaveProperty('Content-Type');

    const processCall = fetchMock.mock.calls[1] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(processCall[0])).toContain('/materials/material-1/process');
    expect(processCall[1].method).toBe('POST');

    const replaceCall = fetchMock.mock.calls[2] as unknown as [RequestInfo | URL, RequestInit];
    expect(String(replaceCall[0])).toContain('/materials/material-1/replace');
    expect(replaceCall[1].body).toBeInstanceOf(FormData);
    expect((replaceCall[1].body as FormData).get('file')).toBe(file);
  });
});
