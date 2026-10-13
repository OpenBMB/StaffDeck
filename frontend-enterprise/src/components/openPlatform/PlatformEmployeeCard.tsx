import { Ban } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';

import { cn } from '@/lib/utils';
import { usePointerFx } from '@/lib/motion';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';

export type PlatformStat = {
  value: ReactNode;
  label: string;
};

export type PlatformEmployeeCardProps = {
  /** Avatar illustration, typically an <EmployeeAvatar />. */
  avatar: ReactNode;
  name: ReactNode;
  role: ReactNode;
  online?: boolean;
  description: ReactNode;
  /** Bottom metric segments (资料 / 技能 / SOP …). */
  stats: PlatformStat[];
  /** 免费员工徽标(管理员标记了全局免费模型的员工):卡片右上角常驻。 */
  freeBadge?: boolean;
  onOpen?: () => void;
  onUnpublish?: () => void;
  unpublishing?: boolean;
  className?: string;
};

/** Compact 数字员工广场 card with an admin-only gallery governance action. */
export default function PlatformEmployeeCard({
  avatar,
  name,
  role,
  online = true,
  description,
  stats,
  freeBadge = false,
  onOpen,
  onUnpublish,
  unpublishing = false,
  className,
}: PlatformEmployeeCardProps) {
  // 指针抬起 + 微倾斜 + 跟随光斑（reduced-motion 下 usePointerFx 直接不绑定）
  const ref = usePointerFx<HTMLElement>({ tilt: 4, lift: 4, scale: 1.008 });
  return (
    <article
      ref={ref}
      style={{ '--pointer-fx-glow': 'rgba(109, 63, 212, 0.20)' } as CSSProperties}
      className={cn(
        'group pointer-fx relative h-[180px] w-full shrink-0 rounded-[20px] border-[0.5px] border-[#dfe4ee] bg-white p-[6px] text-left transition-[box-shadow,border-color] hover:border-[#cbd3e6] hover:shadow-[0_16px_34px_rgba(15,23,42,0.10)]',
        className,
      )}
    >
      <span aria-hidden className="pointer-fx-spotlight" />
      {freeBadge && <FreeBadge />}
      <button
        type="button"
        onClick={onOpen}
        className="relative flex h-full w-full flex-col justify-end gap-[8px] rounded-[16px] text-left outline-none focus-visible:ring-2 focus-visible:ring-[#9dd7cf]"
      >
        <div className="flex w-full flex-col px-[8px] pb-[2px]">
          <div className="flex h-[72px] w-full items-end justify-between rounded-[14px] bg-[#f6f6f6] px-[12px] pb-[6px] pt-[10px]">
            <div className="flex min-w-0 items-end gap-[12px]">
              <div className="flex h-[78px] w-[66px] shrink-0 items-end justify-center">
                {avatar}
              </div>
              <div className="flex min-w-0 flex-col items-start justify-center gap-[4px]">
                <p className="truncate text-[15px] leading-[1.35] font-medium text-[#18181a]">
                  {typeof name === 'string' && name.includes(' @')
                    ? (
                      <>
                        <span>{name.slice(0, name.indexOf(' @'))}</span>
                        {/* 作者名比员工名小一号、弱化字重，仅作层级提示 */}
                        <span className="text-[12px] font-normal">{name.slice(name.indexOf(' @'))}</span>
                      </>
                    )
                    : name}
                </p>
                <p className="truncate text-[11px] leading-[1.6] text-[#757f9c]">{role}</p>
                <span className="inline-flex w-[40px] items-center justify-center rounded-[90px] bg-white px-[5px] py-[2px]">
                  <span className="flex items-center gap-[3px]">
                    <i
                      className={cn('size-[5px] shrink-0 rounded-full', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')}
                      aria-hidden="true"
                    />
                    <span className="text-[10px] text-[#757f9c]">{online ? '在线' : '下线'}</span>
                  </span>
                </span>
              </div>
            </div>
            <span className="grid size-[28px] shrink-0 self-center place-items-center rounded-[10px] bg-white text-[#757f9c] transition-colors group-hover:text-[#18181a]">
              <IconArrowRight className="size-[16px]" />
            </span>
          </div>
        </div>

        <p className="line-clamp-2 h-[36px] w-full px-[10px] text-[12px] leading-[18px] text-[#757f9c]">
          {description}
        </p>

        <div className="flex w-full items-stretch px-[10px] pb-[4px]">
          {stats.map((stat, index) => (
            <div
              key={stat.label}
              className={cn(
                'flex h-[34px] flex-1 items-center justify-center border-[0.5px] border-[#e3e7f1] px-[10px]',
                index === 0 && 'rounded-l-[10px]',
                index === stats.length - 1 && 'rounded-r-[10px]',
                index > 0 && 'border-l-0',
              )}
            >
              <span className="flex items-baseline gap-[2px] leading-none">
                <span className="text-[13px] font-medium text-[#18181a]">{stat.value}</span>
                <span className="text-[10px] text-[#464c5e]">{stat.label}</span>
              </span>
            </div>
          ))}
        </div>
      </button>

      {onUnpublish && (
        <button
          type="button"
          aria-label="从广场下线"
          title="从广场下线"
          disabled={unpublishing}
          onClick={onUnpublish}
          className={cn(
            'absolute top-[8px] inline-flex h-[24px] items-center gap-[4px] rounded-[9px] border border-[#f3c7c7] bg-white px-[7px] text-[9px] font-medium text-[#b42318] shadow-[0_3px_10px_rgba(20,20,20,0.06)] transition-all',
            // 有免费徽标时让位到徽标左侧，避免 hover 时互相压住
            freeBadge ? 'right-[44px]' : 'right-[8px]',
            'pointer-events-none opacity-0 group-hover:pointer-events-auto group-hover:opacity-100 focus-visible:pointer-events-auto focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f1aaaa]',
            'hover:border-[#e49b9b] hover:bg-[#fff7f7] disabled:cursor-wait disabled:opacity-50',
          )}
        >
          <Ban className="size-[11px]" strokeWidth={1.8} />
          下线
        </button>
      )}
    </article>
  );
}

/** 「免费」徽标:免费员工卡片右上角常驻(头像在左上,右上更醒目),hover 下线按钮让位到徽标左侧。 */
export function FreeBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'pointer-events-none absolute top-[8px] right-[8px] z-10 inline-flex h-[22px] items-center rounded-[9px] border border-[#8fd8bd] bg-[#e2f9ef] px-[9px] text-[10px] font-semibold leading-none text-[#0f8a5f] shadow-[0_2px_8px_rgba(15,138,95,0.14)]',
        className,
      )}
    >
      免费
    </span>
  );
}
