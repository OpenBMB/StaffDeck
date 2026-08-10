import { cn } from '@/lib/utils';
import logoMark from '../assets/hmnmkw-logo.png';

export type BrandLogoProps = {
  /** Hide the "Hemony&Miconvey" wordmark and only render the logo mark. */
  markOnly?: boolean;
  /** Height of the logo mark in pixels (width auto-scales to preserve aspect ratio). */
  markSize?: number;
  className?: string;
  /** Extra classes applied to the wordmark wrapper (e.g. to hide it responsively). */
  wordmarkClassName?: string;
};

/** Brand logo lockup (logo mark + "Hemony&Miconvey" wordmark). */
export default function BrandLogo({
  markOnly = false,
  markSize = 28,
  className,
  wordmarkClassName,
}: BrandLogoProps) {
  return (
    <span className={cn('flex items-center gap-[8px] overflow-hidden p-[4px]', className)}>
      <img
        src={logoMark}
        alt="Hemony&Miconvey"
        className="shrink-0"
        style={{ height: markSize, width: 'auto' }}
      />
      {!markOnly && (
        <span className={cn('flex flex-col items-center gap-[2px] leading-none', wordmarkClassName)}>
          <strong className="text-[17px] font-semibold leading-none text-[#18181a]">
            Hemony&Miconvey
          </strong>
        </span>
      )}
    </span>
  );
}
