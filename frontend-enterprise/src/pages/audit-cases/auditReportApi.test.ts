// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createAuditCaseReport,
  downloadAuditCaseReport,
  listAuditCaseReports,
  publishAuditCaseReport,
} from './auditReportApi';
import { processAuditCaseEvidence } from './auditCaseApi';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
    blob: async () => new Blob(['docx']),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('audit report api', () => {
  it('posts evidence processing with the tenant and optional model config', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ status: 'succeeded', materials: [], coverage: {} }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await processAuditCaseEvidence('case/one', 'model-1');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/audit-cases/case%2Fone/process?tenant_id=tenant_demo',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ model_config_id: 'model-1' }),
      }),
    );
  });

  it('lists, creates, publishes, and downloads report versions with scoped paths', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ id: 'report-1', sections: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listAuditCaseReports('case/one');
    await createAuditCaseReport('case/one');
    await publishAuditCaseReport('case/one', 'report/1');
    await downloadAuditCaseReport('case/one', 'report/1');

    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      '/api/audit-cases/case%2Fone/reports?tenant_id=tenant_demo',
      '/api/audit-cases/case%2Fone/reports?tenant_id=tenant_demo',
      '/api/audit-cases/case%2Fone/reports/report%2F1/publish?tenant_id=tenant_demo',
      '/api/audit-cases/case%2Fone/reports/report%2F1/download?tenant_id=tenant_demo',
    ]);
    const createCall = fetchMock.mock.calls[1] as unknown as [RequestInfo | URL, RequestInit];
    expect(createCall[1].body).toBe(JSON.stringify({ publish: false }));
    const publishCall = fetchMock.mock.calls[2] as unknown as [RequestInfo | URL, RequestInit];
    expect(publishCall[1].body).toBe('{}');
  });

  it('sends the selected document snapshot when creating a scoped report', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ id: 'report-1', sections: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await createAuditCaseReport(
      'case/one',
      undefined,
      'document/1',
      'document-version/2',
    );

    const [, init] = fetchMock.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      source_document_id: 'document/1',
      source_document_version_id: 'document-version/2',
      publish: false,
    });
  });
});
