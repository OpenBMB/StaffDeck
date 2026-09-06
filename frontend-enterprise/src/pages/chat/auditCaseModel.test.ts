import { describe, expect, it } from 'vitest';

import type { AuditCaseMaterialRead } from '@/types';

import { auditCaseSummary } from './auditCaseModel';

describe('auditCaseSummary', () => {
  it('shows only unresolved current material states as missing', () => {
    const summary = auditCaseSummary([
      {
        id: 'm1',
        audit_case_id: 'case-1',
        attachment_id: 'a1',
        material_type: 'audit_record',
        filename: '记录.pdf',
        content_type: 'application/pdf',
        sha256: 'a'.repeat(64),
        size: 100,
        characters: 1000,
        extraction_status: 'succeeded',
        processing_status: 'succeeded',
        version: 1,
        is_current: true,
        created_at: '',
        updated_at: '',
      },
      {
        id: 'm2',
        audit_case_id: 'case-1',
        attachment_id: 'a2',
        material_type: 'template',
        filename: '模板.docx',
        content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        sha256: 'b'.repeat(64),
        size: 100,
        characters: 0,
        extraction_status: 'failed',
        processing_status: 'failed',
        version: 1,
        is_current: true,
        error_code: 'UNSUPPORTED_FORMAT',
        created_at: '',
        updated_at: '',
      },
    ] as AuditCaseMaterialRead[]);

    expect(summary.ready).toBe(1);
    expect(summary.failed).toEqual(['模板.docx']);
    expect(summary.pending).toBe(0);
  });
});
