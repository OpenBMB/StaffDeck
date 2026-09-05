import { useMemo, useState } from 'react';

import { Input } from '@/components/ui';
import type { AuditCaseManagementOptions } from '@/types';

import type { AuditCaseCreateRequest } from '../auditCaseTypes';

const DEFAULT_AUDIT_TYPES = [
  { value: '一阶段审核', label: '一阶段审核', code: 'A' },
  { value: '认证审核', label: '认证审核', code: 'B' },
  { value: '第一次监督审核', label: '第一次监督审核', code: 'C' },
  { value: '第二次监督审核', label: '第二次监督审核', code: 'D' },
  { value: '再认证审核', label: '再认证审核', code: 'E' },
  { value: '补充审核', label: '补充审核' },
  { value: '扩项审核', label: '扩项审核' },
  { value: '确认审核', label: '确认审核' },
  { value: '暂停恢复审核', label: '暂停恢复审核' },
  { value: '标准转换审核', label: '标准转换审核' },
  { value: '证后监督', label: '证后监督', code: 'H' },
  { value: '其他审核', label: '其他审核' },
];

const DEFAULT_MANAGEMENT_SYSTEMS = [
  '质量管理体系',
  '环境管理体系',
  '职业健康安全管理体系',
  '信息安全管理体系',
  '信息技术服务管理体系',
  '能源管理体系',
  '食品安全管理体系',
  '其他管理体系',
].map((value) => ({ value, label: value }));

export function AuditCaseBasicForm({
  draft,
  options,
  onChange,
}: {
  draft: AuditCaseCreateRequest;
  options?: AuditCaseManagementOptions;
  onChange: (patch: Partial<AuditCaseCreateRequest>) => void;
}) {
  const [systemsOpen, setSystemsOpen] = useState(false);
  const [systemQuery, setSystemQuery] = useState('');
  const auditTypes = useMemo(() => {
    const configured = options?.audit_types?.length ? options.audit_types : DEFAULT_AUDIT_TYPES;
    return draft.report_type && !configured.some((item) => item.value === draft.report_type)
      ? [{ value: draft.report_type, label: draft.report_type }, ...configured]
      : configured;
  }, [draft.report_type, options?.audit_types]);
  const managementSystems = useMemo(() => {
    const configured = options?.management_systems?.length
      ? options.management_systems
      : DEFAULT_MANAGEMENT_SYSTEMS;
    return draft.management_systems.reduce(
      (items, value) => items.some((item) => item.value === value)
        ? items
        : [{ value, label: value }, ...items],
      [...configured],
    );
  }, [draft.management_systems, options?.management_systems]);
  const visibleManagementSystems = managementSystems.filter((item) => (
    !systemQuery.trim() || item.label.toLocaleLowerCase().includes(systemQuery.trim().toLocaleLowerCase())
  ));

  function toggleManagementSystem(value: string) {
    onChange({
      management_systems: draft.management_systems.includes(value)
        ? draft.management_systems.filter((item) => item !== value)
        : [...draft.management_systems, value],
    });
  }

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
        审核类型 <span className="text-[#d20b0b]">*</span>
        <select
          aria-label="审核类型"
          value={draft.report_type}
          onChange={(event) => onChange({ report_type: event.target.value })}
          className="h-[36px] rounded-[9px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#464c5e] outline-none focus:border-[#aebbd3]"
        >
          <option value="">请选择审核类型</option>
          {auditTypes.map((item) => (
            <option key={item.value} value={item.value}>{item.code ? `${item.code}、${item.label}` : item.label}</option>
          ))}
        </select>
      </label>
      <div className="grid gap-[6px] text-[12px] text-[#464c5e]">
        <span>管理体系</span>
        <button
          type="button"
          aria-label="选择管理体系"
          aria-expanded={systemsOpen}
          onClick={() => setSystemsOpen((open) => !open)}
          className="flex min-h-[36px] items-center justify-between rounded-[9px] border border-[#e3e7f1] bg-white px-[10px] text-left text-[13px] text-[#464c5e] outline-none hover:border-[#cbd3e6]"
        >
          <span className={draft.management_systems.length ? 'text-[#464c5e]' : 'text-[#a0a6b5]'}>
            {draft.management_systems.length ? `已选择 ${draft.management_systems.length} 个管理体系` : '请选择管理体系'}
          </span>
          <span className="text-[11px] text-[#a0a6b5]">{systemsOpen ? '收起' : '展开'}</span>
        </button>
        {draft.management_systems.length > 0 && (
          <div className="flex flex-wrap gap-[6px]">
            {draft.management_systems.map((value) => (
              <span key={value} className="inline-flex items-center gap-[5px] rounded-full bg-[#f1f5ff] px-[8px] py-[4px] text-[11px] text-[#4669a8]">
                {value}
                <button type="button" aria-label={`移除${value}`} onClick={() => toggleManagementSystem(value)} className="text-[#7993c5] hover:text-[#274e91]">×</button>
              </span>
            ))}
          </div>
        )}
        {systemsOpen && (
          <div className="grid gap-[8px] rounded-[9px] border border-[#e3e7f1] bg-white p-[10px] shadow-sm">
            <Input
              aria-label="搜索管理体系"
              value={systemQuery}
              onChange={(event) => setSystemQuery(event.target.value)}
              placeholder="搜索管理体系"
              className="h-[32px] rounded-[8px] text-[12px]"
            />
            <div className="grid max-h-[180px] gap-[2px] overflow-y-auto">
              {visibleManagementSystems.map((item) => (
                <label key={item.value} className="flex cursor-pointer items-center gap-[8px] rounded-[7px] px-[7px] py-[7px] hover:bg-[#f7f8fb]">
                  <input
                    type="checkbox"
                    aria-label={item.label}
                    checked={draft.management_systems.includes(item.value)}
                    onChange={() => toggleManagementSystem(item.value)}
                  />
                  <span>{item.label}</span>
                </label>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
