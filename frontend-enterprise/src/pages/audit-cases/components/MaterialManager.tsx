import { useEffect, useMemo, useState } from 'react';

import type { AuditCaseCoverageRead, AuditCaseManagementOptions, AuditCaseMaterialRead } from '@/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { notify } from '@/components/ui/app-toast';

import {
  loadAuditCaseMaterials,
  processAuditCaseMaterial,
  replaceAuditCaseMaterial,
  type AuditCaseMaterialType,
  uploadAuditCaseMaterials,
} from '../auditCaseApi';
import { auditCaseErrorCodeMessage, auditCaseErrorMessage } from '../auditCaseErrors';
import { CoveragePanel } from './CoveragePanel';

const DEFAULT_MATERIAL_CATEGORIES: Array<{ type: AuditCaseMaterialType; label: string; description: string; group: string }> = [
  { type: 'audit_notice', label: '审核通知单', description: '审核通知与任务安排', group: '审核前准备' },
  { type: 'permanent_site_list', label: '常设场所清单', description: '常设场所和审核范围', group: '审核前准备' },
  { type: 'temporary_site_list', label: '临时场所清单', description: '临时场所和审核范围', group: '审核前准备' },
  { type: 'document_review_report', label: '文审报告', description: '文件审核结论与记录', group: '审核前准备' },
  { type: 'audit_team_preparation_record', label: '审核组准备会记录', description: '审核组准备会过程记录', group: '审核前准备' },
  { type: 'opening_closing_attendance', label: '首末次会议签到表', description: '首末次会议参会签到', group: '会议与信息确认' },
  { type: 'organization_information_confirmation', label: '受审核组织信息确认表', description: '受审核组织基本信息确认', group: '会议与信息确认' },
  { type: 'opening_meeting_record', label: '管理体系审核首次会议记录', description: '首次会议过程记录', group: '会议与信息确认' },
  { type: 'closing_meeting_record', label: '管理体系审核末次会议记录', description: '末次会议过程记录', group: '会议与信息确认' },
  { type: 'audit_record_form', label: '审核记录表', description: '条款审核记录与证据', group: '审核实施与发现' },
  { type: 'nonconformity_report', label: '不符合项报告', description: '不符合项及证据', group: '审核实施与发现' },
  { type: 'improvement_suggestion_report', label: '改进建议报告', description: '改进建议及依据', group: '审核实施与发现' },
  { type: 'stage_one_audit_report', label: '一阶段审核报告', description: '一阶段审核结论', group: '审核实施与发现' },
  { type: 'stage_one_findings_summary', label: '一阶段审核发现问题汇总表', description: '一阶段问题汇总', group: '审核实施与发现' },
  { type: 'audit_report', label: '审核报告', description: '既有审核报告或参考报告', group: '报告与监督' },
  { type: 'surveillance_audit_plan', label: '监督审核策划表', description: '监督审核策划信息', group: '报告与监督' },
  { type: 'audit_performance_tracking', label: '审核过程绩效跟踪分析表', description: '审核过程绩效跟踪', group: '报告与监督' },
  { type: 'other_material', label: '其他资料', description: '无法归入上述类别的资料', group: '报告与监督' },
];

const LEGACY_MATERIAL_LABELS: Record<string, string> = {
  audit_plan: '审核计划',
  audit_record: '审核记录',
  performance_record: '绩效记录',
  report_template: '报告模板',
};

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
  if (material.extraction_status === 'processing' || material.processing_status === 'processing') return { label: '处理中', className: 'text-[#1a71ff]' };
  return { label: '排队中', className: 'text-[#a15c00]' };
}

function extractionMethodLabel(method?: string | null): string {
  if (method === 'structured') return '结构化提取';
  if (method === 'native') return '原生提取';
  return method || '';
}

function materialErrorMessage(material: AuditCaseMaterialRead): string {
  if (!material.error_code) return '';
  return auditCaseErrorCodeMessage(material.error_code, `处理失败：${material.error_code}`);
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
  const categories = useMemo(() => (
    options.material_types?.length
      ? options.material_types.map((item) => ({
        type: item.value as AuditCaseMaterialType,
        label: item.label,
        description: item.description,
        group: item.group,
      }))
      : DEFAULT_MATERIAL_CATEGORIES
  ), [options.material_types]);
  const categoryTypes = useMemo(() => new Set(categories.map((category) => category.type)), [categories]);
  const groupedCategories = useMemo(() => {
    const groups = new Map<string, typeof categories>();
    categories.forEach((category) => groups.set(category.group, [...(groups.get(category.group) || []), category]));
    return [...groups.entries()];
  }, [categories]);
  const legacyMaterials = localMaterials.filter((material) => material.is_current && !categoryTypes.has(material.material_type as AuditCaseMaterialType));
  const extensions = useMemo(
    () => (options.supported_extensions.length ? options.supported_extensions : DEFAULT_EXTENSIONS)
      .map((extension) => extension.toLowerCase()),
    [options.supported_extensions],
  );

  useEffect(() => {
    setLocalMaterials(materials);
  }, [materials]);

  useEffect(() => {
    if (disabled || !localMaterials.some((material) => material.is_current && (['pending', 'processing'].includes(material.extraction_status) || ['pending', 'processing'].includes(material.processing_status)))) return;
    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const refreshed = await loadAuditCaseMaterials(caseId);
        if (active) {
          setLocalMaterials(refreshed);
          await onChanged?.();
        }
      } catch {
        // A later poll can recover from a transient status request failure.
      }
    }, 1500);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [caseId, disabled, localMaterials, onChanged]);

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
      const categoryLabel = categories.find((item) => item.type === category)?.label || '材料';
      setError(`${categoryLabel}已上传，但处理失败 ${failures} 份，可单独重试`);
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
      <div className="grid gap-[10px]">
        {groupedCategories.map(([group, groupCategories], groupIndex) => (
          <details key={group} open={groupIndex === 0} className="rounded-[12px] border border-[#edf0f5] bg-white p-[12px]">
            <summary className="cursor-pointer list-none text-[13px] font-medium text-[#464c5e]">{group}<span className="ml-[7px] text-[11px] font-normal text-[#a0a6b5]">选传 · {groupCategories.length} 类</span></summary>
            <div className="mt-[10px] grid gap-[8px] xl:grid-cols-2">
              {groupCategories.map((category) => {
                const categoryMaterials = localMaterials.filter((material) => material.material_type === category.type && material.is_current);
                const inputId = `audit-case-${caseId}-${category.type}`;
                return (
                  <section key={category.type} className="grid gap-[8px] rounded-[10px] border border-[#edf0f5] p-[10px]">
                    <div className="flex items-start justify-between gap-[10px]">
                      <div className="min-w-0"><h3 className="text-[12px] font-medium text-[#464c5e]">{category.label}<span className="ml-[5px] text-[10px] font-normal text-[#a0a6b5]">选传</span></h3><p className="mt-[3px] text-[11px] text-[#a0a6b5]">{category.description}</p></div>
                      <label htmlFor={inputId} className={disabled || uploadingType === category.type ? 'pointer-events-none rounded-[8px] border border-[#e5e8ef] px-[9px] py-[5px] text-[11px] text-[#b1b7c5]' : 'cursor-pointer rounded-[8px] border border-[#dfe4ee] px-[9px] py-[5px] text-[11px] text-[#464c5e] hover:bg-[#f5f7fb]'}>
                        {uploadingType === category.type ? '上传中…' : '上传材料'}
                      </label>
                      <Input id={inputId} type="file" multiple accept={extensions.join(',')} aria-label={`上传${category.label}`} disabled={disabled || uploadingType === category.type} className="sr-only" onChange={(event) => { void handleUpload(category.type, Array.from(event.currentTarget.files || [])); event.currentTarget.value = ''; }} />
                    </div>
                    {categoryMaterials.length === 0 && <p className="rounded-[8px] border border-dashed border-[#e1e5ed] px-[10px] py-[12px] text-center text-[11px] text-[#a0a6b5]">暂无{category.label}</p>}
                    {categoryMaterials.map((material) => {
                      const status = materialStatus(material);
                      const replacing = busyKey === material.id;
                      const replaceInputId = `${inputId}-${material.id}-replace`;
                      return (
                        <div key={material.id} className="flex items-center justify-between gap-[10px] rounded-[8px] bg-[#f8f9fb] px-[10px] py-[8px]">
                          <div className="min-w-0"><p className="truncate text-[12px] text-[#464c5e]" title={material.filename}>{material.filename}</p><p className={`mt-[3px] text-[11px] ${status.className}`}>{status.label}{material.characters ? ` · ${material.characters.toLocaleString()} 字` : ''}{material.page_count ? ` · ${material.page_count} 页` : ''}{material.extraction_method ? ` · ${extractionMethodLabel(material.extraction_method)}` : ''}</p>{materialErrorMessage(material) && <p className="mt-[3px] text-[11px] leading-[1.5] text-[#c20d0d]">{materialErrorMessage(material)}</p>}</div>
                          <div className="flex shrink-0 items-center gap-[6px]">
                            {(material.extraction_status === 'failed' || material.processing_status === 'failed') && <Button type="button" variant="ghost" size="sm" disabled={disabled || replacing} onClick={() => void handleRetry(material)}>{replacing ? '处理中…' : '重试'}</Button>}
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
          </details>
        ))}
      </div>
      {legacyMaterials.length > 0 && (
        <section className="grid gap-[8px] rounded-[12px] border border-dashed border-[#e5e8ef] bg-[#fbfcfe] p-[12px]">
          <div><h3 className="text-[13px] font-medium text-[#697189]">历史分类材料</h3><p className="mt-[3px] text-[11px] text-[#a0a6b5]">旧项目材料仍会参与调取，新项目不再使用这些分类上传。</p></div>
          {legacyMaterials.map((material) => {
            const status = materialStatus(material);
            return <div key={material.id} className="flex items-center justify-between gap-[10px] rounded-[8px] bg-white px-[10px] py-[8px]"><div className="min-w-0"><p className="truncate text-[12px] text-[#464c5e]" title={material.filename}>{material.filename}</p><p className="mt-[3px] text-[11px] text-[#a0a6b5]">{LEGACY_MATERIAL_LABELS[material.material_type] || material.material_type} · {status.label}</p></div><span className="shrink-0 text-[10px] text-[#a0a6b5]">历史只读</span></div>;
          })}
        </section>
      )}
      <p className="text-[11px] text-[#a0a6b5]">支持格式：{extensions.join('、')}；单个文件上限 {Math.round(options.max_material_bytes / 1024 / 1024)} MB。上传后系统会自动提取文本并处理，失败文件可单独重试或替换。</p>
    </div>
  );
}
