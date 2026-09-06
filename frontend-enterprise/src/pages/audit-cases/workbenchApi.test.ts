// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createDocumentCheck, createWorkIssue, loadDocumentChecks, loadWorkbenchInbox, reportToWorkDocument, retryDocumentCheck, setWorkbenchRole, transitionWorkItem, type WorkItem } from './workbenchApi';

afterEach(() => vi.unstubAllGlobals());
describe('workbench API contracts', () => {
  it('sends immutable revision and explicit idempotency key with an encoded scoped route', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => '{}' } as Response)); vi.stubGlobal('fetch', fetchMock);
    await transitionWorkItem('case/a', { id: 'item/b', revision: 7 } as WorkItem, 'approve', '复核依据', 'same-request');
    const [input, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(input).toContain('/cases/case%2Fa/items/item%2Fb/transition?tenant_id=');
    expect(JSON.parse(String(options.body))).toEqual({ action: 'approve', expected_revision: 7, request_key: 'same-request', comment: '复核依据' });
  });
  it('persists explicit check references, retries and never promotes a check into an NC', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => '{}' } as Response)); vi.stubGlobal('fetch', fetchMock);
    await createDocumentCheck('case', 'doc', ['reference'], 'check-key'); await retryDocumentCheck('case', 'job', 'retry-key');
    await createWorkIssue('case', { work_item_id: 'item', kind: 'document_check', title: '缺少字段', detail: '原文证据', blocking: true, assigned_to_user_id: 'editor' });
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(JSON.parse(String(calls[0][1].body))).toEqual({ document_id: 'doc', reference_document_ids: ['reference'], request_key: 'check-key' });
    expect(calls[1][0]).toContain('/checks/job/retry?'); expect(JSON.parse(String(calls[1][1].body)).request_key).toBe('retry-key');
    expect(JSON.parse(String(calls[2][1].body)).kind).toBe('document_check');
  });
  it('reads inbox/checks and sends member role/report export to scoped endpoints', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => '{}' } as Response)); vi.stubGlobal('fetch', fetchMock);
    await loadWorkbenchInbox(); await loadDocumentChecks('case', 'doc/x'); await setWorkbenchRole('case', 'member', 'viewer'); await reportToWorkDocument('case', 'report');
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toContain('/inbox?tenant_id='); expect(calls[1][0]).toContain('document_id=doc%2Fx');
    expect(calls[2][1].method).toBe('PUT'); expect(calls[3][0]).toContain('/reports/report/work-document?');
  });
});
