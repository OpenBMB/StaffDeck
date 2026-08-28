import type { AuditCaseCoverageRead } from '@/types';
import { formatCoverage } from '../auditCasePresentation';

export function CoveragePanel({ coverage }: { coverage: AuditCaseCoverageRead | null }) {
  if (!coverage) return <p className="text-[12px] text-[#858b9c]">覆盖率暂不可用</p>;
  const items = [
    ['文件覆盖率', coverage.file_coverage],
    ['分块覆盖率', coverage.chunk_coverage],
    ['审核要素证据覆盖率', coverage.element_coverage ?? 0],
  ] as const;
  const blockers = coverage.blockers || [];
  const blockerLabels: Record<string, string> = {
    NO_CURRENT_MATERIALS: '尚未上传审核材料',
    FILE_COVERAGE_INCOMPLETE: '文件覆盖率不足',
    CHUNK_COVERAGE_INCOMPLETE: '分块覆盖率不足',
    EVIDENCE_COVERAGE_INCOMPLETE: '证据处理尚未完成',
    CHUNK_INTERVAL_GAP: '分块存在文本区间缺口',
    NO_REQUIRED_AUDIT_ELEMENTS: '当前管理体系没有可用审核要素',
    ELEMENT_COVERAGE_INCOMPLETE: '审核要素证据覆盖率不足',
  };
  const publishAllowed = coverage.publish_allowed === true;
  const pendingMaterials = coverage.pending_material_ids?.length || 0;
  const failedMaterials = coverage.failed_material_ids?.length || coverage.failed_material_count;
  const pendingChunks = coverage.pending_chunk_ids?.length || 0;
  return (
    <div className="grid gap-[12px] sm:grid-cols-2">
      {items.map(([label, value]) => (
        <div key={label} className="rounded-[10px] border border-[#edf0f5] bg-white px-[14px] py-[12px]">
          <div className="flex items-center justify-between gap-[8px] text-[12px] text-[#858b9c]">
            <span>{label}</span><strong className="text-[15px] font-medium text-[#464c5e]">{formatCoverage(value)}</strong>
          </div>
          <div className="mt-[8px] h-[6px] overflow-hidden rounded-full bg-[#eef1f6]">
            <div className="h-full rounded-full bg-[#4d8cff] transition-all" style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }} />
          </div>
        </div>
      ))}
      <p className="text-[11px] text-[#a0a6b5] sm:col-span-2">已处理文件 {coverage.successful_material_count} / {coverage.current_material_count}，成功分块 {coverage.successful_chunk_count} / {coverage.total_chunk_count}</p>
      <div className={`rounded-[10px] border px-[12px] py-[10px] text-[12px] sm:col-span-2 ${publishAllowed ? 'border-[#ccebd7] bg-[#f2fbf5] text-[#20834c]' : 'border-[#f1d8b1] bg-[#fffaf1] text-[#a15c00]'}`}>
        <p className="font-medium">报告生成门槛：{publishAllowed ? '可以生成' : '暂不可生成'}</p>
        <p className="mt-[4px] text-[11px]">待处理材料 {pendingMaterials} · 失败材料 {failedMaterials} · 待处理证据分块 {pendingChunks}</p>
        {blockers.length > 0 && <ul className="mt-[5px] list-disc space-y-[2px] pl-[16px] text-[11px]">{blockers.map((blocker) => <li key={blocker}>{blockerLabels[blocker] || blocker}</li>)}</ul>}
      </div>
    </div>
  );
}
