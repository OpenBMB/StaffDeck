import type { AuditCaseCoverageRead } from '@/types';
import { formatCoverage } from '../auditCasePresentation';

export function CoveragePanel({ coverage }: { coverage: AuditCaseCoverageRead | null }) {
  if (!coverage) return <p className="text-[12px] text-[#858b9c]">覆盖率暂不可用</p>;
  const items = [
    ['文件覆盖率', coverage.file_coverage],
    ['分块覆盖率', coverage.chunk_coverage],
  ] as const;
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
    </div>
  );
}
