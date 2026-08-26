import type {
  AuditCaseCoverageRead,
  AuditCaseMaterialRead,
  AuditCaseRead,
} from '@/types';

import { auditCaseSummary } from '../auditCaseModel';

type AuditCasePanelProps = {
  cases: AuditCaseRead[];
  selectedId: string | null;
  materials: AuditCaseMaterialRead[];
  coverage: AuditCaseCoverageRead | null;
  loading: boolean;
  onSelect: (caseId: string | null) => void;
  locked?: boolean;
};

export default function AuditCasePanel({
  cases,
  selectedId,
  materials,
  coverage,
  loading,
  onSelect,
  locked = false,
}: AuditCasePanelProps) {
  const selectedCase = cases.find((item) => item.id === selectedId) || null;
  const summary = auditCaseSummary(materials);

  return (
    <section className="mx-auto mt-[12px] w-[min(820px,calc(100%-48px))] rounded-[14px] border border-[#e3e7f1] bg-white px-[14px] py-[10px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]" aria-label="审核项目">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <div className="flex items-center gap-[8px]">
          <strong className="text-[13px] text-[#18181a]">审核项目</strong>
          <span className="text-[11px] text-[#858b9c]">
            {selectedCase ? `${selectedCase.organization_name} · ${selectedCase.report_type}` : '未绑定，选择项目后自动复用资料'}
          </span>
        </div>
        {selectedCase && (
          <span className="rounded-full bg-[#edf7ef] px-[8px] py-[2px] text-[11px] text-[#2e8b57]">
            已绑定
          </span>
        )}
      </div>

      <div className="mt-[8px] flex flex-wrap gap-[6px]">
        {!locked && (
          <button
            type="button"
            aria-pressed={!selectedId}
            onClick={() => onSelect(null)}
            className="rounded-[8px] border border-[#e3e7f1] px-[9px] py-[5px] text-[11px] text-[#757f9c] transition-colors hover:bg-[#f7f8fa]"
          >
            不绑定项目
          </button>
        )}
        {cases.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-pressed={selectedId === item.id}
            disabled={locked && selectedId !== item.id}
            onClick={() => onSelect(item.id)}
            className={`rounded-[8px] border px-[9px] py-[5px] text-[11px] transition-colors ${selectedId === item.id ? 'border-[#9bbcff] bg-[#edf3ff] text-[#1a71ff]' : 'border-[#e3e7f1] text-[#464c5e] hover:bg-[#f7f8fa]'}`}
          >
            {item.organization_name}
          </button>
        ))}
        {!loading && cases.length === 0 && (
          <span className="text-[11px] text-[#858b9c]">暂无可用审核项目</span>
        )}
      </div>

      {selectedCase && (
        <div className="mt-[8px] flex flex-wrap items-center gap-[8px] text-[11px] text-[#757f9c]">
          <span>当前资料 {summary.ready}/{summary.total} 已就绪</span>
          {summary.pending > 0 && <span className="text-[#b7791f]">处理中 {summary.pending}</span>}
          {summary.failed.length > 0 && (
            <span className="text-[#c13e35]">待处理：{summary.failed.join('、')}</span>
          )}
          {coverage && <span>分块覆盖率 {Math.round(coverage.chunk_coverage * 100)}%</span>}
        </div>
      )}
    </section>
  );
}
