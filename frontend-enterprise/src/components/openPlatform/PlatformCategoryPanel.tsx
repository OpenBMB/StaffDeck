import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';

export type PlatformCategoryPanelProps = {
  /** 分类图标，和页签使用同一套 SD1 线性 icon。 */
  icon: ReactNode;
  /** 分类标题，例如"数字员工广场"。 */
  title: ReactNode;
  /** 当前分类下的内容总数，展示在标题后面的括号里。 */
  count: number;
  /** 分类下方的筛选标签。 */
  filters?: string[];
  /** 搜索框当前的输入值，受控组件。 */
  searchValue: string;
  /** 搜索框的 placeholder，通常按分类给出，例如"搜索员工"。 */
  searchPlaceholder: string;
  /** 搜索框内容变化时触发。 */
  onSearchChange: (value: string) => void;
  /** 是否展示骨架屏。 */
  loading?: boolean;
  /** 当前分类是否没有内容（含搜索后没有匹配项），为 true 时显示空状态。 */
  isEmpty?: boolean;
  emptyText?: string;
  emptyHint?: string;
  /** 该分类下的全部卡片，一次性渲染，超出可视高度时靠面板自身滚动查看。 */
  children?: ReactNode;
  className?: string;
};

/**
 * 页签切换后展示的单分类内容面板：顶部是分类信息、筛选标签和搜索框，下面是卡片网格。
 * 卡片不做分页预览，全部渲染在网格里，多出的内容通过面板纵向滚动查看，
 * 因此不再需要旧版 PlatformColumn 上的"查看全部"按钮跳转。
 */
export default function PlatformCategoryPanel({
  icon,
  title,
  count,
  filters,
  searchValue,
  searchPlaceholder,
  onSearchChange,
  loading = false,
  isEmpty = false,
  emptyText = '暂无开放内容',
  emptyHint = '发布内容后会在这里展示',
  children,
  className,
}: PlatformCategoryPanelProps) {
  return (
    <div className={cn('flex h-full min-h-0 w-full flex-col gap-[14px]', className)}>
      <div className="flex w-full shrink-0 flex-col gap-[10px]">
        <div className="flex items-center gap-[6px]">
          <span className="flex size-[18px] shrink-0 items-center justify-center text-[#464c5e]">{icon}</span>
          <p className="text-[15px] font-semibold text-[#18181a]">
            {title}
            <span className="ml-[2px] font-normal text-[#98a2b3]">（{count}）</span>
          </p>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-[16px]">
          {filters && filters.length > 0 && (
            <div className="flex min-w-[160px] flex-1 flex-wrap items-center gap-[8px]">
              {filters.map((filter) => (
                <span
                  key={filter}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[10px] py-[4px] text-[11px] leading-[normal] text-[#757f9c]"
                >
                  {filter}
                </span>
              ))}
            </div>
          )}

          <label className="flex h-[34px] w-[260px] max-w-full shrink-0 items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              aria-label={searchPlaceholder}
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchValue}
              placeholder={searchPlaceholder}
              onChange={(event) => onSearchChange(event.target.value)}
              className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
            />
          </label>
        </div>

        <div className="h-px w-full bg-[#e3e7f1]" />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <PlatformCategorySkeleton />
        ) : isEmpty ? (
          <div className="flex min-h-[240px] w-full items-center justify-center rounded-[18px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[18px] py-[40px] text-center">
            <div className="flex max-w-[220px] flex-col items-center">
              <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
                <IconChevronDown className="size-[16px] rotate-90" />
              </span>
              <p className="mt-[12px] text-[13px] font-medium leading-[19px] text-[#7f879a]">{emptyText}</p>
              <p className="mt-[4px] text-[10px] leading-[16px] text-[#a7adbb]">{emptyHint}</p>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-[14px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
            {children}
          </div>
        )}
      </div>
    </div>
  );
}

// 分类内容加载中的占位骨架，数量和实际网格的常见展示密度接近。
function PlatformCategorySkeleton() {
  return (
    <div className="grid grid-cols-1 gap-[14px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: 10 }, (_, index) => (
        <div
          key={index}
          className="h-[140px] w-full animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]"
        />
      ))}
    </div>
  );
}
