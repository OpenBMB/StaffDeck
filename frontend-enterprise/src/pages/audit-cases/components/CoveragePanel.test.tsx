// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AuditCaseCoverageRead } from '@/types';

import { CoveragePanel } from './CoveragePanel';

describe('CoveragePanel', () => {
  it('shows evidence coverage, processing counts, and report blockers', () => {
    const coverage = {
      current_material_count: 4,
      successful_material_count: 2,
      failed_material_count: 1,
      total_chunk_count: 20,
      successful_chunk_count: 15,
      file_coverage: 0.5,
      chunk_coverage: 0.75,
      element_coverage: 0.4,
      publish_allowed: false,
      blockers: ['FILE_COVERAGE_INCOMPLETE', 'EVIDENCE_COVERAGE_INCOMPLETE'],
      pending_material_ids: ['material-pending'],
      failed_material_ids: ['material-failed'],
      pending_chunk_ids: ['chunk-1', 'chunk-2'],
    } as AuditCaseCoverageRead;

    render(<CoveragePanel coverage={coverage} />);

    expect(screen.getByText('审核要素证据覆盖率')).toBeTruthy();
    expect(screen.getByText('40%')).toBeTruthy();
    expect(screen.getByText(/报告生成门槛：暂不可生成/)).toBeTruthy();
    expect(screen.getByText(/待处理材料 1/)).toBeTruthy();
    expect(screen.getByText(/失败材料 1/)).toBeTruthy();
    expect(screen.getByText(/待处理证据分块 2/)).toBeTruthy();
    expect(screen.getByText(/文件覆盖率不足/)).toBeTruthy();
  });
});
