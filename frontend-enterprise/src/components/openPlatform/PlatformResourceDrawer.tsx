import type { ReactNode } from 'react';

import { Sheet, SheetContent } from '@/components/ui';
import { cn } from '@/lib/utils';
import { MOTION, useStaggerReveal } from '@/lib/motion';
import { Download, XIcon } from 'lucide-react';

import IconChevronDown from '../../assets/icons/chevron-down.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';

import { platformResourceAccentStyles, type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformResourceDrawerProps = {
  open: boolean;
  platformTitle: string;
  icon: ReactNode;
  accent?: PlatformResourceAccent;
  title: ReactNode;
  description: ReactNode;
  badge: ReactNode;
  categoryMeta: ReactNode;
  /**
   * 第二个信息格的标题。默认「分类」；Agent 广场这类没有分类的资源传「作者 / 更新时间」，
   * 避免两格都写「分类」把作者信息说成分类。
   */
  categoryMetaLabel?: string;
  /**
   * 第一个信息格的标题。默认「分类」（值是 platformTitle，即所属广场名）；
   * 技能广场把第二格用作真正的「分类」时，这里改传「所属广场」以免两格同名。
   */
  platformTitleLabel?: string;
  detailText: ReactNode;
  useLabel: string;
  canManage?: boolean;
  deleting?: boolean;
  /** 提供后底部出现「下载」按钮（目前仅技能广场资源支持）。 */
  onDownload?: () => void;
  downloading?: boolean;
  /** 下载进行中的真实进度；存在且非空时在下载按钮行上方显示进度条（弹窗内可见）。 */
  downloadProgress?: { percent: number | null; receivedBytes?: number } | null;
  hasPrev?: boolean;
  hasNext?: boolean;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  onDelete?: () => void;
  onUse: () => void;
};

const DRAWER_SHEET_CLASS = cn(
  'platform-resource-drawer flex w-[400px] flex-col gap-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px_20px] shadow-[0_4px_15px_rgba(0,0,0,0.25)] sm:max-w-[400px]',
  'top-[24px]! right-[24px]! bottom-[24px]! left-auto! h-auto! max-h-[calc(100vh-48px)] rounded-[20px]',
  '',
);

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
 * SD1 广场资源详情侧拉（知识库 298:4801 / SOP·技能·工具 298:4869 系列）。
 */
export default function PlatformResourceDrawer({
  open,
  platformTitle,
  icon,
  accent = 'green',
  title,
  description,
  badge,
  categoryMeta,
  categoryMetaLabel,
  platformTitleLabel,
  detailText,
  useLabel,
  canManage = false,
  deleting = false,
  onDownload,
  downloading = false,
  downloadProgress = null,
  hasPrev = false,
  hasNext = false,
  onClose,
  onPrev,
  onNext,
  onDelete,
  onUse,
}: PlatformResourceDrawerProps) {
  const accentStyles = platformResourceAccentStyles[accent];
  // 抽屉内容分两段错开入场：主体（图标 → 标题 → 信息格 → 说明）与底部操作。
  // 抽屉每次打开都是重新挂载（调用方用 detailItem 控制渲染），所以只要一次入场。
  const bodyRef = useStaggerReveal<HTMLDivElement>(open, {
    selector: ':scope > *',
    y: 10,
    stagger: 55,
    duration: MOTION.base,
  });
  const footerRef = useStaggerReveal<HTMLDivElement>(open, {
    selector: ':scope > *',
    y: 8,
    stagger: 45,
    duration: MOTION.base,
  });

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent side="right" showCloseButton={false} className={DRAWER_SHEET_CLASS}>
        <div className="flex w-full shrink-0 flex-col gap-[10px]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-[4px]">
              <span className="text-[12px] font-medium capitalize text-[#464c5e]">
                {platformTitle}
              </span>
              <NavChevron direction="prev" disabled={!hasPrev} onClick={onPrev} label="上一项" />
              <NavChevron direction="next" disabled={!hasNext} onClick={onNext} label="下一项" />
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

        <div ref={bodyRef} className="flex min-h-0 flex-1 flex-col gap-[10px] overflow-auto px-[4px]">
          <div className="size-[36px] shrink-0">{icon}</div>

          <div className="flex min-h-[75px] w-full flex-col justify-center gap-[8px] pb-[2px]">
            <div className="flex flex-col gap-[4px]">
              <p className="text-[16px] font-medium capitalize text-[#464c5e]">
                {title}
              </p>
              <p className="text-[12px] leading-[18px] text-[#757f9c]">
                {description}
              </p>
            </div>
            <span
              className={cn(
                'inline-flex w-fit items-center rounded-[90px] px-[10px] py-[4px] text-[10px] capitalize',
                accentStyles.tag,
              )}
            >
              {badge}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-[10px]">
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">{platformTitleLabel || '分类'}</span>
              <strong className="truncate text-[12px] leading-[16px] font-medium text-[#18181a]">
                {platformTitle}
              </strong>
            </div>
            <div className="flex min-h-[60px] flex-col justify-center gap-[4px] rounded-[14px] border-[0.5px] border-[#e3e7f1] px-[16px] py-[8px]">
              <span className="text-[10px] leading-[13px] text-[#464c5e]">{categoryMetaLabel || '分类'}</span>
              <strong className={cn('truncate text-[12px] leading-[16px] font-medium', accentStyles.meta)}>
                {categoryMeta}
              </strong>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-[8px]">
            <span className="text-[12px] capitalize text-[#464c5e]">说明</span>
            <p className="text-[12px] leading-[20px] text-[#757f9c]">
              {detailText}
            </p>
          </div>
        </div>

        <DrawerDivider />

        <DrawerDivider />

        {downloading && downloadProgress && (
          <div aria-live="polite" className="mb-[12px] shrink-0">
            <div
              role="progressbar"
              aria-label="技能包下载进度"
              aria-valuenow={downloadProgress.percent ?? undefined}
              aria-valuemin={0}
              aria-valuemax={100}
              className="h-[5px] w-full overflow-hidden rounded-full bg-[#eef1f7]"
            >
              <div
                className={cn(
                  'h-full rounded-full bg-[#18181a] transition-[width] duration-150',
                  (downloadProgress.percent === null || downloadProgress.percent === 0) && 'w-1/3 animate-pulse',
                )}
                style={downloadProgress.percent !== null && downloadProgress.percent > 0 ? { width: `${downloadProgress.percent}%` } : undefined}
              />
            </div>
            <p className="mt-[4px] text-[11px] text-[#858b9c]">
              {downloadProgress.percent !== null && downloadProgress.percent > 0
                ? `${downloadProgress.percent}%`
                : downloadProgress.receivedBytes
                  ? `已接收 ${(downloadProgress.receivedBytes / 1_048_576).toFixed(1)} MB`
                  : '准备下载…'}
            </p>
          </div>
        )}

        <div ref={footerRef} className="flex shrink-0 justify-end gap-[10px]">
          {onDownload && (
            <button
              type="button"
              disabled={downloading}
              onClick={onDownload}
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#dfe4ee] bg-white text-[12px] text-[#464c5e] transition-colors hover:bg-[#f4f6fa] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Download className="size-[14px]" />
              {downloading ? '下载中' : '下载'}
            </button>
          )}
          {canManage && onDelete && (
            <button
              type="button"
              disabled={deleting}
              onClick={onDelete}
              className="inline-flex h-[34px] w-[80px] items-center justify-center gap-[4px] rounded-[10px] border-[0.5px] border-[#d20b0b] bg-white text-[12px] text-[#d20b0b] transition-colors hover:bg-[#fce7e7] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <IconTrash className="size-[14px]" />
              删除
            </button>
          )}
          <button
            type="button"
            onClick={onUse}
            className="inline-flex h-[34px] items-center justify-center rounded-[10px] bg-[#18181a] px-[20px] text-[12px] text-white transition-colors hover:bg-[#2a2a2e]"
          >
            {useLabel}
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
