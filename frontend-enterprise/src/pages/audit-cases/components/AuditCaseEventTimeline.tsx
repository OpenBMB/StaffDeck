import type { AuditCaseEventRead } from '@/types';
import { formatAuditCaseDate } from '../auditCasePresentation';

function eventLabel(eventType: string): string {
  const labels: Record<string, string> = {
    'audit_case.created': '创建项目',
    'audit_case.updated': '更新项目',
    'audit_case.members_replaced': '调整项目成员',
    'audit_case.material_added': '上传材料',
    'audit_case.material_replaced': '替换材料',
    'audit_case.material_processed': '处理材料',
    'audit_case.archived': '归档项目',
  };
  return labels[eventType] || eventType;
}

export function AuditCaseEventTimeline({ events }: { events: AuditCaseEventRead[] }) {
  if (events.length === 0) return <p className="text-[12px] text-[#858b9c]">暂无操作记录</p>;
  return (
    <ol className="grid gap-[12px]">
      {events.map((event) => (
        <li key={event.id} className="flex items-start gap-[10px] rounded-[10px] border border-[#edf0f5] bg-white px-[14px] py-[12px]">
          <span className="mt-[4px] size-[7px] shrink-0 rounded-full bg-[#4d8cff]" />
          <span className="min-w-0 flex-1">
            <span className="block text-[12px] font-medium text-[#464c5e]">{eventLabel(event.event_type)}</span>
            <span className="mt-[3px] block text-[11px] text-[#a0a6b5]">操作账号 {event.actor_user_id} · {formatAuditCaseDate(event.created_at)}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}
