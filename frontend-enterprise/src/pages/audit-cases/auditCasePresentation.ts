export const AUDIT_CASE_STATUS_LABEL: Record<string, string> = {
  collecting: '材料收集中',
  ready: '待生成报告',
  generating: '报告生成中',
  archived: '已归档',
};

export const AUDIT_CASE_STATUS_TONE: Record<string, string> = {
  collecting: 'bg-[#fff7e8] text-[#a15c00]',
  ready: 'bg-[#eaf8ef] text-[#20834c]',
  generating: 'bg-[#eaf2ff] text-[#1a71ff]',
  archived: 'bg-[#f1f2f5] text-[#858b9c]',
};

export function auditCaseStatusLabel(status: string): string {
  return AUDIT_CASE_STATUS_LABEL[status] || status || '未知状态';
}

export function auditCaseStatusClass(status: string): string {
  return AUDIT_CASE_STATUS_TONE[status] || 'bg-[#f1f2f5] text-[#858b9c]';
}

export function formatCoverage(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

export function formatMaterialSummary(total: number, ready: number, failed: number): string {
  if (!total) return '暂无材料';
  return `材料 ${total} · 已处理 ${ready}${failed ? ` · 失败 ${failed}` : ''}`;
}

export function formatAuditCaseDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleDateString('zh-CN');
}
