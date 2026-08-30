import type { KnowledgeBaseRead } from '@/types';

type KnowledgeTypeBadgeProps = {
  mode?: KnowledgeBaseRead['mode'];
};

export function KnowledgeTypeBadge({ mode = 'dedicated' }: KnowledgeTypeBadgeProps) {
  /** 用产品确认的名称区分员工专用库与团队可绑定共享库。 */
  const shared = mode === 'shared';
  const label = shared ? '共享知识库' : '专用知识库';
  return (
    <span
      aria-label={`知识库类型：${label}`}
      className={shared
        ? 'inline-flex items-center rounded-full bg-[#ede9fe] px-[9px] py-[3px] text-[11px] font-medium text-[#6d28d9]'
        : 'inline-flex items-center rounded-full bg-[#eef2f7] px-[9px] py-[3px] text-[11px] font-medium text-[#596174]'}
    >
      {label}
    </span>
  );
}
