import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';

export type PlatformColumnProps = {
  /** Small 14px glyph shown before the title. */
  icon: ReactNode;
  /** Column title, e.g. 数字员工广场. */
  title: ReactNode;
  /** Count shown on the right of the header. */
  count: number;
  /** Unit label rendered after the count, e.g. 员工 / 内容. */
  countLabel: string;
  /** Filter chips rendered under the title. */
  filters?: string[];
  /** Renders a skeleton-free muted list while data loads. */
  loading?: boolean;
  error?: string;
  /** Whether the column has no content — shows the empty placeholder. */
  isEmpty?: boolean;
  /** Text for the empty placeholder. */
  emptyText?: string;
  /** Fired when the "查看全部" button is pressed. */
  onViewAll?: () => void;
  /** The column's cards. */
  children?: ReactNode;
  className?: string;
};

/**
 * Shared shell for a single 开放广场 column. It captures the parts that repeat
 * across all five modules (数字员工 / 知识库 / 技能 / SOP / 工具): the icon+title
 * header with a count, the filter chip row, the divider, the card list (or an
 * empty placeholder) and the "查看全部" footer button. Each module only supplies
 * its own cards via `children`.
 */
export default function PlatformColumn({
  icon,
  title,
  count,
  countLabel,
  filters,
  loading = false,
  error,
  isEmpty = false,
  emptyText = '暂无开放内容',
  onViewAll,
  children,
  className,
}: PlatformColumnProps) {
  return (
    <section
      className={cn(
        'flex h-full min-h-0 w-full min-w-[180px] flex-col items-center gap-[10px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[12px] py-[14px]',
        '',
        className,
      )}
    >
      <div className="flex w-full min-h-0 flex-1 flex-col gap-[16px]">
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex w-full items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="flex size-[14px] shrink-0 items-center justify-center text-[#464c5e]">
                {icon}
              </span>
              <p className="truncate text-[12px] font-medium text-[#464c5e]">{title}</p>
            </div>
            <div className="flex shrink-0 items-center gap-[2px] text-[12px] text-[#464c5e]">
              <span>{error || loading ? '—' : count}</span>
              {/* <span>{countLabel}</span> */}
              {/* <IconChevronDown className="size-[14px] text-[#757f9c]" /> */}
            </div>
          </div>

          {filters && filters.length > 0 && (
            <div className="flex flex-wrap items-center gap-[6px]">
              {filters.map((filter) => (
                <span
                  key={filter}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px] text-[10px] leading-[normal] text-[#757f9c]"
                >
                  {filter}
                </span>
              ))}
            </div>
          )}

          <div className="h-px w-full bg-[#e3e7f1]" />
        </div>

        <div className="mr-[-12px] flex min-h-0 w-[calc(100%+12px)] flex-1 flex-col gap-[16px] overflow-y-auto pr-[12px]">
          {error ? <p role="alert" className="text-sm text-amber-800">{error}</p> : loading ? (
            <PlatformColumnSkeleton />
          ) : isEmpty ? (
            <div className="flex min-h-[180px] w-full flex-1 items-center justify-center rounded-[18px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[18px] py-[28px] text-center">
              <div className="flex max-w-[180px] flex-col items-center">
                <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
                  <IconChevronDown className="size-[16px] rotate-90" />
                </span>
                <p className="mt-[12px] text-[13px] font-medium leading-[19px] text-[#7f879a]">
                  {emptyText}
                </p>
                <p className="mt-[4px] text-[10px] leading-[16px] text-[#a7adbb]">
                  发布内容后会在这里展示
                </p>
              </div>
            </div>
          ) : (
            children
          )}
        </div>
      </div>

      {!isEmpty && (
        <>
          <div className="h-px w-full shrink-0 bg-[#e3e7f1]" />

          <button
            type="button"
            onClick={onViewAll}
            className="flex w-[120px] shrink-0 items-center justify-center gap-[2px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] py-[8px] text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
          >
            查看全部
            <IconChevronDown className="size-[14px] shrink-0 -rotate-90" />
          </button>
        </>
      )}
    </section>
  );
}

function PlatformColumnSkeleton() {
  return (
    <div className="flex w-full flex-col gap-[16px]">
      {[0, 1, 2].map((index) => (
        <div
          key={index}
          className="h-[112px] w-full shrink-0 animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]"
        />
      ))}
    </div>
  );
}
