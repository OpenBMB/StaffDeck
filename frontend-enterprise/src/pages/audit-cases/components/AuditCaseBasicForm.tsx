import { Input } from '@/components/ui';

import type { AuditCaseCreateRequest } from '../auditCaseTypes';

export function AuditCaseBasicForm({
  draft,
  onChange,
}: {
  draft: AuditCaseCreateRequest;
  onChange: (patch: Partial<AuditCaseCreateRequest>) => void;
}) {
  return (
    <div className="grid gap-[16px]">
      <label className="grid gap-[6px] text-[12px] text-[#464c5e]">
        企业名称 <span className="text-[#d20b0b]">*</span>
        <Input
          aria-label="企业名称"
          value={draft.organization_name}
          onChange={(event) => onChange({ organization_name: event.target.value })}
          placeholder="例如：示例企业"
          className="h-[36px] rounded-[9px] text-[13px]"
        />
      </label>
      <label className="grid gap-[6px] text-[12px] text-[#464c5e]">
        报告类型 <span className="text-[#d20b0b]">*</span>
        <Input
          aria-label="报告类型"
          value={draft.report_type}
          onChange={(event) => onChange({ report_type: event.target.value })}
          placeholder="例如：再认证审核报告"
          className="h-[36px] rounded-[9px] text-[13px]"
        />
      </label>
      <label className="grid gap-[6px] text-[12px] text-[#464c5e]">
        管理体系
        <Input
          aria-label="管理体系"
          value={draft.management_systems.join('、')}
          onChange={(event) => onChange({ management_systems: event.target.value.split(/[、,，]/) })}
          placeholder="多个体系用顿号分隔"
          className="h-[36px] rounded-[9px] text-[13px]"
        />
      </label>
    </div>
  );
}
