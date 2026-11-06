import { useEffect, useMemo, useState } from 'react';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { cn } from '@/lib/utils';

export type SearchableSelectOption = {
  value: string;
  label: string;
  /** 参与过滤的补充关键字(如用户名),命中任一即展示。 */
  keywords?: string[];
};

type Props = {
  value: string;
  onValueChange: (value: string) => void;
  options: SearchableSelectOption[];
  /** 未选中时触发器展示的占位文案。 */
  placeholder?: string;
  /** 搜索框占位文案。 */
  searchPlaceholder?: string;
  /** 搜索无结果的提示文案。 */
  emptyText?: string;
  className?: string;
  disabled?: boolean;
};

export default function SearchableSelect({
  value,
  onValueChange,
  options,
  placeholder = '请选择',
  searchPlaceholder = '搜索',
  emptyText = '无匹配项',
  className,
  disabled = false,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);

  const selected = options.find((option) => option.value === value) || null;

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return options;
    return options.filter((option) => {
      const haystack = [option.label, ...(option.keywords || [])]
        .join('\n')
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    setQuery('');
    const selectedIndex = filtered.findIndex((option) => option.value === value);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function choose(option: SearchableSelectOption) {
    onValueChange(option.value);
    setOpen(false);
  }

  function handleKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (filtered.length === 0) return;
      setActiveIndex((current) => {
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        return (current + delta + filtered.length) % filtered.length;
      });
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const option = filtered[activeIndex];
      if (option) choose(option);
    }
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <UIButton
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          className={cn(
            'h-[32px] w-[180px] justify-between rounded-[10px] border-[#e3e7f1] px-[10px] text-[12px] font-normal text-[#18181a] hover:bg-[#f6f6f6]',
            className,
          )}
        >
          <span className={cn('min-w-0 flex-1 truncate text-left', !selected && 'text-[#858b9c]')}>
            {selected ? selected.label : placeholder}
          </span>
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={cn('size-[14px] shrink-0 text-[#858b9c] transition-transform', open && 'rotate-180')}
          >
            <path d="m6 9 6 6 6-6" />
          </svg>
        </UIButton>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[220px] gap-[6px] p-[8px]"
        onOpenAutoFocus={(event) => event.preventDefault()}
      >
        <Input
          type="text"
          autoFocus
          value={query}
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          placeholder={searchPlaceholder}
          onChange={(event) => {
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          onKeyDown={handleKeyDown}
          className="h-8 rounded-[10px] text-[12px]"
        />
        <div className="flex max-h-[240px] min-h-[60px] flex-col gap-[2px] overflow-y-auto pt-[2px]">
          {filtered.length === 0 ? (
            <span className="py-[12px] text-center text-[12px] text-[#858b9c]">{emptyText}</span>
          ) : (
            filtered.map((option, index) => (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={option.value === value}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(option)}
                className={cn(
                  'flex w-full items-center justify-between gap-[8px] rounded-[8px] px-[10px] py-[6px] text-left text-[12px] text-[#18181a] outline-none',
                  index === activeIndex && 'bg-[#f2f3f7]',
                  option.value === value && 'font-medium',
                )}
              >
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {option.value === value && (
                  <svg
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="size-[14px] shrink-0 text-[#18181a]"
                  >
                    <path d="M20 6 9 17l-5-5" />
                  </svg>
                )}
              </button>
            ))
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
