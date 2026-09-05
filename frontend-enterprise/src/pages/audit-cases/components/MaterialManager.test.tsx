// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';

import type { AuditCaseCoverageRead, AuditCaseManagementOptions, AuditCaseMaterialRead } from '@/types';
import { I18nProvider } from '@/i18n';

import {
  processAuditCaseMaterial,
  uploadAuditCaseMaterials,
} from '../auditCaseApi';
import { MaterialManager } from './MaterialManager';

vi.mock('../auditCaseApi', () => ({
  processAuditCaseMaterial: vi.fn(),
  replaceAuditCaseMaterial: vi.fn(),
  uploadAuditCaseMaterials: vi.fn(),
}));

const options: AuditCaseManagementOptions = {
  knowledge_versions: [],
  supported_extensions: ['.txt', '.docx'],
  max_material_bytes: 50 * 1024 * 1024,
};

const coverage: AuditCaseCoverageRead = {
  current_material_count: 0,
  successful_material_count: 0,
  failed_material_count: 0,
  total_chunk_count: 0,
  successful_chunk_count: 0,
  file_coverage: 0,
  chunk_coverage: 0,
};

function material(id: string, filename: string, materialType: AuditCaseMaterialRead['material_type'] = 'audit_record_form'): AuditCaseMaterialRead {
  return {
    id,
    audit_case_id: 'case-1',
    attachment_id: `attachment-${id}`,
    material_type: materialType,
    filename,
    content_type: 'text/plain',
    sha256: id,
    size: 10,
    characters: 10,
    extraction_status: 'succeeded',
    processing_status: 'succeeded',
    version: 1,
    is_current: true,
    supersedes_material_id: null,
    error_code: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderManager(props: Partial<ComponentProps<typeof MaterialManager>> = {}) {
  return render(
    <I18nProvider>
      <MaterialManager caseId="case-1" materials={[]} coverage={coverage} options={options} {...props} />
    </I18nProvider>,
  );
}

describe('MaterialManager', () => {
  it('validates files, uploads them by category, and processes each uploaded material', async () => {
    const user = userEvent.setup();
    const uploaded = material('material-1', '审核记录.txt');
    vi.mocked(uploadAuditCaseMaterials).mockResolvedValue([uploaded]);
    vi.mocked(processAuditCaseMaterial).mockResolvedValue({ ...uploaded, processing_status: 'succeeded' });

    renderManager();

    await user.upload(screen.getByLabelText('上传审核记录表'), new File(['记录内容'], '审核记录.txt', { type: 'text/plain' }));
    expect(uploadAuditCaseMaterials).toHaveBeenCalledWith('case-1', 'audit_record_form', [expect.any(File)]);
    expect(processAuditCaseMaterial).toHaveBeenCalledWith('case-1', 'material-1');
    expect(await screen.findByText('审核记录.txt')).toBeTruthy();
    expect(screen.getByText('处理成功', { exact: false })).toBeTruthy();

    fireEvent.change(screen.getByLabelText('上传审核记录表'), {
      target: { files: [new File(['旧格式'], '审核记录.doc', { type: 'application/msword' })] },
    });
    expect((await screen.findByRole('alert')).textContent).toContain('文件格式暂不支持');
    expect(uploadAuditCaseMaterials).toHaveBeenCalledTimes(1);
  });

  it('keeps successful uploads visible when another material fails to process', async () => {
    const user = userEvent.setup();
    const first = material('material-1', '记录一.txt');
    const second = material('material-2', '记录二.txt');
    vi.mocked(uploadAuditCaseMaterials).mockResolvedValue([first, second]);
    vi.mocked(processAuditCaseMaterial).mockImplementation(async (_caseId, materialId) => {
      if (materialId === 'material-2') throw new Error('提取失败');
      return { ...first, processing_status: 'succeeded' };
    });

    renderManager();
    await user.upload(screen.getByLabelText('上传审核记录表'), [
      new File(['一'], '记录一.txt', { type: 'text/plain' }),
      new File(['二'], '记录二.txt', { type: 'text/plain' }),
    ]);

    expect(await screen.findByText('记录一.txt')).toBeTruthy();
    expect(screen.getByText('记录二.txt')).toBeTruthy();
    expect(screen.getByText('处理失败 1 份')).toBeTruthy();
  });

  it('disables all material changes for an archived project', () => {
    renderManager({ disabled: true });
    expect((screen.getByLabelText('上传审核记录表') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText('项目已归档，材料不可修改')).toBeTruthy();
  });

  it('keeps legacy categories visible without offering a new upload control', () => {
    renderManager({ materials: [material('legacy-1', '旧审核记录.txt', 'audit_record')] });

    expect(screen.getByText('历史分类材料')).toBeTruthy();
    expect(screen.getByText('旧审核记录.txt')).toBeTruthy();
    expect(screen.queryByLabelText('上传绩效记录')).toBeNull();
    expect(screen.queryByLabelText('上传报告模板')).toBeNull();
  });

  it('explains how to fix an image-only PDF with the offline OCR path', () => {
    renderManager({
      materials: [{
        ...material('scan-1', '扫描审核记录.pdf'),
        extraction_status: 'failed',
        processing_status: 'failed',
        error_code: 'PDF_TEXT_LAYER_MISSING',
      }],
    });

    expect(screen.getByText(/RapidDoc/)).toBeTruthy();
    expect(screen.getByText(/可搜索 PDF/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('shows extraction metadata and allows retry after OCR failure', async () => {
    const user = userEvent.setup();
    vi.mocked(processAuditCaseMaterial).mockResolvedValue(material('scan-1', '扫描审核记录.pdf'));
    renderManager({
      materials: [{
        ...material('scan-1', '扫描审核记录.pdf'),
        extraction_status: 'failed',
        processing_status: 'failed',
        error_code: 'OCR_MODEL_MISSING',
        page_count: 31,
        extraction_method: 'structured',
      }],
    });

    expect(screen.getByText(/31 页/)).toBeTruthy();
    expect(screen.getByText(/结构化提取/)).toBeTruthy();
    expect(screen.getByText(/RapidDoc 模型/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '重试' }));
    expect(processAuditCaseMaterial).toHaveBeenCalledWith('case-1', 'scan-1');
  });
});
