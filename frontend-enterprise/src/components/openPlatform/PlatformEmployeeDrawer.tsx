import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { cn } from '@/lib/utils';
import { Ban, XIcon } from 'lucide-react';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import EmployeeAvatar from '../EmployeeAvatar';
import type { AgentProfileRead } from '../../types';

import type { PlatformStat } from './PlatformEmployeeCard';

export type PlatformEmployeeDrawerProps = {
  open: boolean;
  agent: AgentProfileRead;
  platformTitle: string;
  name: ReactNode;
  role: ReactNode;
  description: ReactNode;
  detailText: ReactNode;
  workStyles: string[];
  stats: PlatformStat[];
  online?: boolean;
  canManage?: boolean;
  unpublishing?: boolean;
  hasPrev?: boolean;
  hasNext?: boolean;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  onUnpublish?: () => void;
  onUse: () => void;
};

function DrawerDivider() {
  return <div className="h-px w-full shrink-0 bg-[#e3e7f1]" />;
}

function NavChevron({
  direction,
  disabled,
  onClick,
  label,
}: {
  direction: 'prev' | 'next';
  disabled?: boolean;
  onClick?: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="grid size-[14px] place-items-center text-[#757f9c] transition-colors enabled:hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-35"
    >
      <IconChevronDown
        className={cn('size-[14px]', direction === 'prev' ? 'rotate-90' : '-rotate-90')}
      />
    </button>
  );
}

/**
 * SD1 数字员工广场详情侧拉（Figma 298:1416）。
 */
export default function PlatformEmployeeDrawer({
  open,
  agent,
  platformTitle,
  name,
  role,
  description,
  detailText,
  workStyles,
  stats,
  online = true,
  canManage = false,
  unpublishing = false,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onUnpublish,
  onUse,
}: PlatformEmployeeDrawerProps) {
  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent
        side="right"
        showCloseButton={false}
        className={cn(
          'platform-employee-drawer flex w-[400px] flex-col gap-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px_20px] shadow-[0_4px_15px_rgba(0,0,0,0.25)] sm:max-w-[400px]',
          'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
          '',
        )}
      >
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="text-[12px] font-medium capitalize text-[#464c5e]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一位员工" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一位员工" />
            </div>
            <button
              type="button"
              aria-label="关闭"
              onClick={onClose}
              className="grid size-[14px] place-items-center text-[#757f9c] transition-colors hover:text-[#18181a]"
            >
              <XIcon className="size-[14px]" strokeWidth={1.75} />
            </button>
          </div>
          <DrawerDivider />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto px-[4px] pt-[48px]">
          <div className="flex w-full items-end gap-[10px] pb-[4px]">
            <div className="flex h-[117.5px] w-[100px] shrink-0 items-end justify-center overflow-hidden">
              <EmployeeAvatar
                agent={agent}
                width={100}
                height={118}
                fit="contain"
                objectPosition="center bottom"
                className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
              />
            </div>
            <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] pb-[2px]">
              <div className="flex flex-col gap-[4px]">
                <p className="truncate text-[16px] font-medium capitalize text-[#464c5e]">
                  {name}
                </p>
                <p className="line-clamp-2 text-[12px] leading-[18px] text-[#757f9c]">
                  {description}
                </p>
              </div>
              <span
                className={cn(
                  'inline-flex w-fit items-center gap-[4px] rounded-[90px] border-[0.5px] px-[10px] py-[4px]',
                  online
                    ? 'border-[#96d9b0] bg-[#e9f7ef] text-[#2cb360]'
                    : 'border-[#d1d5db] bg-[#f3f4f6] text-[#757f9c]',
                )}
              >
                <i
                  className={cn('size-[4px] shrink-0 rounded-full shadow-[inset_1px_1px_2px_0.5px_rgba(0,0,0,0.05)]', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')}
                  aria-hidden="true"
                />
                <span className="text-[10px] capitalize">{online ? '在线' : '下线'}</span>
              </span>
            </div>
          </div>

          <div className="flex w-full items-stretch">
            {stats.map((stat, index) => (
              <div
                key={stat.label}
                className={cn(
                  'flex h-[60px] flex-1 flex-col justify-center gap-[4px] border-[0.5px] border-[#e3e7f1] px-[20px] py-[8px]',
                  index === 0 && 'rounded-l-[14px]',
                  index === stats.length - 1 && 'rounded-r-[14px]',
                  index > 0 && 'border-l-0',
                )}
              >
                <strong className="text-[18px] font-medium text-[#18181a]">{stat.value}</strong>
                <span className="text-[10px] text-[#464c5e]">{stat.label}</span>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-[10px]">
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">{platformTitle}</strong>
            </div>
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">分类</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">{role}</strong>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="text-[12px] capitalize text-[#464c5e]">说明</span>
            <div className="flex min-h-0 flex-1 flex-col gap-[10px]">
              {workStyles.length > 0 && (
                <div className="flex flex-wrap gap-[10px]">
                  {workStyles.slice(0, 3).map((tag) => (
                    <span
                      key={tag}
                      className="rounded-[10px] bg-[#f6f6f6] px-[12px] py-[4px] text-[12px] text-[#757f9c]"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}
              <p className="text-[12px] leading-[20px] text-[#757f9c]">
                {detailText}
              </p>
            </div>
          </div>
        </div>

        <DrawerDivider />

        <div className="flex shrink-0 justify-end gap-[10px]">
          {canManage && onUnpublish && (
            <button
              type="button"
              disabled={unpublishing}
              onClick={onUnpublish}
              className="inline-flex h-[34px] w-[112px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#efc2c2] bg-white text-[12px] text-[#b42318] transition-colors hover:border-[#d20b0b] hover:bg-[#fff7f7] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Ban className="size-[14px]" strokeWidth={1.8} />
              从广场下线
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] w-[80px] items-center justify-center rounded-[10px] bg-[#18181a] text-[12px] text-white transition-colors hover:bg-[#2a2a2e]"
          >
            使用员工
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
