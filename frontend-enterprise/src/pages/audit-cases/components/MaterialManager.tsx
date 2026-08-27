import { useEffect, useMemo, useState } from 'react';

import type { AuditCaseCoverageRead, AuditCaseManagementOptions, AuditCaseMaterialRead } from '@/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { notify } from '@/components/ui/app-toast';

import {
  processAuditCaseMaterial,
  replaceAuditCaseMaterial,
  type AuditCaseMaterialType,
  uploadAuditCaseMaterials,
} from '../auditCaseApi';
import { auditCaseErrorMessage } from '../auditCaseErrors';
import { CoveragePanel } from './CoveragePanel';

const MATERIAL_CATEGORIES: Array<{ type: AuditCaseMaterialType; label: string; description: string }> = [
  { type: 'audit_plan', label: '审核计划', description: '现场审核计划、日程与审核安排' },
  { type: 'audit_record', label: '审核记录', description: '审核记录、检查表和现场证据' },
  { type: 'performance_record', label: '绩效记录', description: '能源绩效、监视测量和统计记录' },
  { type: 'report_template', label: '报告模板', description: '审核报告、结论和改进建议模板' },
];

const DEFAULT_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md', '.markdown', '.html', '.htm'];

function extensionOf(filename: string): string {
  const index = filename.lastIndexOf('.');
  return index < 0 ? '' : filename.slice(index).toLowerCase();
}

function upsertMaterials(current: AuditCaseMaterialRead[], incoming: AuditCaseMaterialRead[]): AuditCaseMaterialRead[] {
  const byId = new Map(current.map((material) => [material.id, material]));
  incoming.forEach((material) => byId.set(material.id, material));
  return [...byId.values()];
}

function materialStatus(material: AuditCaseMaterialRead): { label: string; className: string } {
  if (material.extraction_status === 'failed') return { label: '提取失败', className: 'text-[#c20d0d]' };
  if (material.processing_status === 'failed') return { label: '处理失败', className: 'text-[#c20d0d]' };
  if (material.processing_status === 'succeeded') return { label: '处理成功', className: 'text-[#20834c]' };
  if (material.processing_status === 'processing') return { label: '处理中', className: 'text-[#1a71ff]' };
  return { label: '待处理', className: 'text-[#a15c00]' };
}

export function MaterialManager({
  caseId,
  materials,
  coverage,
  options,
  disabled = false,
  onChanged,
}: {
  caseId: string;
  materials: AuditCaseMaterialRead[];
  coverage: AuditCaseCoverageRead | null;
  options: AuditCaseManagementOptions;
  disabled?: boolean;
  onChanged?: () => Promise<void> | void;
}) {
  const [localMaterials, setLocalMaterials] = useState(materials);
  const [busyKey, setBusyKey] = useState('');
  const [uploadingType, setUploadingType] = useState<AuditCaseMaterialType | null>(null);
  const [failureCount, setFailureCount] = useState(0);
  const [error, setError] = useState('');
  const extensions = useMemo(
    () => (options.supported_extensions.length ? options.supported_extensions : DEFAULT_EXTENSIONS)
      .map((extension) => extension.toLowerCase()),
    [options.supported_extensions],
  );

  useEffect(() => {
    setLocalMaterials(materials);
  }, [materials]);

  function validateFiles(files: File[]): string | null {
    const invalid = files.find((file) => !extensions.includes(extensionOf(file.name)));
    if (invalid) return `${invalid.name}：文件格式暂不支持，请转换后再上传`;
    const oversized = files.find((file) => file.size > options.max_material_bytes);
    if (oversized) return `${oversized.name}：单个文件不能超过 ${Math.round(options.max_material_bytes / 1024 / 1024)} MB`;
    return null;
  }

  async function processUploaded(category: AuditCaseMaterialType, uploaded: AuditCaseMaterialRead[]) {
    setLocalMaterials((current) => upsertMaterials(current, uploaded));
    const results = await Promise.allSettled(uploaded.map((material) => processAuditCaseMaterial(caseId, material.id)));
    let failures = 0;
    results.forEach((result, index) => {
      const uploadedMaterial = uploaded[index];
      if (result.status === 'fulfilled') {
        setLocalMaterials((current) => upsertMaterials(current, [result.value]));
      } else {
        failures += 1;
        setLocalMaterials((current) => upsertMaterials(current, [{ ...uploadedMaterial, processing_status: 'failed', error_code: 'PROCESSING_FAILED' }]));
      }
    });
    setFailureCount(failures);
    if (failures) {
      setError(`${category === 'audit_record' ? '审核记录' : '材料'}已上传，但处理失败 ${failures} 份，可单独重试`);
    } else {
      setError('');
    }
    if (onChanged) await onChanged();
  }

  async function handleUpload(category: AuditCaseMaterialType, files: File[]) {
    if (disabled || !files.length) return;
    const validationError = validateFiles(files);
    if (validationError) {
      setError(validationError);
      return;
    }
    setUploadingType(category);
    setError('');
    try {
      const uploaded = await uploadAuditCaseMaterials(caseId, category, files);
      await processUploaded(category, uploaded);
      notify.success(`已上传 ${uploaded.length} 份材料`);
    } catch (uploadError) {
      setError(auditCaseErrorMessage(uploadError, '材料上传失败'));
    } finally {
      setUploadingType(null);
    }
  }

  async function handleRetry(material: AuditCaseMaterialRead) {
    if (disabled) return;
    setBusyKey(material.id);
    setError('');
    try {
      const processed = await processAuditCaseMaterial(caseId, material.id);
      setLocalMaterials((current) => upsertMaterials(current, [processed]));
      setFailureCount((current) => Math.max(0, current - 1));
      await onChanged?.();
    } catch (retryError) {
      setError(auditCaseErrorMessage(retryError, `${material.filename} 处理失败`));
    } finally {
      setBusyKey('');
    }
  }

  async function handleReplace(material: AuditCaseMaterialRead, file: File) {
    if (disabled) return;
    const validationError = validateFiles([file]);
    if (validationError) {
      setError(validationError);
      return;
    }
    setBusyKey(material.id);
    setError('');
    try {
      const replacement = await replaceAuditCaseMaterial(caseId, material.id, file);
      await processUploaded(material.material_type as AuditCaseMaterialType, [replacement]);
      notify.success(`${material.filename} 已替换`);
    } catch (replaceError) {
      setError(auditCaseErrorMessage(replaceError, `${material.filename} 替换失败`));
    } finally {
      setBusyKey('');
    }
  }

  return (
    <div className="grid gap-[16px]">
      <CoveragePanel coverage={coverage} />
      {disabled && <p className="rounded-[9px] bg-[#f5f6f8] px-[12px] py-[10px] text-[12px] text-[#858b9c]">项目已归档，材料不可修改</p>}
      {error && <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">{error}</p>}
      {failureCount > 0 && <p className="text-[12px] text-[#c20d0d]">处理失败 {failureCount} 份</p>}
      <div className="grid gap-[12px] xl:grid-cols-2">
        {MATERIAL_CATEGORIES.map((category) => {
          const categoryMaterials = localMaterials.filter((material) => material.material_type === category.type && material.is_current);
          const inputId = `audit-case-${caseId}-${category.type}`;
          return (
            <section key={category.type} className="grid gap-[12px] rounded-[12px] border border-[#edf0f5] bg-white p-[14px]">
              <div className="flex items-start justify-between gap-[12px]">
                <div><h3 className="text-[13px] font-medium text-[#464c5e]">{category.label}</h3><p className="mt-[3px] text-[11px] text-[#a0a6b5]">{category.description}</p></div>
                <label htmlFor={inputId} className={disabled || uploadingType === category.type ? 'pointer-events-none rounded-[8px] border border-[#e5e8ef] px-[10px] py-[6px] text-[11px] text-[#b1b7c5]' : 'cursor-pointer rounded-[8px] border border-[#dfe4ee] px-[10px] py-[6px] text-[11px] text-[#464c5e] hover:bg-[#f5f7fb]'}>
                  {uploadingType === category.type ? '上传中…' : '上传材料'}
                </label>
                <Input id={inputId} type="file" multiple accept={extensions.join(',')} aria-label={`上传${category.label}`} disabled={disabled || uploadingType === category.type} className="sr-only" onChange={(event) => { void handleUpload(category.type, Array.from(event.currentTarget.files || [])); event.currentTarget.value = ''; }} />
              </div>
              {categoryMaterials.length === 0 && <p className="rounded-[9px] border border-dashed border-[#e1e5ed] px-[12px] py-[18px] text-center text-[12px] text-[#a0a6b5]">暂无{category.label}</p>}
              {categoryMaterials.map((material) => {
                const status = materialStatus(material);
                const replacing = busyKey === material.id;
                const replaceInputId = `${inputId}-${material.id}-replace`;
                return (
                  <div key={material.id} className="flex items-center justify-between gap-[10px] rounded-[9px] bg-[#f8f9fb] px-[12px] py-[10px]">
                    <div className="min-w-0"><p className="truncate text-[12px] text-[#464c5e]" title={material.filename}>{material.filename}</p><p className={`mt-[3px] text-[11px] ${status.className}`}>{status.label}{material.characters ? ` · ${material.characters.toLocaleString()} 字` : ''}</p></div>
                    <div className="flex shrink-0 items-center gap-[6px]">
                      {material.processing_status === 'failed' && <Button type="button" variant="ghost" size="sm" disabled={disabled || replacing} onClick={() => void handleRetry(material)}>{replacing ? '处理中…' : '重试'}</Button>}
                      <label htmlFor={replaceInputId} className={disabled || replacing ? 'pointer-events-none text-[11px] text-[#b1b7c5]' : 'cursor-pointer text-[11px] text-[#1a71ff]'}>{replacing ? '处理中…' : '替换'}</label>
                      <Input id={replaceInputId} type="file" accept={extensions.join(',')} aria-label={`替换${material.filename}`} disabled={disabled || replacing} className="sr-only" onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) void handleReplace(material, file); event.currentTarget.value = ''; }} />
                    </div>
                  </div>
                );
              })}
            </section>
          );
        })}
      </div>
      <p className="text-[11px] text-[#a0a6b5]">支持格式：{extensions.join('、')}；单个文件上限 {Math.round(options.max_material_bytes / 1024 / 1024)} MB。上传后系统会自动提取文本并处理，失败文件可单独重试或替换。</p>
    </div>
  );
}
