import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type UnderlineTabItem<T extends string = string> = {
  value: T;
  label: ReactNode;
  disabled?: boolean;
};

/**
 * `dot`  – short centered rounded indicator under the active label (SD1 node 38:6404).
 * `line` – full-tab-width 2px active bar over a full-width bottom divider (SD1 node 281:1935).
 */
export type UnderlineTabsVariant = 'dot' | 'line';

export type UnderlineTabsProps<T extends string = string> = {
  items: UnderlineTabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  variant?: UnderlineTabsVariant;
  className?: string;
  /** Extra classes for each tab button (e.g. override the default fixed width). */
  tabClassName?: string;
  'aria-label'?: string;
};

/**
 * Global underline tab bar.
 * Use `variant="dot"` (default) for the short rounded indicator, or `variant="line"`
 * for the full-width divider with a full-tab-width active bar.
 */
export function UnderlineTabs<T extends string = string>({
  items,
  value,
  onChange,
  variant = 'dot',
  className,
  tabClassName,
  'aria-label': ariaLabel,
}: UnderlineTabsProps<T>) {
  const isLine = variant === 'line';
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn(
        'flex items-start',
        isLine && 'border-b-[0.5px] border-[#e3e7f1]',
        className,
      )}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={active}
            disabled={item.disabled}
            onClick={() => onChange(item.value)}
            className={cn(
              'relative flex w-[120px] items-start justify-center px-[16px] text-[14px] capitalize transition-colors outline-none',
              isLine ? 'pt-[6px] pb-[8px] mb-[-0.5px] border-b-2' : 'py-[6px]',
              isLine
                ? active
                  ? 'border-[#18181a] font-medium text-[#18181a]'
                  : 'border-transparent font-normal text-[#4f5669] hover:text-[#18181a]'
                : active
                  ? 'font-medium text-[#18181A]'
                  : 'font-normal text-[#858B9C] hover:text-[#18181A]',
              'disabled:cursor-not-allowed disabled:opacity-50',
              tabClassName,
            )}
          >
            {item.label}
            {!isLine && active && (
              <span
                aria-hidden="true"
                className="absolute top-[33px] left-1/2 h-[3px] w-[10px] -translate-x-1/2 rounded-[4px] bg-[#18181A] max-[560px]:top-auto max-[560px]:bottom-0"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}
