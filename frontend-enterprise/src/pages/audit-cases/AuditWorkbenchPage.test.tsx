// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '@/i18n';
import type { AuditCaseDocumentRead } from '@/types';
import AuditWorkbenchPage from './AuditWorkbenchPage';
import type { WorkbenchSnapshot, WorkItem } from './workbenchApi';
import { lineDiff } from './components/DocumentVersionDiff';

vi.mock('@/components/AppHeader', () => ({ default: ({ left }: { left: React.ReactNode }) => <header>{left}</header> }));
const file = (id: string): AuditCaseDocumentRead => ({ id, audit_case_id: 'case', document_key: id, title: `文件${id}`, document_type: 'work_document', zone: 'workspace', status: 'active', active_version_id: `${id}-v1`, active_version: { id: `${id}-v1`, document_id: id, version: 1, content_format: 'markdown', content: `内容${id}`, content_sha256: '', characters: 3, created_by_user_id: 'editor', created_at: '' }, created_by_user_id: 'editor', updated_by_user_id: 'editor', created_at: '', updated_at: '' });
const files = [file('one'), file('two')];
const baseSnapshot: WorkbenchSnapshot = { case_id: 'case', role: 'editor', members: [{ user_id: 'editor', display_name: '编辑甲', role: 'editor' }, { user_id: 'reviewer', display_name: '复核乙', role: 'reviewer' }], processes: Array.from({ length: 36 }, (_, index) => ({ number: index + 1, name: `流程${index + 1}`, stage: 1, enabled: index === 14 })), work_items: [], issues: [] };
function mount(snapshot = baseSnapshot, userId = 'editor') {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); let body: unknown = [];
    if (url.includes('/audit-workbench/cases/case?')) body = snapshot;
    else if (url.includes('/audit-workbench/cases/case/process-gates?')) body = snapshot.processes.map((process) => ({ process_number: process.number, enabled: process.enabled, ready: process.enabled, blockers: [], predecessors: [], required_reference_document_ids: [], check: null }));
    else if (url.includes('/audit-cases/case?')) body = { id: 'case', organization_name: '测试企业', report_type: '再认证', status: 'active' };
    else if (url.includes('/documents?')) body = files;
    else if (url.includes('/documents/')) { const selected = files.find((f) => url.includes(`/documents/${f.id}?`))!; body = { document: selected, versions: [selected.active_version] }; }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  render(<I18nProvider><MemoryRouter initialEntries={['/enterprise/audit-cases/case/workbench']}><Routes><Route path="/enterprise/audit-cases/:caseId/workbench" element={<AuditWorkbenchPage currentUser={{ id: userId, role: 'member', username: userId, tenant_id: 'tenant_demo' }} />} /><Route path="/enterprise/audit-workbench" element={<p>收件箱页面</p>} /></Routes></MemoryRouter></I18nProvider>);
  return fetchMock;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe('unified workbench', () => {
  it('loads a project member without admin-only management options, and protects file/tab/route changes', async () => {
    const fetchMock = mount();
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/process-gates?'))).toBe(true));
    const editor = await screen.findByRole('textbox', { name: '文档内容' });
    await waitFor(() => expect((editor as HTMLTextAreaElement).value).toBe('内容one'));
    fireEvent.change(editor, { target: { value: '未保存草稿' } });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    fireEvent.click(screen.getByRole('button', { name: /文件two/ }));
    expect((editor as HTMLTextAreaElement).value).toBe('未保存草稿');
    fireEvent.click(screen.getByRole('tab', { name: '版本差异' }));
    expect(screen.getByRole('textbox', { name: '文档内容' })).toBeTruthy();
    fireEvent.click(screen.getByRole('link', { name: /我的待办/ }));
    expect(screen.queryByText('收件箱页面')).toBeNull(); expect(confirm).toHaveBeenCalledTimes(3);
    confirm.mockReturnValue(true); fireEvent.click(screen.getByRole('button', { name: /文件two/ }));
    await waitFor(() => expect((screen.getByRole('textbox', { name: '文档内容' }) as HTMLTextAreaElement).value).toBe('内容two'));
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('management-options'))).toBe(false);
  });

  it('refreshes the document-scoped gate when the selected document changes', async () => {
    const fetchMock = mount();
    await screen.findByRole('textbox', { name: '文档内容' });
    const before = fetchMock.mock.calls.filter(([url]) => String(url).includes('/process-gates?')).length;
    fireEvent.click(screen.getByRole('button', { name: /文件two/ }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/process-gates?')).length).toBeGreaterThan(before));
  });
  it('makes viewer content read-only and does not expose formal issue creation', async () => {
    mount({ ...baseSnapshot, role: 'viewer' });
    expect((await screen.findByRole('textbox', { name: '文档内容' }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: /新建工作文档/ })).toBeNull();
    expect(screen.queryByText('正式不符合项')).toBeNull();
  });
  it('locks submitted documents and prevents approving a stale snapshot', async () => {
    const item = { id: 'item', audit_case_id: 'case', document_id: 'one', process_number: 15, document_version_id: 'one-v1', assigned_to_user_id: 'editor', reviewer_user_id: 'reviewer', submitted_by_user_id: 'editor', status: 'submitted', revision: 2, stale: true, reference_versions: [] } as unknown as WorkItem;
    mount({ ...baseSnapshot, role: 'reviewer', work_items: [item] }, 'reviewer');
    expect((await screen.findByRole('textbox', { name: '文档内容' }) as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '内部复核通过' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('流转说明'), { target: { value: '关联版本已变，请重新确认' } });
    expect((screen.getByRole('button', { name: '退回修改' }) as HTMLButtonElement).disabled).toBe(false);
  });
  it('shows actual added and removed lines while retaining common text', () => {
    const diff = lineDiff('企业：甲\n审核：初审\n相同', '企业：甲\n审核：再认证\n相同');
    expect(diff).toContainEqual({ kind: 'removed', text: '审核：初审' }); expect(diff).toContainEqual({ kind: 'added', text: '审核：再认证' }); expect(diff).toContainEqual({ kind: 'same', text: '相同' });
  });
});
