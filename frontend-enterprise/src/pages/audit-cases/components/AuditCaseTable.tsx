import { ArrowRight, ClipboardCheck } from 'lucide-react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { cn } from '@/lib/utils';

import type { AuditCaseManagementRead } from '@/types';
import {
  auditCaseStatusClass,
  auditCaseStatusLabel,
  formatAuditCaseDate,
  formatCoverage,
  formatMaterialSummary,
} from '../auditCasePresentation';

export function AuditCaseTable({
  rows,
  loading,
  onOpen,
}: {
  rows: AuditCaseManagementRead[];
  loading: boolean;
  onOpen: (caseId: string) => void;
}) {
  const columns: DataTableColumn<AuditCaseManagementRead>[] = [
    {
      key: 'organization_name',
      title: '认证项目',
      width: 260,
      render: (row) => (
        <div className="flex min-w-0 items-center gap-[10px]">
          <span className="grid size-[30px] shrink-0 place-items-center rounded-[9px] bg-[#eef4ff] text-[#4d8cff]">
            <ClipboardCheck className="size-[16px]" />
          </span>
          <span className="min-w-0">
            <span className="block truncate font-medium text-[#18181a]">{row.organization_name}</span>
            <span className="mt-[3px] block truncate text-[11px] text-[#a0a6b5]">{row.report_type}</span>
          </span>
        </div>
      ),
    },
    {
      key: 'management_systems',
      title: '管理体系',
      width: 210,
      render: (row) => <span>{row.management_systems.length ? row.management_systems.join('、') : '未设置'}</span>,
    },
    {
      key: 'status',
      title: '状态',
      width: 120,
      render: (row) => (
        <span className={cn('inline-flex rounded-full px-[10px] py-[4px] text-[10px]', auditCaseStatusClass(row.status))}>
          {auditCaseStatusLabel(row.status)}
        </span>
      ),
    },
    {
      key: 'materials',
      title: '材料与覆盖率',
      width: 220,
      render: (row) => (
        <div>
          <span className="block text-[#464c5e]">{formatMaterialSummary(row.material_total, row.material_ready, row.material_failed)}</span>
          <span className="mt-[3px] block text-[11px] text-[#a0a6b5]">文件 {formatCoverage(row.file_coverage)} · 分块 {formatCoverage(row.chunk_coverage)}</span>
        </div>
      ),
    },
    {
      key: 'updated_at',
      title: '最近更新',
      width: 120,
      render: (row) => formatAuditCaseDate(row.updated_at),
    },
    {
      key: 'action',
      title: '',
      width: 54,
      align: 'right',
      render: (row) => <ArrowRight aria-hidden="true" className="ml-auto size-[16px] text-[#a4adbf]" onClick={() => onOpen(row.id)} />,
    },
  ];

  return (
    <DataTable
      aria-label="认证项目列表"
      columns={columns}
      data={rows}
      rowKey={(row) => row.id}
      loading={loading}
      emptyText="暂无认证项目"
      loadingText="正在加载认证项目…"
      onRowClick={(row) => onOpen(row.id)}
      className="bg-white"
    />
  );
}
