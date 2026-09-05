// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditCaseCoverageRead, AuditReportRead } from '@/types';

import { processAuditCaseEvidence } from '../auditCaseApi';
import {
  createAuditCaseReport,
  downloadAuditCaseReport,
  listAuditCaseReports,
  publishAuditCaseReport,
} from '../auditReportApi';
import { EvidenceReportPanel } from './EvidenceReportPanel';

vi.mock('../auditCaseApi', () => ({
  processAuditCaseEvidence: vi.fn(),
}));

vi.mock('../auditReportApi', () => ({
  createAuditCaseReport: vi.fn(),
  downloadAuditCaseReport: vi.fn(),
  listAuditCaseReports: vi.fn(),
  publishAuditCaseReport: vi.fn(),
}));

function coverage(overrides: Partial<AuditCaseCoverageRead> = {}): AuditCaseCoverageRead {
  return {
    current_material_count: 1,
    successful_material_count: 1,
    failed_material_count: 0,
    total_chunk_count: 1,
    successful_chunk_count: 1,
    file_coverage: 1,
    chunk_coverage: 1,
    element_coverage: 1,
    publish_allowed: true,
    blockers: [],
    pending_material_ids: [],
    failed_material_ids: [],
    pending_chunk_ids: [],
    ...overrides,
  };
}

function report(overrides: Partial<AuditReportRead> = {}): AuditReportRead {
  return {
    id: 'report-1',
    tenant_id: 'tenant_demo',
    audit_case_id: 'case-1',
    version: 1,
    status: 'draft',
    material_version_ids: [],
    knowledge_base_version_ids: [],
    rule_set_version_ids: [],
    rule_traceability_status: 'not_configured',
    coverage_snapshot: {},
    sections: [
      {
        id: 'section-1',
        section_id: 'summary',
        title: '摘要',
        sequence: 1,
        status: 'succeeded',
        retry_count: 0,
        rule_definition_ids: [],
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  vi.mocked(listAuditCaseReports).mockResolvedValue([]);
  vi.mocked(processAuditCaseEvidence).mockResolvedValue({
    status: 'succeeded',
    materials: [],
    coverage: coverage(),
  });
  vi.mocked(createAuditCaseReport).mockResolvedValue(report());
  vi.mocked(publishAuditCaseReport).mockResolvedValue(report({ status: 'published' }));
  vi.mocked(downloadAuditCaseReport).mockResolvedValue(new Blob(['docx']));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('EvidenceReportPanel', () => {
  it('offers evidence processing when chunks are pending and preserves state on failure', async () => {
    vi.mocked(processAuditCaseEvidence).mockRejectedValueOnce(new Error('network'));
    const user = userEvent.setup();
    render(
      <EvidenceReportPanel
        caseId="case-1"
        coverage={coverage({ pending_chunk_ids: ['chunk-1'], chunk_coverage: 1 })}
      />,
    );

    const processButton = await screen.findByRole('button', { name: '处理证据' });
    expect((processButton as HTMLButtonElement).disabled).toBe(false);
    await user.click(processButton);
    expect((await screen.findByRole('alert')).textContent).toContain('证据处理失败');
    expect(screen.getByText(/规则尚未配置/)).toBeTruthy();
  });

  it('allows a draft after coverage completes but blocks confirmation without a rule snapshot', async () => {
    const user = userEvent.setup();
    render(<EvidenceReportPanel caseId="case-1" coverage={coverage()} />);

    await user.click(await screen.findByRole('button', { name: '生成待确认草稿' }));
    expect(await screen.findByText(/规则尚未配置/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '确认发布' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('publishes and downloads a complete report', async () => {
    vi.mocked(listAuditCaseReports).mockResolvedValueOnce([
      report({ rule_traceability_status: 'complete', rule_set_version_ids: ['version-1'] }),
    ]);
    vi.mocked(publishAuditCaseReport).mockResolvedValueOnce(report({
      status: 'published',
      rule_traceability_status: 'complete',
      final_storage_key: 'audit_cases/report.docx',
    }));
    const createObjectURL = vi.spyOn(window.URL, 'createObjectURL').mockReturnValue('blob:report');
    const revokeObjectURL = vi.spyOn(window.URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<EvidenceReportPanel caseId="case-1" coverage={coverage()} />);

    const publish = await screen.findByRole('button', { name: '确认发布' });
    expect((publish as HTMLButtonElement).disabled).toBe(false);
    await user.click(publish);
    await user.click(await screen.findByRole('button', { name: '下载报告' }));
    expect(createObjectURL).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:report');
    createObjectURL.mockRestore();
    revokeObjectURL.mockRestore();
  });
});
