import { Search } from 'lucide-react';

import { Input, Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';

import type { AuditCaseListParams } from '../auditCaseTypes';

export function AuditCaseFilters({
  filters,
  onFilter,
}: {
  filters: AuditCaseListParams;
  onFilter: (key: 'query' | 'status' | 'management_system' | 'report_type', value: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-[8px]">
      <div className="relative min-w-[220px] flex-1 sm:max-w-[320px]">
        <Search className="pointer-events-none absolute left-[12px] top-1/2 size-[15px] -translate-y-1/2 text-[#a3aaba]" />
        <Input
          aria-label="搜索认证项目"
          value={filters.query || ''}
          onChange={(event) => onFilter('query', event.target.value)}
          placeholder="搜索企业名称"
          className="h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] pl-[34px] text-[12px] shadow-none"
        />
      </div>
      <Select value={filters.status || 'all'} onValueChange={(value) => onFilter('status', value)}>
        <SelectTrigger aria-label="项目状态" className={SELECT_TRIGGER_CLASS}>
          <SelectValue placeholder="项目状态" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">全部状态</SelectItem>
          <SelectItem value="active">进行中</SelectItem>
          <SelectItem value="archived">已归档</SelectItem>
        </SelectContent>
      </Select>
      <Input
        aria-label="管理体系筛选"
        value={filters.management_system || ''}
        onChange={(event) => onFilter('management_system', event.target.value)}
        placeholder="管理体系"
        className="h-[34px] w-[140px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px] shadow-none"
      />
      <Input
        aria-label="报告类型筛选"
        value={filters.report_type || ''}
        onChange={(event) => onFilter('report_type', event.target.value)}
        placeholder="报告类型"
        className="h-[34px] w-[120px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px] shadow-none"
      />
    </div>
  );
}
