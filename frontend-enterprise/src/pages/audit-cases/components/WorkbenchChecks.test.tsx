// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditCaseDocumentRead } from '@/types';
import { createDocumentCheck, loadDocumentChecks, retryDocumentCheck, type CheckJob } from '../workbenchApi';
import { WorkbenchChecks } from './WorkbenchChecks';

vi.mock('../workbenchApi', () => ({ createDocumentCheck: vi.fn(), loadDocumentChecks: vi.fn(), retryDocumentCheck: vi.fn() }));
const primary = { id: 'primary', title: '主文件', active_version_id: 'v1' } as AuditCaseDocumentRead;
const reference = { id: 'ref', title: '关联文件', active_version_id: 'v2', status: 'archived', active_version: { version: 2 } } as AuditCaseDocumentRead;
const job = { id: 'job', document_id: 'primary', document_version_id: 'v1', status: 'failed', reference_versions: [{ document_id: 'ref', document_version_id: 'v2' }], findings: [{ code: 'mismatch', severity: 'error', title: '企业名称不一致', detail: '关联文件标注不同', document_id: 'ref', document_version_id: 'v2', evidence_excerpt: '企业名称：乙' }], rule_snapshot: [], error_code: 'INTERRUPTED', stale: false, created_at: '2026-09-06' } as CheckJob;
beforeEach(() => { vi.mocked(loadDocumentChecks).mockResolvedValue([job]); vi.mocked(retryDocumentCheck).mockResolvedValue(job); vi.mocked(createDocumentCheck).mockResolvedValue(job); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe('persisted checks panel', () => {
  it('selects explicit archived references, shows source evidence and retries a stored failure', async () => {
    const register = vi.fn().mockResolvedValue(undefined);
    render(<WorkbenchChecks caseId="case" documentId="primary" documents={[primary, reference]} canRun onRegister={register} />);
    expect(await screen.findByText('企业名称：乙')).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: /关联文件/ }));
    fireEvent.click(screen.getByRole('button', { name: '检查保存版本' }));
    await waitFor(() => expect(createDocumentCheck).toHaveBeenCalledWith('case', 'primary', ['ref']));
    await waitFor(() => expect((screen.getByRole('button', { name: '重试或恢复检查' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '重试或恢复检查' }));
    await waitFor(() => expect(retryDocumentCheck).toHaveBeenCalledWith('case', 'job'));
    await waitFor(() => expect((screen.getByRole('button', { name: '登记为检查问题' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '登记为检查问题' }));
    await waitFor(() => expect(register).toHaveBeenCalledWith(job.findings[0]));
  });
  it('marks stale findings and blocks registering them as current issues', async () => {
    vi.mocked(loadDocumentChecks).mockResolvedValue([{ ...job, stale: true, status: 'completed' }]);
    render(<WorkbenchChecks caseId="case" documentId="primary" documents={[primary, reference]} canRun onRegister={vi.fn()} />);
    expect(await screen.findByText('版本已变化，需重新检查')).toBeTruthy();
    expect((screen.getByRole('button', { name: '登记为检查问题' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
