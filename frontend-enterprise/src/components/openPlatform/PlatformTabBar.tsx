import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import type { PlatformKind } from './types';

export type PlatformTabItem = {
  kind: PlatformKind;
  label: string;
  count: number;
  icon: ReactNode;
};

// 每个分类页签选中时使用的强调色，取值和 PlatformResourceCard 各分类的强调色保持一致，
// 数字员工广场没有独立的强调色，用中性深色作为默认选中态。
const TAB_ACCENT_COLOR: Record<PlatformKind, string> = {
  agents: '#18181a',
  knowledge: '#2cb360',
  'general-skills': '#1a71ff',
  skills: '#27c9ff',
  tools: '#ff7f00',
};

export type PlatformTabBarProps = {
  /** 5 个分类页签，顺序即展示顺序。 */
  items: PlatformTabItem[];
  /** 当前选中的分类。 */
  activeKind: PlatformKind;
  /** 用户点击某个页签时触发，由外部负责切换 activeKind。 */
  onChange: (kind: PlatformKind) => void;
  className?: string;
};

/**
 * 开放广场页签切换控件：分段胶囊样式，5 个分类共用一个圆角容器，选中项浮起为白色胶囊。
 * 用于替代此前"数字员工/知识库/技能/SOP/工具"五列并排、窄屏下容易显得拥挤的布局。
 */
export default function PlatformTabBar({ items, activeKind, onChange, className }: PlatformTabBarProps) {
  return (
    <div
      role="tablist"
      className={cn('inline-flex flex-wrap items-center gap-[2px] rounded-[14px] bg-[#f1f2f4] p-[4px]', className)}
    >
      {items.map((item) => {
        const isActive = item.kind === activeKind;
        return (
          <button
            key={item.kind}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(item.kind)}
            style={isActive ? { color: TAB_ACCENT_COLOR[item.kind] } : undefined}
            className={cn(
              'inline-flex items-center gap-[6px] whitespace-nowrap rounded-[11px] px-[16px] py-[9px] text-[13px] font-medium text-[#757f9c] transition-colors',
              isActive && 'bg-white shadow-[0_1px_6px_rgba(15,23,42,0.10)]',
            )}
          >
            <span className="flex size-[14px] shrink-0 items-center justify-center">{item.icon}</span>
            {item.label}
            <span className="text-[10.5px] font-normal text-[#98a2b3]">{item.count}</span>
          </button>
        );
      })}
    </div>
  );
}
