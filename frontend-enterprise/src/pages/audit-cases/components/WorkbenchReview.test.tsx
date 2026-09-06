// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AuditCaseDocumentRead } from '@/types';
import { WorkbenchReview } from './WorkbenchReview';
import type { ProcessGate, WorkbenchSnapshot } from '../workbenchApi';

const document = { id: 'doc', title: '审核方案', document_type: 'work_document', status: 'active' } as AuditCaseDocumentRead;
const snapshot = (processNumber: number): WorkbenchSnapshot => ({
  case_id: 'case',
  role: 'editor',
  members: [
    { user_id: 'editor', display_name: '编辑甲', role: 'editor' },
    { user_id: 'reviewer', display_name: '复核乙', role: 'reviewer' },
  ],
  processes: [{ number: processNumber, name: `流程${processNumber}`, stage: 2, enabled: true }],
  work_items: [],
  issues: [],
});

const gate = (processNumber: number, ready: boolean, predecessorNumber: number): ProcessGate => ({
  process_number: processNumber,
  enabled: true,
  ready,
  blockers: ready ? [] : [{ code: 'PROCESS_PRECONDITION_REQUIRED', message: `流程 ${predecessorNumber} 尚未内部复核通过。`, process_number: predecessorNumber }],
  predecessors: [{ number: predecessorNumber, name: `流程${predecessorNumber}`, status: ready ? 'approved' : 'missing', approved: ready, work_item_id: ready ? 'item' : null, document_id: ready ? 'doc' : null, document_version_id: ready ? 'doc-v1' : null }],
  required_reference_document_ids: [],
  check: null,
});

function renderReview(processNumber: number, processGate: ProcessGate) {
  return render(<WorkbenchReview caseId="case" userId="editor" snapshot={snapshot(processNumber)} documentId="doc" documents={[document]} processNumber={processNumber} gate={processGate} blocked={false} run={vi.fn(async () => undefined)} />);
}

describe('WorkbenchReview process gates', () => {
  afterEach(() => cleanup());
  it('blocks creation and explains a missing predecessor', () => {
    renderReview(17, gate(17, false, 16));
    expect(screen.getByText(/流程 16 尚未内部复核通过/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '建立工作项' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows process 23 ready from process 18 without requiring process 19', async () => {
    renderReview(23, gate(23, true, 18));
    expect(screen.getByText(/流程 18/)).toBeTruthy();
    expect(screen.queryByText(/流程 19/)).toBeNull();
    fireEvent.change(screen.getByLabelText('工作项复核人'), { target: { value: 'reviewer' } });
    await waitFor(() => expect((screen.getByRole('button', { name: '建立工作项' }) as HTMLButtonElement).disabled).toBe(false));
  });
});
